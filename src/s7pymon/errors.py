"""Error logging that survives TUI alternate-screen exit.

Accumulates errors in memory during the session, then prints them to
``stderr`` after the TUI exits (when the alternate screen is swapped out
and stderr is visible again).  Also writes each error to ``stderr``
immediately so users who redirect stderr to a file see them in real time.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime

_errors: list[tuple[str, str, str]] = []
"""Accumulated (timestamp, message, traceback) tuples."""


def log_error(msg: str) -> None:
    """Log *msg* and the current exception traceback.

    Must be called from within an ``except`` block.
    Writes to stderr immediately (flush=True) for file-redirected stderr,
    and appends to an in-memory list for the exit dump.
    """
    tb = traceback.format_exc()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n[{ts}] ERROR: {msg}", file=sys.stderr, flush=True)
    print(tb, file=sys.stderr, flush=True)

    _errors.append((ts, msg, tb))


def dump_errors() -> None:
    """Print all accumulated errors to stderr.

    Intended to be called *after* the TUI (or web server) exits, when the
    terminal is back in normal mode and stderr output is visible.
    Does nothing if there were no errors.
    """
    if not _errors:
        return

    print(file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print("ERRORS DURING SESSION", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    for ts, msg, tb in _errors:
        print(f"\n[{ts}] ERROR: {msg}", file=sys.stderr)
        print(tb, file=sys.stderr)
    print("=" * 60, file=sys.stderr)
