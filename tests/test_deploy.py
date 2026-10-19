import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from wsdc_catalog.api import serve
from wsdc_catalog.config import Config
from wsdc_catalog.deploy import run_update


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


class TestRunUpdate(unittest.TestCase):
    """A local 'origin' repo stands in for GitHub; the clone stands in for the Sprite's copy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin, self.clone = root / "origin", root / "clone"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "requirements.txt").write_text("")
        (self.origin / "v.txt").write_text("1")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-q", "-m", "v1")
        git(root, "clone", "-q", str(self.origin), str(self.clone))
        self.pip_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_fake_pip(self, cmd, **kw):
        if cmd[0] != "git":
            self.pip_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, **kw)

    def test_no_change(self):
        out = run_update(self.clone, run=self.run_fake_pip)
        self.assertEqual((out["ok"], out["changed"]), (True, False))
        self.assertEqual(self.pip_calls, [])

    def test_pulls_new_commit_and_installs(self):
        (self.origin / "v.txt").write_text("2")
        git(self.origin, "commit", "-qam", "v2")
        out = run_update(self.clone, run=self.run_fake_pip)
        self.assertEqual((out["ok"], out["changed"]), (True, True))
        self.assertNotEqual(out["before"], out["after"])
        self.assertEqual((self.clone / "v.txt").read_text(), "2")
        self.assertEqual(len(self.pip_calls), 1)

    def test_conflict_reports_error_without_restart(self):
        (self.origin / "v.txt").write_text("2")
        git(self.origin, "commit", "-qam", "v2")
        (self.clone / "v.txt").write_text("local edit")
        git(self.clone, "commit", "-qam", "local")
        out = run_update(self.clone, run=self.run_fake_pip)
        self.assertEqual((out["ok"], out["step"]), (False, "git pull"))


class TestDeployEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.restarts = []
        self.result = {"ok": True, "changed": True, "before": "aaa", "after": "bbb"}
        cfg = Config(db_path=str(Path(self.tmp.name) / "c.sqlite3"), snapshot_dir=self.tmp.name,
                     catalog_api_key="r", admin_api_key="a")
        self.server = serve(cfg, "127.0.0.1", 0,
                            deploy_hooks=(lambda: self.result, lambda: self.restarts.append(1)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/admin/deploy"

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def post(self, key):
        req = urllib.request.Request(self.url, data=b"{}", method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_requires_admin_key(self):
        self.assertEqual(self.post("r")[0], 401)
        self.assertEqual(self.restarts, [])

    def test_restarts_only_when_code_changed(self):
        status, out = self.post("a")
        self.assertEqual((status, out["after"], self.restarts), (200, "bbb", [1]))
        self.result = {"ok": True, "changed": False, "before": "bbb", "after": "bbb"}
        self.assertEqual(self.post("a")[1]["message"], "Already up to date.")
        self.assertEqual(self.restarts, [1])

    def test_failure_does_not_restart(self):
        self.result = {"ok": False, "step": "git pull", "error": "merge conflict"}
        status, out = self.post("a")
        self.assertEqual((status, out["error"]["code"], self.restarts), (500, "deploy_failed", []))
