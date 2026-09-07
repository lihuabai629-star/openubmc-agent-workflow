#!/usr/bin/env python3
"""Export public checks from the same immutable source used to build a plugin."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'check_behavior.py': 'scripts/public_plugin_checks.py',
    'host/package.json': 'plugin/host/package.json',
    'host/package-lock.json': 'plugin/host/package-lock.json',
    'behavior/plugin_fixture.py': 'scripts/tests/plugin_fixture.py',
    'behavior/test_failure_gates.py': 'openubmc-build/tests/test_failure_gates.py',
    'behavior/test_openssh_timeouts.py': 'openubmc-target-runtime/tests/test_openssh_timeouts.py',
    'behavior/test_artifact_retention.py': 'openubmc-target-runtime/tests/test_artifact_retention.py',
    'behavior/test_credential_commands.py': 'openubmc-environment-setup/tests/test_credential_commands.py',
    'behavior/test_bundle_extraction.py': 'openubmc-log-analyzer/tests/test_bundle_extraction.py',
    'behavior/test_upgrade_task_states.py': 'openubmc-upgrade/tests/test_upgrade_task_states.py',
    'behavior/redfish_fixture.py': 'openubmc-upgrade/tests/redfish_fixture.py',
    'behavior/test_plugin_credentials.py': 'scripts/tests/test_plugin_credentials.py',
    'behavior/test_plugin_disable_migration.py': 'scripts/tests/test_plugin_disable_migration.py',
    'behavior/config.test.mjs': 'openubmc-kb-mcp/test/config.test.js',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-ref', default='HEAD')
    parser.add_argument('--output', type=Path, required=True, help='public repository scripts directory')
    args = parser.parse_args()
    commit = subprocess.check_output(['git', 'rev-parse', args.source_ref + '^{commit}'], cwd=ROOT, text=True).strip()
    inventory = {}
    for name, source in FILES.items():
        data = subprocess.check_output(['git', 'show', commit + ':' + source], cwd=ROOT)
        target = args.output/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        inventory[name] = hashlib.sha256(data).hexdigest()
    (args.output/'behavior/source.json').write_text(json.dumps({'source_commit': commit, 'files': inventory}, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
