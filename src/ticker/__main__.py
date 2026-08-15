# MIT License — Copyright (c) 2026 John Kuok
"""Run the hardware renderer with ``python -m ticker``."""

# Note: ticker/__init__.py forces stdout/stderr to UTF-8, so print/log lines
# with non-ASCII characters no longer crash under systemd LC_ALL=C.
from ticker.renderer import run

if __name__ == "__main__":
    run()
