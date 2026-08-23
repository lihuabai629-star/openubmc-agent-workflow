from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    LocalArtifactStore,
    ReferenceViolation,
    SQLiteArtifactRepository,
)


class ArtifactLifecycleTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
