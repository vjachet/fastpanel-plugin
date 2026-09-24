#!/usr/bin/env python3
"""Databases of a panel account: list them, add one, inspect the servers.

"add" is idempotent: an existing database of that name is left alone. The
generated database password goes to a 0600 file, never to stdout.
"""

import os
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "lib")
)
import fastpanel_api as fp  # noqa: E402


# -- panel objects ---------------------------------------------------------


def charsets(panel):
    """GET /api/charsets — what the panel's own "new database" form offers."""
    status, data = panel.call("GET", "/charsets")
    if status != 200:
        return []
    out = []
    for item in fp.unwrap(data):
        if isinstance(item, dict):
            name = item.get("Charset") or item.get("charset") or item.get("name")
        else:
            name = item
        if name:
            out.append(name)
    return out


def check_charset(panel, charset):
    known = charsets(panel)
    if known and charset not in known:
        fp.die("charset %s is not offered by this panel; available: %s"
               % (charset, ", ".join(known)))


def db_servers(panel):
    status, data = panel.call("GET", "/databases/servers")
    if status != 200:
        fp.die("cannot list database servers (HTTP %s): %s" % (status, fp.panel_error(data)))
    return fp.unwrap(data)


def pick_server(servers, kind):
    """kind: mysql | pg | pg15"""
    if kind == "mysql":
        found = [s for s in servers if s.get("type") == "mysql"]
    elif kind == "pg15":
        found = [s for s in servers if s.get("type") == "postgresql" and s.get("port") == 5433]
    else:
        found = [s for s in servers if s.get("type") == "postgresql" and s.get("port") != 5433]
    if not found:
        fp.die("no %s server on this panel; available: %s"
               % (kind, ", ".join("%s:%s" % (s.get("type"), s.get("port")) for s in servers)))
    return found[0]


def server_host(server, cfg):
    """A local server reports an empty host (unix socket / loopback)."""
    host = (server.get("host") or "").strip()
    if host:
        return host
    if fp.truthy(server.get("local", True)):
        return "127.0.0.1"
    return cfg["url"].split("//")[-1].split("/")[0]


def server_port(server):
    port = server.get("port")
    if port:
        return port
    return 3306 if server.get("type") == "mysql" else 5432


def all_databases(panel):
    """GET /api/databases?offset=..&limit=.. — the panel pages this list."""
    out, offset = [], 0
    while True:
        status, data = panel.call("GET", "/databases?offset=%d&limit=%d" % (offset, fp.PAGE))
        if status != 200:
            fp.die("cannot list databases (HTTP %s): %s" % (status, fp.panel_error(data)))
        page = fp.unwrap(data)
        out.extend(page)
        if len(page) < fp.PAGE:
            return out
        offset += fp.PAGE


def find_database(panel, name):
    for db in all_databases(panel):
        if db.get("name") == name:
            return db
    return None


def db_name_prefix(login):
    """Panels usually name a user's databases <login>_<something>."""
    prefix = fp.re.sub(r"[^A-Za-z0-9]", "", login).lower()
    return prefix[:8]


def suggest_names(panel, name, login, count=3):
    """Names to offer when the wanted one is taken by another panel user."""
    taken = {db.get("name") for db in all_databases(panel)}
    prefix = db_name_prefix(login)
    candidates = []
    if prefix and not name.startswith(prefix + "_"):
        candidates.append("%s_%s" % (prefix, name))
    candidates.extend("%s%d" % (name, n) for n in range(2, 2 + count))
    if prefix:
        candidates.extend("%s_%s%d" % (prefix, name, n) for n in range(2, 2 + count))
    out = []
    for cand in candidates:
        if cand != name and cand not in taken and cand not in out:
            out.append(cand)
        if len(out) == count:
            break
    return out


def choose_name(name, hints):
    """Interactive fallback: offer the free names, or let the user type one.

    Only used when a human is at the terminal. Under an agent the script exits
    with code 3 instead and the agent asks the user.
    """
    print("Database name %s is taken by another panel user. Options:" % name)
    for i, hint in enumerate(hints, 1):
        print("  %d) %s" % (i, hint))
    print("  %d) type another name" % (len(hints) + 1))
    print("  0) cancel")
    while True:
        try:
            answer = input("choice: ").strip()
        except EOFError:
            return None
        if answer in ("0", "", "q"):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(hints):
            return hints[int(answer) - 1]
        if answer.isdigit() and int(answer) == len(hints) + 1:
            typed = input("new database name: ").strip()
            if typed:
                return typed
            continue
        # anything else that is not a menu number is taken as the name itself
        if not answer.isdigit():
            return answer
        print("no such option")


NAME_TAKEN = 3  # exit code: the name belongs to another panel user


def save_db_credentials(account, name, host, port, db_user, db_pass, engine):
    out_dir = os.path.join(fp.DB_OUT_DIR, account)
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    path = os.path.join(out_dir, name + ".env")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(
            "DB_ENGINE=%s\nDB_HOST=%s\nDB_PORT=%s\nDB_NAME=%s\nDB_USER=%s\nDB_PASSWORD=%s\n"
            % (engine, host, port, name, db_user, db_pass)
        )
    return path


