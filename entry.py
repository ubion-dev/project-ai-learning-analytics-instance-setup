"""Installed root-owned entrypoint; execute using the private interpreter with -I."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ala.cli import main
raise SystemExit(main())
