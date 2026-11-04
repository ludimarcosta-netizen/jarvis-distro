"""Run the local Hermes checkout with its existing Python 3.11 venv."""

import runpy
import os
import sys
from pathlib import Path


root = Path(__file__).resolve().parent.parent / "hermes-agent"
os.environ.setdefault("HERMES_HOME", str(root.parent))
os.chdir(root)
sys.path.insert(0, str(root))

# The machine's managed 3.14 dependency generation cannot run under this 3.11
# interpreter. The checkout has a complete 3.11 venv, so keep its own deps.
import pm.environments  # noqa: E402
import hermes_cli.venv_sync  # noqa: E402

pm.environments.activate_dependencies = lambda _root: None
hermes_cli.venv_sync.prepare_launch = lambda _root, _args: None
sys.argv = [str(root / "cli.py"), *sys.argv[1:]]
runpy.run_path(str(root / "cli.py"), run_name="__main__")
