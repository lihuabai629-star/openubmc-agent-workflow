# Windows private-root recovery

Status: Proposed. Tracks #297 and completes the affected default-path row of #293.

An installed native Windows plugin may encounter an existing `%LOCALAPPDATA%\openubmc` directory owned by the current user but carrying an inherited, read-only ACE for another principal. In that state `prepare`, `doctor`, and both MCP preflights must return `windows_private_root_conflict` with the affected logical root and an instruction to open the local configuration page. They must not turn it into a generic dependency or handshake error.

The installed `configure` launcher must open the loopback page without requiring the conflicting root to pass private-store validation. The page displays the affected local root and a single explicit repair action. The action accepts only a named plugin-owned root and a snapshot token returned by its current status; it never accepts an arbitrary filesystem path. It removes inherited read-only ACEs only when the directory is current-user-owned, not a reparse link, and unchanged since the page preview. It preserves existing file bytes and does not change a foreign-owned path, an explicit outside ACE, an outside write ACE, an unsupported ACE, or a path that changed after preview. A repaired root must pass the existing private-path validator before normal setup resumes. Linux behavior remains unchanged.

The public test seams are the installed `pluginctl storage-status`/`repair-storage`/`prepare`/`doctor` commands, the loopback `/api/state` and `/api/plugin` recovery action, and MCP `initialize`/`tools/list` on the original default Windows path. Windows tests use owned ACL fixtures and inspect retained bytes. No real device operation is part of this recovery.
