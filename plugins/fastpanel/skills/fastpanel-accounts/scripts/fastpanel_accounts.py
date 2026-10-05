#!/usr/bin/env python3
"""Configured panel accounts: what is set up, and who we are on the panel.

Never prints a login or a password — not the panel ones, not any other.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "lib")
)
import fastpanel_api as fp  # noqa: E402


def cmd_list(args):
    return fp.list_accounts()


def cmd_whoami(args):
    panel = fp.panel_for(args)
    uid = fp.own_user_id(panel)
    print("account: %s" % panel.cfg["name"])
    print("panel:   %s" % panel.base)
    print("user id: %s" % uid)

    status, data = panel.call("GET", "/users")
    for user in fp.unwrap(data) if status == 200 else []:
        if user.get("id") != uid:
            continue
        owner = user.get("owner") or {}
        quota = user.get("quota") or {}
        print("home:    %s" % user.get("home_dir", "?"))
        print("roles:   %s" % ", ".join(user.get("roles") or []) or "?")
        if owner:
            print("owner:   id %s" % owner.get("id", "?"))
        if quota:
            used, limit = quota.get("used"), quota.get("limit")
            print("quota:   %s used, limit %s" % (used, limit or "none"))
    return 0


def cmd_users(args):
    panel = fp.panel_for(args)
    for line in fp.user_lines(panel):
        print(line)
    return 0


def main():
    ap = fp.argparse.ArgumentParser(description="FastPanel accounts configured for this machine")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="configured accounts: name, panel, label")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("whoami", help="log in and show who this account is on the panel")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("users", help="panel users this account sees: id and login "
                       "(all of them under the panel administrator)")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_users)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    fp.run(main)
