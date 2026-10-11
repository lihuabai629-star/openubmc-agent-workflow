# Windows CI Python 3.12 toolchain

The Runtime support contract remains CPython 3.12 on Windows x64. Linux CI stays
on 3.12.13, and `requirements-ci.lock` retains all existing versions and hashes.

Python.org stopped Windows binary installers after 3.12.10. Later 3.12 releases
contain security fixes, so selecting 3.12.10 just to satisfy `setup-python` would
discard those fixes. The Windows validation and release-qualification jobs use
the exact Astral `python-build-standalone` 3.12.15 build in
`scripts/windows-python.lock.json`. This is the CPython distribution used by uv;
it is not a Python.org Windows installer. The product launcher and its 3.12
version check are unchanged. This is CI toolchain acquisition, not a new bundled
product interpreter or a system-wide Python installation.

`scripts/setup_windows_python.ps1` requires a fresh output directory, verifies
the archive SHA-256 before extraction, and checks the actual CPython version,
Windows platform and 64-bit process. It then exports the isolated interpreter
through GitHub's job-local environment files and the launcher's existing
`OPENUBMC_PLUGIN_WINDOWS_PYTHON` override. It does not edit the registry or relax
TLS, dependency hashes, package verification or file-access rules. Failed
downloads or verification never fall back to an older interpreter.

The lock records the supplier release, build commit, exact asset digest and
upstream CPython source digest. These were checked against the release asset
metadata, `SHA256SUMS`, and Python.org. Supplier provenance metadata is available;
digest checking is not a claim of independently verifying its attestation.

For an offline local check, pass the already verified archive explicitly:

```powershell
./scripts/setup_windows_python.ps1 -OutputDirectory <fresh-directory> -Archive <locked-archive>
```

Use `python -m pip`, since this portable distribution does not promise a
`pip.exe` wrapper. Native Windows qualification must cover real imports,
process lifecycle, private storage and local SSH fixtures; metadata or
setup-mode MCP success alone is not Runtime qualification. Python temporary
directories enforce owner access, so a restricted sandbox token can fail these
tests even inside a writable workspace. Run them with the ordinary user's token
in a fresh isolated test profile without changing existing file permissions.

Sources: [Python 3.12.15](https://www.python.org/downloads/release/python-31215/),
[Astral build](https://github.com/astral-sh/python-build-standalone/releases/tag/20261003),
[uv's CPython distributions](https://docs.astral.sh/uv/concepts/python-versions/#cpython-distributions),
[portable Windows pip behavior](https://gregoryszorc.com/docs/python-build-standalone/main/quirks.html#no-pip-exe-on-windows).
