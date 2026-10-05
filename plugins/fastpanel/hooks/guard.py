#!/usr/bin/env python3
"""PreToolUse guard: the agent never opens the panel credentials or the token.

The fastpanel scripts read ~/.config/fastpanel (login, password, database
passwords) and ~/.cache/fastpanel (token) themselves and scrub them from their
output. This hook closes the other door: a tool call that reaches into those
places directly is denied before it runs.

File tools are checked by resolved path. A shell command is only text, so it is
matched by pattern — that stops the plain cases, not a determined workaround.
"""

import json
import os
import re
import sys

HOME = os.path.expanduser("~")


def protected_dirs():
    dirs = [os.path.join(HOME, ".config", "fastpanel"), os.path.join(HOME, ".cache", "fastpanel")]
    if os.environ.get("FASTPANEL_CONFIG_DIR"):
        dirs.append(os.environ["FASTPANEL_CONFIG_DIR"])
    return [os.path.realpath(os.path.expanduser(d)) for d in dirs]


def protected_files():
    path = os.environ.get("FASTPANEL_CONFIG")
    return [os.path.realpath(os.path.expanduser(path))] if path else []


def inside(path, parent):
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def path_hit(raw, cwd, searches=False):
    """True if the path is a protected one; for a search, also if it contains one."""
    if not raw:
        raw = cwd if searches else ""
    if not raw:
        return False
    path = os.path.expanduser(str(raw))
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    path = os.path.realpath(path)
    targets = protected_dirs() + protected_files()
    if any(inside(path, t) for t in targets):
        return True
    return searches and any(inside(t, path) for t in targets)


# What a shell command must not mention: the two directories however the home
# is spelled, the files in them, and the variables that point there.
SHELL_PATTERNS = [
    r"\.config/+fastpanel",
    r"\.cache/+fastpanel",
    r"fastpanel/+(config\.json|databases|token-)",
    r"\$\{?FASTPANEL_CONFIG",
    r"\$\{?XDG_(CONFIG|CACHE)_HOME\}?/+fastpanel",
    # the library holds the credentials in memory: only the plugin's own scripts load it
    r"(import|from)\s+fastpanel_api|import_module\(\s*['\"]fastpanel_api|__import__\(\s*['\"]fastpanel_api",
]


def shell_hit(command):
    text = str(command or "")
    if any(re.search(p, text) for p in SHELL_PATTERNS):
        return True
    for target in protected_dirs() + protected_files():
        # a custom location has no fixed spelling to match — its own path is the only handle
        if target and target in text:
            return True
    return False


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = event.get("tool_name") or ""
    args = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()

    if tool == "Bash":
        hit = shell_hit(args.get("command"))
    elif tool in ("Grep", "Glob"):
        hit = path_hit(args.get("path"), cwd, searches=True) or shell_hit(args.get("pattern")
                                                                          if tool == "Glob" else "")
    else:
        hit = path_hit(args.get("file_path") or args.get("notebook_path"), cwd)
    if not hit:
        return 0

    reason = (
        "fastpanel: the panel credentials (~/.config/fastpanel) and the token cache "
        "(~/.cache/fastpanel) are off limits to the agent. Use the fastpanel scripts — they "
        "read these files themselves. If the config needs a change, tell the user what to "
        "change; they edit it in their own terminal."
    )
    if tool in ("Grep", "Glob"):
        reason += " A search must not cover these directories either: narrow its path."
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
