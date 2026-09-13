"""Entry point (challenge convention): produces ../output.csv.

Run:  python code/main.py
Reads dataset/, writes output.csv at the repository root, then validates it.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_full import run  # noqa: E402


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    run(os.path.join(here, "..", "dataset"), os.path.join(here, "..", "output.csv"))
