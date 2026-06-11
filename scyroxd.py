#!/usr/bin/env python3
"""Compatibility shim — the battery daemon now lives in scyrox/daemon.py.

Prefer the installed `scyroxd` console script. This wrapper keeps
`python scyroxd.py` working from a checkout (the package dir is importable).
"""
from scyrox.daemon import main

if __name__ == "__main__":
    main()
