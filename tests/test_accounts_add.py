"""User creation tests with a fake panel; no live credentials or network."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/fastpanel/skills/fastpanel-accounts/scripts/fastpanel_accounts.py"
spec = importlib.util.spec_from_file_location("accounts_tool", SCRIPT)
accounts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accounts)
fp = accounts.fp


class FakePanel:
    base = "https://panel.example.com"
    cfg = {"name": "work"}

    def __init__(self, directory):
        self.directory = directory
        self.reads = [[], [{"id": 42, "username": "shop_user"}]]
        self.calls = []
        self.response = (201, {"data": {"id": 42}})
        self.error = None
        self.saved = None

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            rows = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
            return 200, {"data": rows}
        files = list(self.directory.glob("users/work/*.json"))
        if not files:
            raise AssertionError("credentials must be saved before POST")
        self.saved = json.loads(files[-1].read_text())
        if self.error:
            raise self.error
        return self.response


class AddUserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.panel = FakePanel(self.directory)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(fp, "CONFIG_DIR", self.temp.name))
        self.stack.enter_context(mock.patch.object(fp, "SECRETS", []))
        self.stack.enter_context(mock.patch.object(fp, "LOGINS", []))
        self.stack.enter_context(mock.patch.object(fp, "panel_for", return_value=self.panel))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(fp._Scrubbed(self.output)))
        self.stack.enter_context(contextlib.redirect_stderr(fp._Scrubbed(self.output)))

    def run_add(self, **kwargs):
        username = kwargs.pop("username", "shop_user")
        request_id = "a" * 32
        request_dir = self.directory / "user-requests"
        request_dir.mkdir(mode=0o700, exist_ok=True)
        request_path = request_dir / (request_id + ".json")
        request_path.write_text(json.dumps({"username": username}))
        request_path.chmod(0o600)
        values = dict(request=request_id, quota=0, wait=0, account="work")
        values.update(kwargs)
        return accounts.cmd_add(fp.argparse.Namespace(**values))

    def files(self):
        return list(self.directory.glob("users/work/*.json"))

    def test_creates_user_with_ui_payload_and_private_credentials(self):
        self.assertEqual(self.run_add(quota=17), 0)
        post = next(c for c in self.panel.calls if c[0] == "POST")
        body = post[2]
        self.assertEqual(post[:2], ("POST", "/users"))
        self.assertEqual(set(body), {"username", "password", "roles", "quota"})
        self.assertEqual(body["username"], "shop_user")
        self.assertEqual(body["roles"], "ROLE_USER")
        self.assertEqual(body["quota"], 17)
        self.assertEqual(len(body["password"]), 24)
        self.assertEqual(self.panel.saved, dict(url=self.panel.base,
                                              username=body["username"],
                                              password=body["password"], request_id="a" * 32))
        path = self.files()[0]
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertNotIn("shop_user", path.name)
        self.assertNotIn("shop_user", self.output.getvalue())
        self.assertNotIn(body["password"], self.output.getvalue())
        self.assertEqual(json.loads(self.output.getvalue()), {"status": "created", "user_id": 42})

    def test_existing_login_is_idempotent_case_insensitively(self):
        self.panel.reads = [[{"id": 42, "username": "SHOP_USER", "quota": 5}]]
        self.assertEqual(self.run_add(quota=17), 0)
        self.assertFalse(any(c[0] == "POST" for c in self.panel.calls))
        self.assertEqual(self.files(), [])
        self.assertEqual(json.loads(self.output.getvalue()), {"status": "exists", "user_id": 42})

    def test_waits_for_background_creation_without_reposting(self):
        self.panel.response = (202, {})
        self.panel.reads = [[], [], [{"id": 42, "username": "shop_user", "action": "create"}],
                            [{"id": 42, "username": "shop_user", "action": None}]]
        with mock.patch.object(accounts.time, "sleep"):
            self.assertEqual(self.run_add(wait=60), 0)
        self.assertEqual(sum(c[0] == "POST" for c in self.panel.calls), 1)

    def test_timeout_keeps_credentials_and_does_not_claim_success(self):
        self.panel.reads = [[]]
        with self.assertRaises(SystemExit) as exc:
            self.run_add()
        self.assertEqual(exc.exception.code, 1)
        self.assertEqual(len(self.files()), 1)
        self.assertNotIn('"status":"created"', self.output.getvalue())
        self.assertEqual(sum(c[0] == "POST" for c in self.panel.calls), 1)

    def test_rejection_does_not_claim_creation(self):
        self.panel.response = (403, {"message": "permission denied for shop_user"})
        with self.assertRaises(SystemExit) as exc:
            self.run_add()
        self.assertEqual(exc.exception.code, 1)
        self.assertNotIn('"status":"created"', self.output.getvalue())
        self.assertNotIn("shop_user", self.output.getvalue())
        self.assertEqual(len(self.files()), 1)

    def test_password_echoed_in_error_is_scrubbed(self):
        password = "SyntheticTestPassword1234"
        self.panel.response = (400, {"errors": {"password": password}})
        with mock.patch.object(fp, "gen_password", return_value=password):
            with self.assertRaises(SystemExit):
                self.run_add()
        self.assertNotIn(password, self.output.getvalue())
        self.assertEqual(json.loads(self.output.getvalue()), {"status": "error", "code": "create_rejected"})

    def test_racing_duplicate_does_not_replace_existing_password(self):
        self.panel.response = (400, {"errors": {"username": "already exists"}})
        with self.assertRaises(SystemExit) as exc:
            self.run_add()
        self.assertEqual(exc.exception.code, 3)
        self.assertEqual(sum(c[0] == "POST" for c in self.panel.calls), 1)
        self.assertFalse(any(c[0] == "PUT" for c in self.panel.calls))

    def test_lost_response_retains_credentials(self):
        self.panel.error = OSError("synthetic connection lost")
        with self.assertRaises(OSError):
            self.run_add()
        self.assertEqual(len(self.files()), 1)
        self.assertNotIn('"status":"created"', self.output.getvalue())

    def test_cannot_post_if_credentials_cannot_be_saved(self):
        with mock.patch.object(accounts, "save_user_credentials", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_add()
        self.assertFalse(any(c[0] == "POST" for c in self.panel.calls))

    def test_new_attempt_does_not_overwrite_old_credentials(self):
        first = accounts.save_user_credentials(self.panel, "shop_user", "synthetic-first")
        second = accounts.save_user_credentials(self.panel, "shop_user", "synthetic-second")
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(Path(first).read_text())["password"], "synthetic-first")

    def test_admin_and_empty_logins_do_not_create_users(self):
        for username in ["fastpanel", "FASTPANEL", "", "has space"]:
            with self.subTest(username=username), self.assertRaises(SystemExit):
                self.run_add(username=username)
        self.assertEqual(self.panel.calls, [])

    def test_cli_rejects_invalid_quota_and_wait_before_auth(self):
        for flag, value in [("--quota", "-1"), ("--wait", "-1"), ("--quota", "abc")]:
            with self.subTest(flag=flag, value=value), mock.patch.object(
                    sys, "argv", [str(SCRIPT), "add", "--request", "a" * 32, flag, value]):
                with self.assertRaises(SystemExit) as exc:
                    accounts.main()
                self.assertEqual(exc.exception.code, 2)
        self.assertEqual(self.panel.calls, [])


if __name__ == "__main__":
    unittest.main()
