"""Self-update: pull the latest code from GitHub and restart.

POST /admin/deploy (admin key) runs `git pull --ff-only` and `pip install -r requirements.txt`
in the project folder, replies with the old and new commit, then exits the process. The Sprite
restarts its service automatically, so the new code is running a few seconds later.
GitHub Actions calls it after every push to main (.github/workflows/deploy.yml).

If the pull or install fails, nothing restarts and the error is returned.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
RESTART_DELAY = 1.5  # seconds, so the HTTP reply is sent before the process exits


def _git(args, run, cwd):
    return run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def run_update(project_dir: Path = PROJECT_DIR, run=subprocess.run) -> dict:
    head = _git(["rev-parse", "--short", "HEAD"], run, project_dir)
    if head.returncode != 0:
        return {"ok": False, "step": "git", "error": (head.stderr or head.stdout).strip()[-1000:]}
    before = head.stdout.strip()
    pull = _git(["pull", "--ff-only"], run, project_dir)
    if pull.returncode != 0:
        return {"ok": False, "step": "git pull", "before": before, "error": (pull.stderr or pull.stdout).strip()[-1000:]}
    after = _git(["rev-parse", "--short", "HEAD"], run, project_dir).stdout.strip()
    if after == before:
        return {"ok": True, "changed": False, "before": before, "after": after}
    pip = run([sys.executable, "-m", "pip", "install", "-q", "-r", str(project_dir / "requirements.txt")],
              cwd=project_dir, capture_output=True, text=True, timeout=600)
    if pip.returncode != 0:
        return {"ok": False, "step": "pip install", "before": before, "after": after,
                "error": (pip.stderr or pip.stdout).strip()[-1000:]}
    return {"ok": True, "changed": True, "before": before, "after": after}


def restart_soon(delay: float = RESTART_DELAY) -> None:
    """Exit after the reply is sent; the Sprite service runtime starts the process again."""
    threading.Timer(delay, lambda: os._exit(0)).start()
