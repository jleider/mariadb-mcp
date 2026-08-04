# Ensure `src/` is importable so that modules using flat imports (e.g. `from config
# import ...` in server.py) resolve when the suite is run from the repo root via
# `python -m unittest`. Without this, importing `src.server` fails on `config`.
import os
import sys

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
