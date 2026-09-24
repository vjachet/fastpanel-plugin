#!/usr/bin/env python3
"""Sites of a panel account: list them, or show one in detail."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "lib")
)
import fastpanel_api as fp  # noqa: E402


def cmd_list(args):
    panel = fp.panel_for(args)
    rows = fp.all_sites(panel)
    if not rows:
        print("no sites visible to account %s" % panel.cfg["name"])
        return 0
    width = max(len(str(s.get("domain", ""))) for s in rows)
    for site in sorted(rows, key=lambda s: str(s.get("domain", ""))):
        flags = []
        if not site.get("enabled", True):
            flags.append("disabled")
        if site.get("certificate"):
            flags.append("ssl")
        if site.get("error_count"):
            flags.append("errors:%s" % site["error_count"])
        print("%-*s  id %-5s %s" % (width, site.get("domain", "?"), site.get("id", "?"),
                                    " ".join(flags)))
    return 0


def cmd_show(args):
    panel = fp.panel_for(args)
    for site in fp.all_sites(panel):
        if args.domain in fp.site_names(site):
            owner = site.get("owner") or {}
            print("domain:   %s (id %s)" % (site.get("domain"), site.get("id")))
            print("aliases:  %s" % (", ".join(sorted(fp.site_names(site) - {site.get("domain")})) or "—"))
            print("root:     %s" % site.get("index_dir", "?"))
            print("owner:    %s" % (owner.get("username") or owner.get("login") or "?"))
            print("enabled:  %s" % site.get("enabled"))
            print("ssl:      %s" % ("yes" if site.get("certificate") else "no"))
            print("ips:      %s" % ", ".join(str(i) for i in (site.get("ips") or [])) or "?")
            print("created:  %s" % site.get("created_at", "?"))
            return 0
    fp.die("site %s not found under this panel account" % args.domain)


def main():
    ap = fp.argparse.ArgumentParser(description="FastPanel sites of a panel account")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="domains with their site ids")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="details of one site")
    p.add_argument("domain")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_show)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    fp.run(main)
