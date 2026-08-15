# MIT License — Copyright (c) 2026 John Kuok
"""Pi 5 RGB LED matrix ticker package."""

import sys as _sys

# Force UTF-8 on stdout/stderr for every process that imports this package
# (renderer, wifid, gunicorn workers). Under systemd with LC_ALL=C Python
# defaults these streams to latin-1, and any log line that touches a
# non-ASCII character — YouTube titles, station names, weather glyphs —
# raises UnicodeEncodeError and crashes that thread. `errors="replace"`
# keeps the process alive even if a stray byte slips through.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover - non-standard stream
        pass

__version__ = "0.1.0"
