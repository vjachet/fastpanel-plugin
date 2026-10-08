"""Privacy checks use synthetic identities and local temporary files only."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from test_accounts_add import accounts, fp, SCRIPT
from test_accounts_ssh import FakeSSHPanel, PUBLIC_KEY


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(fp, "CONFIG_DIR", self.temp.name))
        self.stack.enter_context(mock.patch.object(fp, "LOGINS", []))
        self.stack.enter_context(mock.patch.object(fp, "SECRETS", []))
        self.output = io.StringIO()
        self.stream = fp._Scrubbed(self.output)
        self.stack.enter_context(contextlib.redirect_stdout(self.stream))
        self.stack.enter_context(contextlib.redirect_stderr(self.stream))

    def test_user_list_and_owner_announcement_expose_only_ids(self):
        panel = FakeSSHPanel()
        panel.users = [{"id": 93, "username": "hiddenalpha"},
                       {"id": 94, "username": "hiddenbeta"},
                       {"id": 1, "username": "fastpanel", "roles": ["ROLE_ADMIN"]}]
        self.stack.enter_context(mock.patch.object(fp, "panel_for", return_value=panel))
        accounts.cmd_users(fp.argparse.Namespace(account="work"))
        fp.announce(panel, "create site", owner_id=93)
        for name in ["hiddenalpha", "hiddenbeta", "fastpanel"]:
            self.assertNotIn(name, self.output.getvalue())
        self.assertEqual([json.loads(line) for line in self.output.getvalue().splitlines()],
                         [{"user_ids": [93, 94, 1], "owner_ids": [93, 94]},
                          {"status": "pending", "owner_id": 93}])

    def test_nested_identities_and_encoded_paths_are_scrubbed(self):
        fp.remember_identities({"owner": {"username": "hidden/alpha"},
                                "data": [{"login": "hiddenbeta"}, {"username": "Юзер"}]})
        for text in ["/var/www/hiddenbeta/data", "/sshd/hidden%2Falpha/auth_keys",
                     json.dumps({"owner": "Юзер"}), "HIDDENBETA"]:
            self.assertNotIn(text, fp.scrub(text))
        self.assertIn("***", fp.scrub("hiddenbeta"))

    def test_scrubber_covers_usernames_split_across_writes(self):
        fp.LOGINS.append("hiddenalpha")
        self.stream.write("hidden")
        self.stream.write("alpha\n")
        self.stream.flush()
        self.assertEqual(self.output.getvalue(), "***\n")

    def test_key_list_projects_safe_fields_only(self):
        panel = FakeSSHPanel()
        panel.key_reads = [[{"id": 40, "type": 1, "enabled": True, "action": None,
                            "username": "not_in_user_list", "desc": "private_personal_label",
                            "raw_key": PUBLIC_KEY + " private_personal_label",
                            "signed_key": "unused_sensitive_metadata"}]]
        self.stack.enter_context(mock.patch.object(fp, "panel_for", return_value=panel))
        accounts.cmd_ssh_keys(fp.argparse.Namespace(user=93, account="work"))
        result = self.output.getvalue()
        for value in ["not_in_user_list", "private_personal_label", "raw_key", "signed_key", PUBLIC_KEY]:
            self.assertNotIn(value, result)
        self.assertEqual(json.loads(result), {"user_id": 93, "key_ids": [40]})

    def test_unknown_identity_in_server_error_never_leaves_process(self):
        data = {"errors": {"username": "hidden_unknown already exists"}}
        print(fp.panel_error(data))
        self.assertNotIn("hidden_unknown", self.output.getvalue())
        self.assertTrue(fp.says_already_exists(data))

    def test_unexpected_exception_does_not_echo_private_values(self):
        def fail():
            raise RuntimeError("hidden_unknown")
        with self.assertRaises(SystemExit):
            fp.run(fail)
        self.assertNotIn("hidden_unknown", self.output.getvalue())
        self.assertEqual(json.loads(self.output.getvalue()), {"status": "error", "code": "operation_failed"})
        diagnostic = Path(self.temp.name) / "diagnostics/last-error.json"
        self.assertIn("RuntimeError", diagnostic.read_text())
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)

    def test_panel_call_registers_response_identities_before_return(self):
        panel = fp.Panel({"name": "work", "url": "https://panel.example.com"})
        panel.token = "synthetic"
        data = {"data": {"owner": {"id": 93, "username": "hiddenalpha"}}}
        with mock.patch.object(panel, "_request", return_value=(200, data)):
            status, response = panel.call("GET", "/sites/1")
        self.assertEqual(status, 200)
        self.assertEqual(response, data)
        print(json.dumps(response))
        self.assertNotIn("hiddenalpha", self.output.getvalue())

    def test_owner_resolution_accepts_only_numeric_ids(self):
        panel = FakeSSHPanel()
        self.assertEqual(fp.resolve_owner(panel, 93), 93)
        with self.assertRaises(fp.argparse.ArgumentTypeError):
            fp.resolve_owner(panel, "test_user")
        self.assertNotIn("test_user", self.output.getvalue())

    def test_preparation_keeps_username_in_private_file_not_output(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(fp, "CONFIG_DIR", directory), \
                    mock.patch.object(sys.stdin, "isatty", return_value=True), \
                    mock.patch.object(sys.stdout, "isatty", return_value=True), \
                    mock.patch.object(accounts.getpass, "getpass", return_value="hiddenalpha"):
                self.assertEqual(accounts.cmd_prepare_user(fp.argparse.Namespace()), 0)
            files = list((Path(directory) / "user-requests").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(files[0].read_text()), {"username": "hiddenalpha"})
            self.assertNotIn("hiddenalpha", files[0].name)
        self.assertNotIn("hiddenalpha", self.output.getvalue())
        self.assertRegex(json.loads(self.output.getvalue())["request_id"], r"^[a-f0-9]{32}$")

    def test_preparation_rejects_noninteractive_agent_calls(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch.object(accounts.getpass, "getpass") as prompt:
            with self.assertRaises(SystemExit):
                accounts.cmd_prepare_user(fp.argparse.Namespace())
            prompt.assert_not_called()

    def test_preparation_refuses_visible_input_fallback(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(sys.stdout, "isatty", return_value=True), \
                mock.patch.object(accounts.getpass, "getpass", side_effect=accounts.getpass.GetPassWarning):
            with self.assertRaises(accounts.getpass.GetPassWarning):
                accounts.cmd_prepare_user(fp.argparse.Namespace())

    def test_creation_rejects_request_accessible_to_other_users(self):
        with tempfile.TemporaryDirectory() as directory:
            request_id = "a" * 32
            request_dir = Path(directory) / "user-requests"
            request_dir.mkdir(mode=0o700)
            path = request_dir / (request_id + ".json")
            path.write_text(json.dumps({"username": "hiddenalpha"}))
            path.chmod(0o644)
            with mock.patch.object(fp, "CONFIG_DIR", directory), \
                    mock.patch.object(fp, "panel_for") as authenticate:
                with self.assertRaises(SystemExit):
                    accounts.cmd_add(fp.argparse.Namespace(request=request_id))
                authenticate.assert_not_called()
        self.assertNotIn("hiddenalpha", self.output.getvalue())

    def test_username_arguments_are_not_an_agent_interface(self):
        for argv in [["add", "hiddenalpha"], ["ssh-keys", "hiddenalpha"],
                     ["ssh-status", "hiddenalpha"], ["ssh-add", "hiddenalpha", "--key", PUBLIC_KEY]]:
            with self.subTest(command=argv[0]), mock.patch.object(sys, "argv", [str(SCRIPT)] + argv):
                with self.assertRaises(SystemExit) as exc:
                    accounts.main()
                self.assertEqual(exc.exception.code, 2)

    def test_request_rejects_path_traversal(self):
        with self.assertRaises(fp.argparse.ArgumentTypeError):
            accounts.request_username("../../private")

    def test_whoami_exposes_only_id_without_fetching_user_details(self):
        panel = FakeSSHPanel()
        with mock.patch.object(fp, "panel_for", return_value=panel), \
                mock.patch.object(fp, "own_user_id", return_value=93):
            accounts.cmd_whoami(fp.argparse.Namespace(account="work"))
        self.assertEqual(json.loads(self.output.getvalue()), {"user_id": 93})
        self.assertEqual(panel.calls, [])

    def test_account_list_and_selection_use_opaque_references(self):
        configs = [{"name": "private_alias", "url": "https://private.example.com",
                    "login": "private_login", "label": "private_label"},
                   {"name": "second", "url": "https://other.example.com"}]
        refs = {"private_alias": "acc-1234abcd", "second": "acc-5678abcd"}
        with mock.patch.object(fp, "load_config", return_value=configs), \
                mock.patch.object(fp, "hashed_name", side_effect=lambda a: refs[a["name"]]):
            self.assertEqual(fp.list_accounts(), 0)
            self.assertIs(fp.resolve_account("acc-1234abcd"), configs[0])
            self.assertIs(fp.resolve_account("private_alias"), configs[0])
        self.assertEqual(json.loads(self.output.getvalue()),
                         {"account_ids": ["acc-1234abcd", "acc-5678abcd"]})

    def test_local_account_id_does_not_echo_alias(self):
        with mock.patch.object(fp, "resolve_account", return_value={"name": "private_alias"}), \
                mock.patch.object(fp, "hashed_name", return_value="acc-1234abcd"):
            accounts.cmd_account_id(fp.argparse.Namespace(account="private_alias"))
        self.assertEqual(json.loads(self.output.getvalue()), {"account_ids": ["acc-1234abcd"]})

    def test_contract_rejects_metadata_and_untyped_values_before_output(self):
        cases = [{"username": "private"}, {"url": "https://private.example.com"},
                 {"user_id": "93"}, {"user_id": True}, {"user_id": 0},
                 {"key_ids": [False]}, {"account_ids": ["work"]},
                 {"status": "private detail"}, {"reason": "server response"},
                 {"ssh_ready": "true"}, {"request_id": "private"}]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(ValueError):
                fp.emit(result)
        self.assertEqual(self.output.getvalue(), "")

    def test_typed_contract_survives_identity_collisions(self):
        fp.LOGINS.extend(["status", "user_id", "93", "added", "acc-1234abcd"])
        result = {"status": "added", "user_id": 93, "account_ids": ["acc-1234abcd"]}
        fp.emit(result)
        self.assertEqual(json.loads(self.output.getvalue()), result)

    def test_error_details_stay_local_and_known_secrets_are_scrubbed(self):
        fp.LOGINS.append("private_login")
        fp.SECRETS.append("private_token")
        with self.assertRaises(SystemExit):
            fp.die("https://private.example.com/path private_login private_token")
        self.assertEqual(json.loads(self.output.getvalue()),
                         {"status": "error", "code": "operation_failed"})
        diagnostic = Path(self.temp.name) / "diagnostics/last-error.json"
        message = json.loads(diagnostic.read_text())["message"]
        self.assertIn("https://private.example.com/path", message)
        self.assertNotIn("private_login", message)
        self.assertNotIn("private_token", message)
        self.assertEqual(diagnostic.parent.stat().st_mode & 0o777, 0o700)

    def test_diagnostic_write_failure_does_not_leak_details(self):
        with mock.patch.object(fp.os, "open", side_effect=OSError("private_path")):
            with self.assertRaises(SystemExit):
                fp.die("private_details")
        self.assertEqual(json.loads(self.output.getvalue()),
                         {"status": "error", "code": "operation_failed"})

    def test_owner_choice_exposes_only_eligible_ids(self):
        panel = FakeSSHPanel()
        panel.users = [{"id": 93, "username": "private_login"},
                       {"id": 1, "username": "fastpanel", "roles": ["ROLE_ADMIN"]}]
        with self.assertRaises(SystemExit) as exc:
            fp.resolve_owner(panel, None)
        self.assertEqual(exc.exception.code, 4)
        self.assertEqual(json.loads(self.output.getvalue()),
                         {"status": "error", "code": "owner_required", "owner_ids": [93]})


if __name__ == "__main__":
    unittest.main()
