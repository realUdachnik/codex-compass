#!/usr/bin/env python3
"""Portable native lifecycle-hook entrypoint; see references/reminders.md."""
import sys
from pathlib import Path
base=Path(__file__).resolve().parent
# Installed bundle includes runtime next to the launcher; source checkout has it above skills/.
for candidate in [base.parent/'runtime',base.parents[2]]:
 if (candidate/'codex_compass').is_dir():sys.path.insert(0,str(candidate));break
from codex_compass.reminders import main
raise SystemExit(main())
