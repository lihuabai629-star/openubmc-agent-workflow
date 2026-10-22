from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".py"}


class SecurityScanTests(unittest.TestCase):
    def test_packaged_files_do_not_contain_literal_secrets(self) -> None:
        patterns = [
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            re.compile(
                r"(?i)\b(?:password|passwd|token|secret)\s*[:=]\s*['\"][^<.\s][^'\"]{5,}['\"]"
            ),
            re.compile(r"AKIA[0-9A-Z]{16}"),
        ]
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        findings = []
        for relative in manifest["files"]:
            path = ROOT / relative
            if path.suffix not in TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                if pattern.search(text):
                    findings.append(f"{relative}: {pattern.pattern}")
        self.assertEqual(findings, [])

    def test_path_token_constant_is_distinct_from_a_secret_assignment(self) -> None:
        pattern = re.compile(r"(?i)\b(?:password|passwd|token|secret)\s*[:=]\s*['\"][^<.\s][^'\"]{5,}['\"]")
        self.assertIsNone(pattern.search('MAPPING_TOKEN = "/mapping_config/"'))
        self.assertIsNotNone(pattern.search('token = "fixture-value"'))
        self.assertIsNotNone(pattern.search('password = "fixture-value"'))


if __name__ == "__main__":
    unittest.main()
