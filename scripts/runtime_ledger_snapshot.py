"""Replay an operator-selected Runtime ledger without opening the source database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import tempfile


def _source_bytes(path: Path) -> dict[str, bytes | None]:
    return {
        suffix: companion.read_bytes() if companion.is_file() else None
        for suffix in ("", "-wal", "-shm")
        for companion in (Path(str(path) + suffix),)
    }


@contextmanager
def stable_runtime_ledger_copy(source: Path) -> Iterator[Path]:
    """Yield a private SQLite copy after a stable, side-effect-free source read."""

    selected = source.expanduser().absolute()
    first = _source_bytes(selected)
    if first[""] is None:
        raise OSError(f"Runtime ledger is unavailable: {selected}")
    second = _source_bytes(selected)
    if second != first:
        raise ValueError("Runtime ledger changed while snapshotting")
    with tempfile.TemporaryDirectory() as raw:
        snapshot = Path(raw) / "runtime-snapshot.sqlite3"
        snapshot.write_bytes(first[""])
        wal = first["-wal"]
        if wal is not None:
            Path(str(snapshot) + "-wal").write_bytes(wal)
        yield snapshot
