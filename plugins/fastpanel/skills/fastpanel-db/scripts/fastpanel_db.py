#!/usr/bin/env python3
"""Databases of a panel account: list them, add one, inspect the servers.

"add" is idempotent: an existing database of that name is left alone. The
generated database password goes to a 0600 file, never to stdout.
"""

import hashlib
import json
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


def suggest_names(panel, name, count=3):
    """Names to offer when the wanted one is taken by another panel user."""
    taken = {db.get("name") for db in all_databases(panel)}
    # numbered only: a login prefix would put the panel login into the output
    candidates = ["%s%d" % (name, n) for n in range(2, 2 + count + len(taken))]
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
        print("%-*s  %s:%s" % (width, db.get("name", "?"), server.get("type", "?"),
                               server.get("port", "?")))
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


def existing_db_user_id(panel, server, login):
    """Id of a database user that already exists on this database server."""
    status, data = panel.call("GET", "/databases/servers/%s/users" % server["id"])
    rows = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        fp.die("cannot list database users of server %s (HTTP %s): %s"
               % (server["id"], status, fp.panel_error(data)))
    for row in rows:
        if isinstance(row, dict) and row.get("login") == login:
            return row.get("id")
    fp.die("database user %s not found on the %s server — drop --existing-user to create a new one"
           % (login, server.get("type")))


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
    owner_id = fp.resolve_owner(panel, args.owner)
    # The panel wants the numeric site id, as its own UI sends it. The site is
    # checked before anything is created: same owner as the database, or no go.
    site_id = fp.site_of_owner(panel, args.site.strip().lower().rstrip("."), owner_id)
    db_user = args.existing_user or args.db_user or args.name
    db_pass = None
    body = {
        "charset": args.charset,
        "name": args.name,
        "owner_id": owner_id,
        "server_id": server["id"],
        "site": site_id,
    }
    if args.existing_user:
        # Attach the new database to a database user that already exists on this
        # server (as the panel form "existing user" does): its password stays as
        # it is, so nothing is generated and no credentials file is written.
        user_id = existing_db_user_id(panel, server, args.existing_user)
        body["user_id"] = user_id
        what = "existing database user %s (id %s)" % (db_user, user_id)
    else:
        db_pass = fp.gen_password()
        body["user"] = {"login": db_user, "password": db_pass}
        what = "new database user %s" % db_user
    fp.announce(panel, "create database %s on %s:%s for site %s, %s"
                % (args.name, server.get("type"), server_port(server), args.site, what),
                owner_id)

    status, data = panel.call("POST", "/databases", body)
    if not (isinstance(data, dict) and (data.get("data") or {}).get("id")):
        if fp.says_already_exists(data):
            # The name is taken on the database server but the database is not
            # in our own list — it belongs to another panel user, so we have no
            # credentials for it and must not touch it. Offer free names instead
            # of picking one: renaming is the user's call.
            hints = suggest_names(panel, args.name)
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

    print("created: database %s on %s:%s, account %s, owner id %s, site %s (id %s)"
          % (args.name, server.get("type"), server_port(server), account, owner_id,
             args.site, site_id))

    # The panel creates the database asynchronously — confirm it really landed.
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if find_database(panel, args.name):
            break
        time.sleep(2)
    else:
        print("warning: database not visible in the panel after %ss" % args.wait,
              file=sys.stderr)

    if db_pass is None:
        print("user: existing database user %s, password unchanged" % db_user)
        return 0
    path = save_db_credentials(
        fp.safe_name(account), args.name, server_host(server, panel.cfg),
        server_port(server), db_user, db_pass, server.get("type"),
    )
    print("credentials: %s (mode 600) — user %s, password not printed" % (path, db_user))
    return 0


def database_or_die(panel, name):
    db = find_database(panel, name)
    if not db:
        fp.die("database %s not found under account %s" % (name, panel.cfg["name"]))
    return db


def db_site(db):
    """(site id, domain) the database is attached to, or (None, None)."""
    site = db.get("site") or {}
    site_id = db.get("site_id") or site.get("id")
    return (site_id or None), (site.get("domain") or None)


def print_database(db):
    server = db.get("server") or {}
    site_id, domain = db_site(db)
    print("database:  %s (id %s)" % (db.get("name"), db.get("id")))
    print("server:    %s:%s" % (server.get("type", "?"), server_port(server)))
    print("owner id:  %s" % fp.owner_id_of(db))
    print("site:      %s" % ("%s (id %s)" % (domain or "?", site_id) if site_id else "—"))
    print("charset:   %s" % db.get("charset", "?"))
    if db.get("size") is not None:
        print("size:      %s bytes" % db.get("size"))


def cmd_show(args):
    panel = fp.panel_for(args)
    print_database(database_or_die(panel, args.name))
    return 0


def db_delete_code(db):
    """Ties a real delete to the dry run the user was shown."""
    site_id, _ = db_site(db)
    seen = json.dumps([db.get("id"), db.get("name"), fp.owner_id_of(db), site_id,
                       (db.get("server") or {}).get("id")], sort_keys=True, default=str)
    return hashlib.sha256(seen.encode()).hexdigest()[:8]


