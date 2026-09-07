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


RUNTIME_ROOT = Path(os.environ['OPENUBMC_TEST_PLUGIN_ROOT']) / 'skills/openubmc-target-runtime' if os.environ.get('OPENUBMC_TEST_PLUGIN_ROOT') else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    ArtifactRef,
    LocalArtifactStore,
    ReferenceViolation,
    SQLiteArtifactRepository,
)


class ArtifactLifecycleTests(unittest.TestCase):

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
