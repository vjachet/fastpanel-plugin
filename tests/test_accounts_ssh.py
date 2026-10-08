"""SSH key operations with a fake panel and synthetic public key material."""

import base64
import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

from test_accounts_add import accounts, fp, SCRIPT


def ssh_string(value):
    return struct.pack(">I", len(value)) + value


PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(
    ssh_string(b"ssh-ed25519") + ssh_string(b"\x11" * 32)).decode("ascii")


class FakeSSHPanel:
    base = "https://panel.example.com"
    cfg = {"name": "work"}

    def __init__(self):
        self.users = [{"id": 93, "username": "test_user", "enabled": True, "ssh_access": True}]
        self.key_reads = [[], [{"id": 40, "raw_key": PUBLIC_KEY, "enabled": True, "action": None}]]
        self.calls = []
        self.post_response = (200, {"data": {"id": 40}})
        self.get_status = 200
        self.error = None
        self.disable_ssh_on_post = False

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/users":
            return 200, {"data": self.users}
        if path.startswith("/users/"):
            return 200, {"data": self.users[0]}
        if method == "GET":
            rows = self.key_reads.pop(0) if len(self.key_reads) > 1 else self.key_reads[0]
            return self.get_status, {"data": rows}
        if self.error:
            raise self.error
        if self.disable_ssh_on_post:
            self.users[0]["ssh_access"] = False
        return self.post_response


class SSHKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.panel = FakeSSHPanel()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(fp, "CONFIG_DIR", self.temp.name))
        self.stack.enter_context(mock.patch.object(fp, "LOGINS", []))
        self.stack.enter_context(mock.patch.object(fp, "SECRETS", []))
        self.panel_for = self.stack.enter_context(mock.patch.object(fp, "panel_for", return_value=self.panel))
        self.stack.enter_context(mock.patch.object(fp, "load_config", return_value=[
            {"name": "target", "url": self.panel.base, "login": "test_user"}]))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(fp._Scrubbed(self.output)))
        self.stack.enter_context(contextlib.redirect_stderr(fp._Scrubbed(self.output)))

    def run_add(self, **kwargs):
        values = dict(user="93", key=PUBLIC_KEY + " laptop", key_file=None,
                      desc=None, wait=0, account="work")
        values.update(kwargs)
        return accounts.cmd_ssh_add(fp.argparse.Namespace(**values))

    def posts(self):
        return [c for c in self.panel.calls if c[0] == "POST"]

    def test_posts_exact_ui_payload_and_verifies_key(self):
        self.assertEqual(self.run_add(), 0)
        self.assertEqual(self.posts(), [("POST", "/sshd/test_user/auth_key",
                         {"type": 1, "raw_key": PUBLIC_KEY + " laptop", "desc": "laptop"})])
        self.assertEqual(sum(c[1] == "/sshd/test_user/auth_keys" for c in self.panel.calls), 2)
        self.assertEqual(json.loads(self.output.getvalue()), {"status": "added", "user_id": 93})

    def test_comment_does_not_duplicate_existing_key(self):
        self.panel.key_reads = [[{"id": 40, "raw_key": PUBLIC_KEY + " old comment", "enabled": True}]]
        self.assertEqual(self.run_add(desc="new description"), 0)
        self.assertEqual(self.posts(), [])
        self.assertEqual(json.loads(self.output.getvalue()), {"status": "exists", "user_id": 93})

    def test_rejects_login_and_configured_account_alias(self):
        for user in ["test_user", "target"]:
            self.panel.calls.clear()
            self.panel.key_reads = [[{"raw_key": PUBLIC_KEY, "enabled": True}]]
            with self.assertRaises(fp.argparse.ArgumentTypeError):
                self.run_add(user=user)
            self.assertEqual(self.panel.calls, [])

    def test_key_file_and_description_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.pub"
            path.write_text(PUBLIC_KEY + " original\n")
            self.assertEqual(self.run_add(key=None, key_file=str(path), desc="workstation"), 0)
        self.assertEqual(self.posts()[0][2]["desc"], "workstation")
        self.assertEqual(self.posts()[0][2]["raw_key"], PUBLIC_KEY + " original")

    def test_empty_description_is_not_replaced_by_comment(self):
        self.assertEqual(self.run_add(desc=""), 0)
        self.assertEqual(self.posts()[0][2]["desc"], "")

    def test_private_multiline_and_invalid_public_keys_fail_before_auth(self):
        invalid = ["-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n-----END OPENSSH PRIVATE KEY-----",
                   PUBLIC_KEY + "\n" + PUBLIC_KEY, "ssh-ed25519 !!!", "ssh-rsa " + PUBLIC_KEY.split()[1],
                   'command="echo" ' + PUBLIC_KEY, "", "ssh-ed25519 AAAA"]
        for key in invalid:
            with self.subTest(key=key), self.assertRaises(SystemExit):
                self.run_add(key=key)
        self.panel_for.assert_not_called()
        self.assertEqual(self.posts(), [])

    def test_unknown_user_does_not_post(self):
        with self.assertRaises(SystemExit):
            self.run_add(user="999")
        self.assertEqual(self.posts(), [])

    def test_account_name_cannot_select_a_user(self):
        with mock.patch.object(fp, "load_config", return_value=[
                {"name": "other", "url": "https://other.example.com", "login": "test_user"}]):
            with self.assertRaises(fp.argparse.ArgumentTypeError):
                self.run_add(user="other")
        self.assertEqual(self.panel.calls, [])

    def test_login_is_encoded_as_one_url_segment(self):
        self.panel.users = [{"id": 93, "username": "user/name", "enabled": True, "ssh_access": True}]
        self.assertEqual(self.run_add(), 0)
        self.assertEqual(self.posts()[0][1], "/sshd/user%2Fname/auth_key")

    def test_failed_or_unexpected_key_list_does_not_post(self):
        for status, records in [(403, []), (200, {"unexpected": "shape"})]:
            self.panel.get_status = status
            self.panel.key_reads = [records]
            with self.subTest(status=status), self.assertRaises(SystemExit):
                self.run_add()
        self.assertEqual(self.posts(), [])

    def test_waits_for_enabled_key_without_pending_action(self):
        self.panel.key_reads = [[], [{"raw_key": PUBLIC_KEY, "enabled": False}],
                                [{"raw_key": PUBLIC_KEY, "enabled": True, "action": "add"}],
                                [{"raw_key": PUBLIC_KEY, "enabled": True, "action": None}]]
        with mock.patch.object(accounts.time, "sleep"):
            self.assertEqual(self.run_add(wait=60), 0)
        self.assertEqual(len(self.posts()), 1)

    def test_rejection_timeout_and_lost_response_do_not_claim_success(self):
        for failure in ["reject", "timeout", "lost"]:
            self.panel = FakeSSHPanel()
            self.panel_for.return_value = self.panel
            self.output.seek(0)
            self.output.truncate()
            if failure == "reject":
                self.panel.post_response = (403, {"message": "forbidden"})
            elif failure == "timeout":
                self.panel.key_reads = [[]]
            else:
                self.panel.error = OSError("synthetic connection lost")
            with self.subTest(failure=failure), self.assertRaises((SystemExit, OSError)):
                self.run_add()
            self.assertEqual(len(self.posts()), 1)
            self.assertNotIn('"status":"added"', self.output.getvalue())

    def test_cli_requires_exactly_one_public_key_source(self):
        cases = [["ssh-add", "93"], ["ssh-add", "93", "--key", PUBLIC_KEY, "--key-file", "key.pub"],
                 ["ssh-add", "93", "--key", PUBLIC_KEY, "--wait", "-1"]]
        for argv in cases:
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", [str(SCRIPT)] + argv):
                with self.assertRaises(SystemExit) as exc:
                    accounts.main()
                self.assertEqual(exc.exception.code, 2)
        self.panel_for.assert_not_called()

    def test_disabled_user_or_ssh_access_prevents_post(self):
        for field, value in [("enabled", False), ("ssh_access", False), ("ssh_access", None)]:
            self.panel.users[0].update(enabled=True, ssh_access=True)
            self.panel.users[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SystemExit):
                self.run_add()
        self.assertEqual(self.posts(), [])

    def test_existing_disabled_or_pending_key_is_not_reported_as_success(self):
        for fields in [{"enabled": False}, {"enabled": True, "action": "add"}]:
            self.panel.key_reads = [[dict(raw_key=PUBLIC_KEY, **fields)]]
            with self.subTest(fields=fields), self.assertRaises(SystemExit):
                self.run_add()
        self.assertEqual(self.posts(), [])
        self.assertNotIn('"status":"exists"', self.output.getvalue())

    def test_ssh_access_is_rechecked_after_post(self):
        self.panel.disable_ssh_on_post = True
        with self.assertRaises(SystemExit):
            self.run_add()
        self.assertEqual(len(self.posts()), 1)
        self.assertNotIn('"status":"added"', self.output.getvalue())

    def test_ssh_status_reports_panel_readiness_without_mutations(self):
        self.panel.key_reads = [[{"raw_key": PUBLIC_KEY, "enabled": True, "type": 1}]]
        args = fp.argparse.Namespace(user="93", account="work")
        self.assertEqual(accounts.cmd_ssh_status(args), 0)
        self.assertEqual(json.loads(self.output.getvalue()), {"user_id": 93, "ssh_ready": True})
        self.assertEqual(self.posts(), [])

    def test_ssh_status_fails_without_active_key_or_access(self):
        args = fp.argparse.Namespace(user="93", account="work")
        self.panel.key_reads = [[]]
        self.assertEqual(accounts.cmd_ssh_status(args), 1)
        self.panel.key_reads = [[{"raw_key": PUBLIC_KEY, "enabled": True, "type": 1}]]
        self.panel.users[0]["ssh_access"] = False
        self.assertEqual(accounts.cmd_ssh_status(args), 1)
        self.assertEqual(self.posts(), [])


if __name__ == "__main__":
    unittest.main()