def cmd_delete(args):
    """The "delete" button of a database (DELETE /api/databases/<id>). Not undoable."""
    panel = fp.panel_for(args)
    db = database_or_die(panel, args.name)
    fp.announce(panel, "delete database %s (id %s) with its data and users"
                % (db.get("name"), db.get("id")), fp.owner_id_of(db))
    print_database(db)
    code = db_delete_code(db)
    if not args.confirm:
        print("dry run: nothing deleted. Show the lines above to the user; once they agree, "
              "repeat the same command with --confirm %s" % code)
        return 0
    if args.confirm != code:
        fp.die("--confirm %s does not match: the database changed since the dry run it came "
               "from. Run without --confirm again, show the output to the user and ask"
               % args.confirm)
    status, data = panel.call("DELETE", "/databases/%s" % db["id"])
    if status >= 300:
        fp.die("delete of %s failed (HTTP %s): %s" % (args.name, status, fp.panel_error(data)))
    # Deletion is queued: wait for the database to leave the list.
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if not find_database(panel, args.name):
            print("deleted: database %s (id %s)" % (args.name, db["id"]))
            return 0
        time.sleep(3)
    fp.die("delete of %s was submitted but it is still listed after %ds — check the panel"
           % (args.name, args.wait))


def cmd_attach(args):
    """Attach an existing database to a site of the same owner (PUT /api/databases/<id>,
    as the panel's own edit form sends it)."""
    panel = fp.panel_for(args)
    db = database_or_die(panel, args.name)
    cur_owner = fp.owner_id_of(db)
    # --owner hands the database to another panel user in the same request:
    # the panel's edit form has both the owner and the site fields.
    owner_id = args.owner if args.owner is not None else cur_owner
    domain = args.site.strip().lower().rstrip(".")
    site_id = fp.site_of_owner(panel, domain, owner_id)
    cur_id, cur_domain = db_site(db)
    if cur_id == site_id and str(owner_id) == str(cur_owner):
        print("unchanged: database %s is already attached to site %s (id %s)"
              % (args.name, domain, site_id))
        return 0
    fp.announce(panel, "attach database %s (id %s) to site %s (id %s); was: %s"
                % (args.name, db["id"], domain, site_id,
                   "%s (id %s)" % (cur_domain or "?", cur_id) if cur_id else "no site")
                + ("; owner: user id %s -> %s" % (cur_owner, owner_id)
                   if str(owner_id) != str(cur_owner) else ""), owner_id)
    # On an existing database the panel's edit form leaves only the owner and the
    # site editable; the site goes as its numeric id, like in "add".
    body = {"id": db["id"], "owner_id": owner_id, "site": site_id}
    status, data = panel.call("PUT", "/databases/%s" % db["id"], body)
    if status >= 300:
        fp.die("attach failed (HTTP %s): %s" % (status, fp.panel_error(data)))
    deadline = time.time() + args.wait
    while time.time() < deadline:
        now = find_database(panel, args.name)
        if now and db_site(now)[0] == site_id and str(fp.owner_id_of(now)) == str(owner_id):
            print("attached: database %s to site %s (id %s), owner user id %s"
                  % (args.name, domain, site_id, owner_id))
            return 0
        time.sleep(2)
    fp.die("the panel accepted the change, but database %s still is not attached to %s after "
           "%ds — check the panel" % (args.name, domain, args.wait))


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
    fp.add_owner_arg(p)
    p.add_argument("--db-user", help="database user login (default: same as the database name)")
    p.add_argument("--existing-user", metavar="LOGIN",
                   help="attach to a database user that already exists; its password is kept")
    p.add_argument("--server", default="mysql", choices=("mysql", "pg", "pg15"),
                   help="database server (default: mysql)")
    p.add_argument("--charset", default="utf8mb4", help="database charset (default: utf8mb4)")
    p.add_argument("--site", metavar="DOMAIN", required=True,
                   help="site the database belongs to; must have the same owner")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the database to appear (default: 60)")
    p.add_argument("--no-prompt", action="store_true",
                   help="never ask interactively; exit with code 3 if the name is taken")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("show", help="one database: id, server, owner, site")
    p.add_argument("name", help="database name")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("delete", help="delete a database; without --confirm only shows it")
    p.add_argument("name", help="database name")
    fp.add_account_arg(p)
    p.add_argument("--confirm", metavar="CODE",
                   help="really delete: the code printed by the same command without --confirm")
    p.add_argument("--wait", type=int, default=120,
                   help="seconds to wait for the database to disappear (default: 120)")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("attach", help="attach an existing database to a site of the same owner")
    p.add_argument("name", help="database name")
    p.add_argument("--site", metavar="DOMAIN", required=True, help="site to attach it to")
    fp.add_account_arg(p)
    fp.add_owner_arg(p)
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the panel to show the new site (default: 60)")
    p.set_defaults(func=cmd_attach)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    fp.run(main)
