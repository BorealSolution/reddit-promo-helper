#!/usr/bin/env python
"""Entry point: `python promoter.py <command>`."""
import sys
from pathlib import Path

# Windows consoles still default to cp1252, which cannot encode the box-drawing
# characters rich uses. Force UTF-8 before anything prints.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from reddit_promoter.cli import main

if __name__ == "__main__":
    sys.exit(main())
