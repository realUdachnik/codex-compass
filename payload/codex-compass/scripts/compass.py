#!/usr/bin/env python3
"""Run the runtime shipped with this skill, without a source checkout or pip."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from codex_compass.cli import main

raise SystemExit(main())
