"""Prepare source-only imports for a packaged Python entrypoint."""
from pathlib import Path
import runpy
import sys
import tempfile


def initialize(entrypoint):
    """Keep the returned cache scope alive for the importing Python process."""
    sys.dont_write_bytecode = True
    cache = tempfile.TemporaryDirectory(prefix='openubmc-python-cache-')
    # -B alone still reads existing caches. A fresh prefix prevents reading
    # the source tree's timestamp- or hash-based __pycache__ entries.
    sys.pycache_prefix = cache.name
    entrypoint = Path(entrypoint).resolve()
    for root in entrypoint.parents:
        if (root/'plugin-lock.json').is_file():
            # run_path compiles this source directly without importing its pyc.
            runpy.run_path(str(root/'scripts/pluginctl.py'))['verify'](root)
            break
    # An isolated interpreter omits the script directory. Add it only after
    # checking the packaged sources, so sibling imports also work under -I.
    directory = str(entrypoint.parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return cache
