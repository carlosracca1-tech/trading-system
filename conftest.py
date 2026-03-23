"""
conftest.py — Project root
Adds the repo root to sys.path so that `apps.*` and `packages.*` imports
work correctly when running pytest from any working directory.
"""
import sys
from pathlib import Path

# Insert project root at the front of sys.path
repo_root = Path(__file__).parent.resolve()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
