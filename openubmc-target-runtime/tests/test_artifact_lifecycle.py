from __future__ import annotations

import json
import hashlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    ArtifactRef,
    LocalArtifactStore,
    ReferenceViolation,
    SQLiteArtifactRepository,
)


class ArtifactLifecycleTests(unittest.TestCase):
    def test_gc_and_new_registration_leave_the_new_reference_readable(self) -> None:
        for persistent in (False, True):
            with self.subTest(persistent=persistent), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                now = [100.0]
                store = LocalArtifactStore(
                    content_root=root / "content",
                    repository=SQLiteArtifactRepository(root / "artifacts.sqlite") if persistent else None,
                    clock=lambda: now[0], temporary_retention_seconds=1,
                )
                source = root / "evidence.log"
                source.write_bytes(b"shared evidence")

                def register(run_id: str, retention: str):
                    return store.put(
                        source, kind="test-evidence", provenance="test",
                        retention_hint=retention, target="192.0.2.1", run_id=run_id,
                        created_by_effect=run_id + "-effect",
                    )

                old = register("old-run", "temporary")
                content_path = store.resolve(old)
                now[0] = 200.0
                deleting = threading.Event()
                resume_delete = threading.Event()
                unlink = os.unlink

                def delayed_unlink(path, *args, **kwargs):
                    if os.fspath(path) == str(content_path):
                        deleting.set()
                        if not resume_delete.wait(5):
                            raise TimeoutError("filesystem deletion was not released")
                    return unlink(path, *args, **kwargs)

                with patch("os.unlink", side_effect=delayed_unlink), ThreadPoolExecutor(max_workers=2) as executor:
                    collection = executor.submit(store.garbage_collect)
                    try:
                        self.assertTrue(deleting.wait(5), "GC did not reach the filesystem")
                        registration = executor.submit(register, "new-run", "audit")
                        try:
                            registration.result(timeout=2)
                        except FutureTimeout:
                            # Atomic implementations may serialize registration behind GC.
                            pass
                    finally:
                        resume_delete.set()
                    collection.result(timeout=5)
                    fresh = registration.result(timeout=5)
                self.assertEqual(store.resolve(fresh).read_bytes(), b"shared evidence")

    def test_expected_digest_is_rechecked_at_the_persisted_copy_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "firmware.hpm"
            expected_body = b"expected firmware"
            source.write_bytes(expected_body)
            expected_digest = hashlib.sha256(expected_body).hexdigest()

            class RacingArtifactStore(LocalArtifactStore):
                def _put(self, path: Path, **kwargs):
                    Path(path).write_bytes(b"replacement firmware")
                    return super()._put(path, **kwargs)

            store = RacingArtifactStore(content_root=root / "content")

            with self.assertRaisesRegex(
                ReferenceViolation,
                "expected SHA-256",
            ):
                store.put(
                    source,
                    kind="openubmc-hpm",
                    provenance="build",
                    retention_hint="run-lifetime",
                    target="192.0.2.4",
                    run_id="run-racing-artifact",
                    created_by_effect="effect-racing-artifact",
                    expected_sha256=expected_digest,
                )

    def test_external_registration_is_idempotent_but_resolution_never_auto_registers(self) -> None:
        for persistent in (False, True):
            with self.subTest(persistent=persistent), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "external.bin"
                source.write_bytes(b"external artifact")
                now = [100.0]
                repository = (
                    SQLiteArtifactRepository(root / "artifacts.sqlite3")
                    if persistent
                    else None
                )
                store = LocalArtifactStore(
                    content_root=root / "content",
                    repository=repository,
                    clock=lambda: now[0],
                )
                reference = ArtifactRef(
                    handle=str(source),
                    digest=hashlib.sha256(source.read_bytes()).hexdigest(),
                    kind="external-result",
                    size=source.stat().st_size,
                    provenance="trusted-producer",
                    retention_hint="temporary",
                    target="192.0.2.5",
                    run_id="run-external",
                )

                with self.assertRaisesRegex(ReferenceViolation, "unavailable"):
                    store.resolve(reference)

                first = store.register(reference, created_by_effect="effect-external")
                now[0] = 200.0
                second = store.register(reference, created_by_effect="effect-external")

                self.assertEqual(first, second)
                self.assertEqual(store.resolve(reference), source)

    def test_external_registration_cannot_rebind_one_local_handle_to_another_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "external.bin"
            source.write_bytes(b"scope-bound artifact")
            store = LocalArtifactStore(content_root=root / "content")
            reference = ArtifactRef(
                handle=str(source),
                digest=hashlib.sha256(source.read_bytes()).hexdigest(),
                kind="external-result",
                size=source.stat().st_size,
                provenance="trusted-producer",
                retention_hint="run-lifetime",
                target="192.0.2.6",
                run_id="run-owner",
            )
            store.register(reference, created_by_effect="effect-owner")
            forged = ArtifactRef.from_public_dict(
                {
                    **reference.to_public_dict(),
                    "target": "192.0.2.7",
                    "run_id": "run-intruder",
                }
            )

            with self.assertRaisesRegex(ReferenceViolation, "scope"):
                store.register(forged, created_by_effect="effect-intruder")
            with self.assertRaisesRegex(ReferenceViolation, "unavailable"):
                store.resolve(forged)

    def test_managed_artifact_survives_store_restart_and_is_content_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle.tar.gz"
            source.write_bytes(b"durable bundle")
            database = root / "artifacts.sqlite3"
            content = root / "content"

            first = LocalArtifactStore(
                content_root=content,
                repository=SQLiteArtifactRepository(database),
            )
            reference = first.put(
                source,
                kind="openubmc-log-bundle",
                provenance="log-bundle-collect",
                retention_hint="run-lifetime",
                target="192.0.2.10",
                run_id="run-artifact-1",
                created_by_effect="effect-collect-1",
            )

            self.assertEqual(
                reference.handle,
                f"artifact://sha256/{reference.digest}",
            )
            self.assertNotEqual(first.resolve(reference), source)

            reopened = LocalArtifactStore(
                content_root=content,
                repository=SQLiteArtifactRepository(database),
            )
            self.assertEqual(reopened.resolve(reference).read_bytes(), b"durable bundle")
            self.assertEqual(
                reopened.find(
                    kind="openubmc-log-bundle",
                    target="192.0.2.10",
                    run_id="run-artifact-1",
                    created_by_effect="effect-collect-1",
                ),
                reference,
            )

    def test_managed_artifact_fails_closed_for_scope_tampering_and_deleted_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle.tar.gz"
            source.write_bytes(b"trusted bundle")
            store = LocalArtifactStore(content_root=root / "content")
            reference = store.put(
                source,
                kind="openubmc-log-bundle",
                provenance="log-bundle-collect",
                retention_hint="temporary",
                target="192.0.2.20",
                run_id="run-artifact-2",
                created_by_effect="effect-collect-2",
            )

            with self.assertRaisesRegex(ReferenceViolation, "target"):
                store.resolve(reference, expected_target="192.0.2.21")
            with self.assertRaisesRegex(ReferenceViolation, "run_id"):
                store.resolve(reference, expected_run_id="another-run")
            with self.assertRaisesRegex(ReferenceViolation, "kind"):
                store.resolve(reference, expected_kinds=("openubmc-log-index",))

            managed_path = store.resolve(reference)
            managed_path.write_bytes(b"tampered bundle")
            with self.assertRaisesRegex(ReferenceViolation, "digest"):
                store.resolve(reference)

    def test_retention_release_and_gc_remove_only_expired_unprotected_records(self) -> None:
        now = [100.0]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = LocalArtifactStore(
                content_root=root / "content",
                clock=lambda: now[0],
                temporary_retention_seconds=10,
            )
            source = root / "shared.json"
            source.write_text('{"password":"secret"}', encoding="utf-8")
            temporary = store.put(
                source,
                kind="temporary-result",
                provenance="test",
                retention_hint="temporary",
                target="192.0.2.30",
                run_id="run-temporary",
                created_by_effect="effect-temporary",
            )
            run_lifetime = store.put(
                source,
                kind="run-result",
                provenance="test",
                retention_hint="run-lifetime",
                target="192.0.2.30",
                run_id="run-retained",
                created_by_effect="effect-run",
            )
            audit = store.put(
                source,
                kind="audit-result",
                provenance="test",
                retention_hint="audit",
                target="192.0.2.30",
                run_id="run-audit",
                created_by_effect="effect-audit",
            )

            now[0] = 111.0
            first_gc = store.garbage_collect()
            self.assertEqual(first_gc["deleted_records"], 1)
            with self.assertRaisesRegex(ReferenceViolation, "unavailable"):
                store.resolve(temporary)
            self.assertTrue(store.resolve(run_lifetime).is_file())
            self.assertTrue(store.resolve(audit).is_file())

            self.assertEqual(store.release_run("run-retained"), 1)
            second_gc = store.garbage_collect()
            self.assertEqual(second_gc["deleted_records"], 1)
            with self.assertRaisesRegex(ReferenceViolation, "unavailable"):
                store.resolve(run_lifetime)
            self.assertTrue(store.resolve(audit).is_file())

    def test_redaction_derives_new_content_and_cannot_relabel_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "query.json"
            source.write_text(
                json.dumps({"password": "secret", "message": "failed"}),
                encoding="utf-8",
            )
            store = LocalArtifactStore(content_root=root / "content")
            reference = store.put(
                source,
                kind="openubmc-log-query-raw",
                provenance="log-bundle-query",
                retention_hint="temporary",
                target="192.0.2.40",
                run_id="run-redaction",
                created_by_effect="effect-query",
            )

            with self.assertRaisesRegex(ReferenceViolation, "redacted"):
                store.resolve(reference, require_redacted=True)

            redacted = store.redact(
                reference,
                kind="openubmc-log-query",
                provenance="log-bundle-query-redaction",
                created_by_effect="effect-query-redact",
            )

            self.assertNotEqual(redacted.digest, reference.digest)
            self.assertIn(b"<redacted>", store.resolve(redacted, require_redacted=True).read_bytes())
            self.assertIn(b"secret", store.resolve(reference).read_bytes())

    def test_redaction_without_sensitive_matches_still_derives_new_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "clean.json"
            source.write_text('{"message":"clean"}', encoding="utf-8")
            store = LocalArtifactStore(content_root=root / "content")
            reference = store.put(
                source,
                kind="clean-raw",
                provenance="test",
                retention_hint="temporary",
                target="192.0.2.9",
                run_id="run-clean-redaction",
                created_by_effect="effect-clean-raw",
            )

            redacted = store.redact(
                reference,
                kind="clean-redacted",
                provenance="test-redaction",
                created_by_effect="effect-clean-redacted",
            )

            self.assertNotEqual(redacted.digest, reference.digest)
            self.assertNotEqual(
                store.resolve(redacted, require_redacted=True).read_bytes(),
                store.resolve(reference).read_bytes(),
            )

    def test_redaction_boundary_never_certifies_arbitrary_derivative_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "query.json"
            source.write_text('{"password":"secret"}', encoding="utf-8")
            derivative = root / "report.md"
            derivative.write_text("password=secret", encoding="utf-8")
            store = LocalArtifactStore(content_root=root / "content")
            raw_reference = store.put(
                source,
                kind="openubmc-log-query-raw",
                provenance="query",
                retention_hint="temporary",
                target="192.0.2.8",
                run_id="run-redaction-owner",
                created_by_effect="effect-query-raw",
            )
            store.redact(
                raw_reference,
                kind="openubmc-log-query",
                provenance="query-redaction",
                created_by_effect="effect-query-redacted",
            )
            derivative_reference = store.put(
                derivative,
                kind="openubmc-log-report-raw",
                provenance="report",
                retention_hint="temporary",
                target="192.0.2.8",
                run_id="run-redaction-owner",
                created_by_effect="effect-report-raw",
            )

            with self.assertRaisesRegex(ReferenceViolation, "redacted"):
                store.resolve(derivative_reference, require_redacted=True)


if __name__ == "__main__":
    unittest.main()
