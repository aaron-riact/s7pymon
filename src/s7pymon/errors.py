"""Error logging that survives TUI alternate-screen exit.

Writes to both ``stderr`` (works when redirected to a file) and a
persistent append-only log at ``/tmp/s7pymon-errors.log`` so tracebacks
are never lost behind the alternate screen buffer.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime

LOG_PATH = "/tmp/s7pymon-errors.log"
_seen_first_error = False


def log_error(msg: str) -> None:
    """Log *msg* and the current exception traceback to ``stderr`` and a file.

    Must be called from within an ``except`` block.
    """
    global _seen_first_error
    tb = traceback.format_exc()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n[{ts}] ERROR: {msg}", file=sys.stderr, flush=True)
    print(tb, file=sys.stderr, flush=True)

    try:
        with open(LOG_PATH, "a") as f:
            print(f"[{ts}] ERROR: {msg}", file=f)
            print(tb, file=f)
    except OSError:
        pass

    if not _seen_first_error:
        _seen_first_error = True
        print(
            f"\n[{ts}] Full error log: {LOG_PATH}",
            file=sys.stderr, flush=True,
        )


def log_error_path() -> str:
    """Return the path to the persistent error log."""
    return LOG_PATH