# -- commands --------------------------------------------------------------


def cmd_list(args):
    panel = fp.panel_for(args)
    rows = all_databases(panel)
    if not rows:
        print("no databases visible to account %s" % panel.cfg["name"])
        return 0
    width = max(len(str(db.get("name", ""))) for db in rows)
    for db in sorted(rows, key=lambda d: str(d.get("name", ""))):
        server = db.get("server") or {}
        owner = db.get("owner") or {}
        print("%-*s  %s:%s  %s" % (width, db.get("name", "?"), server.get("type", "?"),
                                   server.get("port", "?"),
                                   owner.get("username") or owner.get("login") or ""))
    return 0


def cmd_servers(args):
    panel = fp.panel_for(args)
    for s in db_servers(panel):
        print("id %-3s %-11s %s:%s  %s%s"
              % (s.get("id"), s.get("type"), server_host(s, panel.cfg), server_port(s),
                 "available" if s.get("avail") else "unavailable",
                 "  (default)" if s.get("use_as_default") else ""))
    print("charsets: %s" % ", ".join(charsets(panel)))
    return 0


def cmd_add(args):
    panel = fp.panel_for(args)
    account = panel.cfg["name"]

    existing = find_database(panel, args.name)
    if existing:
        print("exists: database %s on account %s (server %s) — nothing to do"
              % (args.name, account, (existing.get("server") or {}).get("type", "?")))
        return 0

    check_charset(panel, args.charset)
    server = pick_server(db_servers(panel), args.server)
    owner_id = fp.own_user_id(panel)
    db_user = args.db_user or args.name
    db_pass = fp.gen_password()

    body = {
        "charset": args.charset,
        "name": args.name,
        "owner_id": owner_id,
        "server_id": server["id"],
        "user": {"login": db_user, "password": db_pass},
    }
    if args.site:
        # The panel wants the numeric site id here, as its own UI sends it.
        body["site"] = fp.find_site_id(panel, args.site)

    status, data = panel.call("POST", "/databases", body)
    if not (isinstance(data, dict) and (data.get("data") or {}).get("id")):
        if fp.says_already_exists(data):
            # The name is taken on the database server but the database is not
            # in our own list — it belongs to another panel user, so we have no
            # credentials for it and must not touch it. Offer free names instead
            # of picking one: renaming is the user's call.
            hints = suggest_names(panel, args.name, panel.cfg["login"])
            if sys.stdin.isatty() and not args.no_prompt:
                chosen = choose_name(args.name, hints)
                if not chosen:
                    fp.die("cancelled — database %s not created" % args.name)
                args.name = chosen  # db user follows the new name unless --db-user was given
                return cmd_add(args)
            print("ERROR: database %s already exists on the %s server, but under another "
                  "panel user — account %s has no access to it."
                  % (args.name, server.get("type"), account), file=sys.stderr)
            print("Ask the user which name to use instead. Free names:", file=sys.stderr)
            for hint in hints:
                print("  %s" % hint, file=sys.stderr)
            print("  (or any other name the user types)", file=sys.stderr)
            return NAME_TAKEN
        fp.die("create failed (HTTP %s): %s" % (status, fp.panel_error(data)))

    print("created: database %s on %s:%s, account %s, owner id %s"
          % (args.name, server.get("type"), server_port(server), account, owner_id))

    # The panel creates the database asynchronously — confirm it really landed.
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if find_database(panel, args.name):
            break
        time.sleep(2)
    else:
        print("warning: database not visible in the panel after %ss" % args.wait,
              file=sys.stderr)

    path = save_db_credentials(
        fp.safe_name(account), args.name, server_host(server, panel.cfg),
        server_port(server), db_user, db_pass, server.get("type"),
    )
    print("credentials: %s (mode 600) — user %s, password not printed" % (path, db_user))
    return 0


def main():
    ap = fp.argparse.ArgumentParser(description="FastPanel databases of a panel account")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="databases visible to this account")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("servers", help="database servers and charsets this panel offers")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_servers)

    p = sub.add_parser("add", help="create a database if it does not exist yet")
    p.add_argument("name", help="database name")
    fp.add_account_arg(p)
    p.add_argument("--db-user", help="database user login (default: same as the database name)")
    p.add_argument("--server", default="mysql", choices=("mysql", "pg", "pg15"),
                   help="database server (default: mysql)")
    p.add_argument("--charset", default="utf8mb4", help="database charset (default: utf8mb4)")
    p.add_argument("--site", metavar="DOMAIN", help="attach the database to this site")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the database to appear (default: 60)")
    p.add_argument("--no-prompt", action="store_true",
                   help="never ask interactively; exit with code 3 if the name is taken")
    p.set_defaults(func=cmd_add)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    fp.run(main)
