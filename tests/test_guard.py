"""The PreToolUse hook, run as Claude Code runs it, against a temporary home."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "plugins/fastpanel/hooks/guard.py"


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / ".config/fastpanel"
        (self.config / "diagnostics").mkdir(parents=True)
        (self.config / "config.json").write_text("{}")
        self.diagnostic = self.config / "diagnostics/last-error.json"
        self.diagnostic.write_text("{}")

    def denied(self, tool, **tool_input):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FASTPANEL_")}
        env["HOME"] = str(self.home)
        event = {"tool_name": tool, "tool_input": tool_input, "cwd": str(self.home)}
        out = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event),
                             capture_output=True, text=True, env=env, check=True).stdout
        return bool(out) and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_diagnostic_file_is_readable_by_exact_path(self):
        self.assertFalse(self.denied("Read", file_path=str(self.diagnostic)))
        self.assertFalse(self.denied("Read", file_path="~/.config/fastpanel/diagnostics/last-error.json"))
        self.assertTrue(self.denied("Grep", path=str(self.diagnostic), pattern="x"))

    def test_everything_else_in_the_config_stays_closed(self):
        self.assertTrue(self.denied("Read", file_path=str(self.config / "config.json")))
        self.assertTrue(self.denied("Write", file_path=str(self.diagnostic), content="{}"))
        self.assertTrue(self.denied("Edit", file_path=str(self.diagnostic)))
        self.assertTrue(self.denied("Read", file_path=str(self.config / "diagnostics")))
        self.assertTrue(self.denied("Grep", path=str(self.config / "diagnostics")))
        # the shell gets no exception: it can build any path from the diagnostic's name
        for command in ["cat ~/.config/fastpanel/config.json",
                        "cat ~/.config/fastpanel/diagnostics/last-error.json",
                        "cat '%s'" % self.diagnostic,
                        'cat "$(dirname ~/.config/fastpanel/diagnostics/last-error.json)/../config.json"',
                        "D=~/.config/fastpanel/diagnostics/last-error.json; cat ${D%/*}/../config.json",
                        "cat ~/.config/fastpanel/diagnostics/*",
                        "cat ~/.cache/fastpanel/users-acc-00000001.json"]:
            with self.subTest(command=command):
                self.assertTrue(self.denied("Bash", command=command))

    def test_symlink_in_place_of_the_diagnostic_is_closed(self):
        self.diagnostic.unlink()
        self.diagnostic.symlink_to(self.config / "config.json")
        self.assertTrue(self.denied("Read", file_path=str(self.diagnostic)))


if __name__ == "__main__":
    unittest.main()
