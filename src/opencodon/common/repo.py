"""Single source of truth for the repository checkout root.

Modules must import ``REPO_ROOT`` from here instead of counting their own
``Path(__file__).parents[N]`` — per-module depth arithmetic silently breaks
every time a file moves (the recurring fallout class of the 2026-08
restructure).  Only meaningful in a source checkout, same as the per-module
anchors it replaces.
"""

from pathlib import Path

# src/opencodon/common/repo.py -> src/opencodon/common -> src/opencodon -> src -> repo
REPO_ROOT = Path(__file__).resolve().parents[3]

# The src/ directory that holds the ``opencodon`` package — for subprocess
# PYTHONPATH / sys.path wiring.
SRC_ROOT = Path(__file__).resolve().parents[2]
