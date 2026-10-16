"""a2t-deus fork identity, reported beside upstream ``__version__`` on /api/health and /api/status.

Lives in its own module (not beside ``__version__`` in ``__init__.py``) so upstream release bumps,
which rewrite that line, never produce an adjacent-hunk merge conflict with the fork's own bump.

``commit`` on those endpoints is upstream's (the code this process BOOTED from); ``fork_commit`` is the
checkout HEAD now — diagnostic only, it differs from ``commit`` after an update the process outlived.

Bump rule: minor for features, patch for fixes; upstream merges reset nothing.
"""

import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

__fork_version__ = "1.2.0"

_INSTALL_ROOT = Path(__file__).resolve().parent.parent
# /api/health is polled; a git spawn per probe is not free, and a diagnostic field can be this stale.
_HEAD_TTL_S = 10.0
_head_lock = threading.Lock()
_head_cache: tuple[float, Optional[str]] = (float("-inf"), None)


def fork_commit() -> Optional[str]:
    """Short sha of the checkout HEAD (cached ``_HEAD_TTL_S``); None when git can't say."""
    global _head_cache
    with _head_lock:
        stamp, sha = _head_cache
        if time.monotonic() - stamp < _HEAD_TTL_S:
            return sha
        try:
            out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_INSTALL_ROOT,
                                 capture_output=True, text=True, timeout=5, check=True)
            sha = out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            sha = None
        _head_cache = (time.monotonic(), sha)
        return sha
