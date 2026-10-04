# Native Windows knowledge MCP test fixtures

Status: Proposed. Tracks #298, part of #293.

The knowledge MCP uses the same private local storage contract on Windows in source tests and in the installed plugin. A source test must create its configuration, token-cache and process-lifecycle fixtures beneath a current-user-owned private directory. It must not rely on the public Windows Temp ACL, and it must not weaken the production ACL validator to make a fixture pass. The test suite must exercise the real Windows helper and keep a deliberately permissive ACL rejection case.

Tests of POSIX signal delivery and `/proc` process discovery run only on POSIX. Equivalent Windows lifecycle behavior is checked through Windows process identity, parent loss, stdin close and explicit task closeout. Tests retain the same observable MCP `initialize`, `tools/list` and task lifecycle outcomes on each supported host.

The public test seams are the source `npm test` command on native Windows and Linux, the knowledge MCP stdio `initialize`/`tools/list`/status calls, and the local configuration and token-store APIs. The Windows suite must finish without a private-store fixture error; any remaining failure must name the actual product behavior rather than a host-specific test path.
