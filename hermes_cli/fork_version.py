"""a2t-deus fork identity, reported beside upstream ``__version__`` on /api/health and /api/status.

Lives in its own module (not beside ``__version__`` in ``__init__.py``) so upstream release bumps,
which rewrite that line, never produce an adjacent-hunk merge conflict with the fork's own bump.

Bump rule: minor for features, patch for fixes; upstream merges reset nothing.
"""

import functools
import subprocess
from pathlib import Path
from typing import Optional

__fork_version__ = "1.1.0"

_INSTALL_ROOT = Path(__file__).resolve().parent.parent


@functools.lru_cache(maxsize=1)
def fork_commit() -> Optional[str]:
    """Short sha of the running checkout, resolved once per process; None when git can't say."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_INSTALL_ROOT,
                             capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None
