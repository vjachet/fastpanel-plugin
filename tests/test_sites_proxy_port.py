"""proxy-port with a fake panel: check, switch, repeat, rollback and each refusal."""

import contextlib
import copy
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/fastpanel/skills/fastpanel-sites/scripts/fastpanel_sites.py"
spec = importlib.util.spec_from_file_location("sites_tool", SCRIPT)
sites = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sites)
fp = sites.fp


class FakePanel:
    cfg = {"name": "work", "url": "https://panel.example.com", "login": "x"}

    def __init__(self):
        self.site = {"id": 190, "domain": "os.example.com", "index_dir": "/var/www/u/os",
                     "status": "active", "action": None}
        self.backend = {"type": "reverse_proxy", "handler": None, "action": None,
                        "upstreams": [{"id": 1, "type": "host", "address": "http://127.0.0.1:4641"},
                                      {"id": 2, "type": "host", "address": "http://127.0.0.1:9000"}]}
        self.site_status = 200
        self.put_status = 200
        self.apply = True
        self.puts = []

    def call(self, method, path, body=None):
        if method == "GET" and path == "/sites/190":
            return self.site_status, {"data": self.site} if self.site_status == 200 else {}
        if method == "GET" and path == "/sites/backend/190":
            return 200, {"data": copy.deepcopy(self.backend)}
        if method == "GET" and path.startswith("/sites/"):
            return 404, {}
        if method == "PUT" and path == "/sites/backend/190":
            self.puts.append(body)
            if self.put_status < 300 and self.apply:
                self.backend["upstreams"] = copy.deepcopy(body["upstreams"])
            return self.put_status, {}
        raise AssertionError((method, path))


class ProxyPortTests(unittest.TestCase):
    def setUp(self):
        self.panel = FakePanel()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(fp, "panel_for", return_value=self.panel))
        self.stack.enter_context(mock.patch.object(fp, "hashed_name", return_value="acc-0000abcd"))
        self.stack.enter_context(mock.patch.object(fp, "save_diagnostic"))
        self.stack.enter_context(mock.patch.object(sites.time, "sleep"))

    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["fastpanel_sites.py", "proxy-port", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = sites.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def addresses(self):
        return [u["address"] for u in self.panel.backend["upstreams"]]

    def test_check_writes_nothing(self):
        code, out = self.run_cmd("os.example.com", "--site-id", "190", "--from-port", "4641",
                                 "--port", "4642", "--check")
        self.assertEqual(code, 0)
        self.assertIn("check:   ok", out)
        self.assertIn("acc-0000abcd", out)
        self.assertEqual(self.panel.puts, [])

    def test_switch_repeat_rollback(self):
        args = ["os.example.com", "--site-id", "190", "--from-port", "4641", "--port", "4642"]
        code, out = self.run_cmd(*args)
        self.assertEqual(code, 0, out)
        self.assertIn("applied: yes", out)
        self.assertEqual(self.addresses(), ["http://127.0.0.1:4642", "http://127.0.0.1:9000"])
        self.assertEqual([u["id"] for u in self.panel.puts[0]["upstreams"]], [1, 2])
        code, out = self.run_cmd(*args)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(len(self.panel.puts), 1)
        code, out = self.run_cmd("os.example.com", "--site-id", "190", "--from-port", "4642",
                                 "--port", "4641")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.addresses(), ["http://127.0.0.1:4641", "http://127.0.0.1:9000"])

    def test_domain_mismatch(self):
        code, out = self.run_cmd("other.example.com", "--site-id", "190", "--from-port", "4641",
                                 "--port", "4642")
        self.assertEqual(code, sites.EXIT_DOMAIN_MISMATCH)
        self.assertIn('"code":"domain_mismatch"', out)
        self.assertEqual(self.panel.puts, [])

    def test_access_denied(self):
        self.panel.site_status = 403
        code, out = self.run_cmd("os.example.com", "--site-id", "190", "--from-port", "4641",
                                 "--port", "4642")
        self.assertEqual(code, sites.EXIT_ACCESS_DENIED)
        self.assertIn('"code":"access_denied"', out)

    def test_put_denied(self):
        self.panel.put_status = 403
        code, out = self.run_cmd("os.example.com", "--site-id", "190", "--from-port", "4641",
                                 "--port", "4642")
        self.assertEqual(code, sites.EXIT_ACCESS_DENIED)

    def test_manual_config(self):
        self.panel.site["manual_changes"] = True
        code, out = self.run_cmd("os.example.com", "--site-id", "190", "--from-port", "4641",
                                 "--port", "4642", "--check")
        self.assertEqual(code, sites.EXIT_MANUAL_CONFIG)
        self.assertIn('"code":"manual_config"', out)

    def test_two_matching_upstreams(self):
        self.panel.backend["upstreams"].append({"type": "host", "address": "http://localhost:4641"})
        code, out = self.run_cmd("os.example.com", "--site-id", "190", "--from-port", "4641",
                                 "--port", "4642", "--check")
        self.assertEqual(code, sites.EXIT_UPSTREAM_MISMATCH)

    def test_timeout_reports_state(self):
        self.panel.apply = False
        code, out = self.run_cmd("os.example.com", "--site-id", "190", "--from-port", "4641",
                                 "--port", "4642", "--wait", "0")
        self.assertEqual(code, sites.EXIT_APPLY_TIMEOUT)
        self.assertIn("not written: still on port 4641", out)
        self.assertIn('"code":"apply_timeout"', out)

    def test_unknown_account(self):
        with mock.patch.object(fp, "panel_for", side_effect=lambda a: fp.resolve_account("acc-ffffffff")), \
                mock.patch.object(fp, "load_config", return_value=[]):
            code, out = self.run_cmd("os.example.com", "--site-id", "190", "--from-port", "4641",
                                     "--port", "4642", "-A", "acc-ffffffff")
        self.assertEqual(code, fp.EXIT_ACCOUNT_NOT_FOUND)
        self.assertIn('"code":"account_not_found"', out)


if __name__ == "__main__":
    unittest.main()
