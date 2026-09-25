#!/usr/bin/env python3
"""Sites of a panel account: list, show one, create one (php, static or reverse proxy)."""

import os
import socket
import sys
import time

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
            print("ips:      %s" % (", ".join(ip_value(i) for i in (site.get("ips") or [])) or "?"))
            print("created:  %s" % site.get("created_at", "?"))
            return 0
    fp.die("site %s not found under this panel account" % args.domain)


NAME_TAKEN = 3

# What a new site gets unless told otherwise. The panel's own defaults differ
# (access log on, 10 rotated files, gzip level 1, no static caching).
DEFAULTS = {
    "access_log": False,
    "rotate": 0,
    "gzip_level": 5,
    "cache_days": 14,
    "ssl": True,
}

# What the panel's https form gets once the certificate is issued.
HTTPS = {"https_redirect": True, "hsts": True, "http2": True, "http3": True}

# The panel picks mpm_itk first; versions that lack it fall through the list.
HANDLER_ORDER = ("mpm_itk", "php_fpm", "fcgi", "cgi")


def ip_value(item):
    return str(item.get("ip") or item.get("value")) if isinstance(item, dict) else str(item)


def panel_settings(panel):
    """GET /api/settings — installed php versions and server ips live here."""
    status, data = panel.call("GET", "/settings")
    if status != 200:
        fp.die("cannot read panel settings (HTTP %s): %s" % (status, fp.panel_error(data)))
    return data.get("data") or {}


def php_versions(settings):
    """[(version, [handlers], is_default)] sorted by version, e.g. ("83", [...], True)."""
    block = (settings.get("configuration") or {}).get("php_version") or {}
    out = []
    for item in block.values():
        if not isinstance(item, dict) or not fp.truthy(item.get("status", True)):
            continue
        version = str(item.get("version") or "").strip()
        if version:
            out.append((version, list(item.get("types") or []), bool(item.get("default"))))
    return sorted(out, key=lambda v: int(v[0]) if v[0].isdigit() else 0)


def server_ips(panel, settings):
    ips = settings.get("ips")
    if not ips:
        status, data = panel.call("GET", "/ips")
        ips = fp.unwrap(data) if status == 200 else []
    return [i["value"] for i in ips if isinstance(i, dict) and i.get("value") and i.get("enabled")]


def default_version(versions):
    for version, _, is_default in versions:
        if is_default:
            return version
    return versions[-1][0] if versions else None


def default_handler(handlers):
    for handler in HANDLER_ORDER:
        if handler in handlers:
            return handler
    return handlers[0] if handlers else None


def php_label(version):
    return "PHP %s.%s" % (version[0], version[1:]) if len(version) > 1 else "PHP " + version


def pick(title, options, default):
    """Numbered choice on a terminal; Enter keeps the default, text is taken as is."""
    print(title, file=sys.stderr)
    for n, (value, label) in enumerate(options, 1):
        mark = "  (default)" if value == default else ""
        print("  %d) %s%s" % (n, label, mark), file=sys.stderr)
    while True:
        try:
            answer = input("choice [Enter = default]: ").strip()
        except EOFError:
            return default
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        return answer


def ask_number(title, default, low, high):
    while True:
        try:
            answer = input("%s [%s]: " % (title, default)).strip()
        except EOFError:
            return default
        if not answer:
            return default
        if answer.isdigit() and low <= int(answer) <= high:
            return int(answer)
        print("  a number from %d to %d, please" % (low, high), file=sys.stderr)


def ask_yes(title, default):
    try:
        answer = input("%s [%s]: " % (title, "Y/n" if default else "y/N")).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


def backend_label(args):
    if args.kind == "static":
        return "static, no php"
    if args.kind == "reverse_proxy":
        return "reverse proxy to %s" % args.proxy
    return "%s, %s" % (php_label(args.php), args.handler)


def ask_port():
    while True:
        try:
            answer = input("App port on this server: ").strip()
        except EOFError:
            fp.die("the app port is required")
        if answer.isdigit() and 1 <= int(answer) <= 65535:
            return int(answer)
        print("  a port from 1 to 65535, please", file=sys.stderr)


def cmd_options(args):
    """Everything `add` can be told, with what it picks when not told."""
    panel = fp.panel_for(args)
    settings = panel_settings(panel)
    versions = php_versions(settings)
    ips = server_ips(panel, settings)
    dv = default_version(versions)

    print("ip (--ip, repeatable):")
    for n, ip in enumerate(ips):
        print("  %s%s" % (ip, "  (default)" if n == 0 else ""))
    print("php version (--php) and handler (--handler):")
    for version, handlers, _ in versions:
        print("  %-4s %-8s handlers: %s%s" % (
            version, php_label(version), ", ".join(handlers),
            "  (default, handler %s)" % default_handler(handlers) if version == dv else ""))
    print("static content, no php (--static)")
    print("reverse proxy to an app port on this server, e.g. node.js (--proxy PORT)")
    print("aliases: www.DOMAIN by default (--no-www to drop it, --alias NAME to add more)")
    print("access log (--access-log / --no-access-log): %s" % ("on" if DEFAULTS["access_log"] else "off"))
    print("log rotation, files kept (--rotate N): %d" % DEFAULTS["rotate"])
    print("gzip level 1..9 (--gzip-level N, --no-gzip): %d" % DEFAULTS["gzip_level"])
    print("static cache, days (--cache-days N, 0 = off): %d" % DEFAULTS["cache_days"])
    print("let's encrypt certificate (--ssl / --no-ssl): %s, email admin@DOMAIN, then %s"
          % ("on" if DEFAULTS["ssl"] else "off", ", ".join(k for k in HTTPS if HTTPS[k])))
    return 0


def resolve_php(args, versions, by_version, dv, interactive):
    if not args.php and interactive:
        fitting = [v for v, h, _ in versions if not args.handler or args.handler in h]
        args.php = pick("PHP version:", [(v, php_label(v)) for v in fitting],
                        dv if dv in fitting else fitting[-1])
    args.php = (args.php or dv).replace(".", "")
    if args.php not in by_version:
        fp.die("php %s is not installed on the panel; available: %s"
               % (args.php, ", ".join(by_version)))
    handlers = by_version[args.php]
    args.handler = args.handler or default_handler(handlers)
    if args.handler not in handlers:
        fp.die("handler %s is not available for %s; available: %s"
               % (args.handler, php_label(args.php), ", ".join(handlers)))


def resolve_choices(args, panel):
    settings = panel_settings(panel)
    versions = php_versions(settings)
    ips = server_ips(panel, settings)
    if not versions:
        fp.die("the panel reports no php versions available to this account")
    if not ips:
        fp.die("the panel reports no enabled ip addresses")
    interactive = sys.stdin.isatty() and not args.no_prompt
    by_version = {v: h for v, h, _ in versions}

    # Four ways to serve the site, as the user picks them: the apache module on
    # the panel's default version, fastcgi on any version that has it, plain
    # static files, or a reverse proxy to an app (node.js and the like).
    dv = default_version(versions)
    if args.static and args.proxy is not None:
        fp.die("--static and --proxy exclude each other")
    if not (args.handler or args.php or args.static or args.proxy is not None) and interactive:
        itk = dv if "mpm_itk" in by_version.get(dv, []) else None
        options = [("mpm_itk", "Apache module (mpm_itk), %s" % php_label(itk))] if itk else []
        options.append(("fcgi", "FastCGI (fcgi), choose the version"))
        options.append(("static", "Static content, no PHP"))
        options.append(("reverse_proxy", "Reverse proxy (node.js and other apps)"))
        args.handler = pick("Site backend:", options, options[0][0])
        if args.handler == "mpm_itk":
            args.php = itk
        elif args.handler == "static":
            args.static, args.handler = True, None
        elif args.handler == "reverse_proxy":
            args.handler = None
            args.proxy = ask_port()
    args.kind = "static" if args.static else "reverse_proxy" if args.proxy is not None else "php"
    if args.kind == "php":
        resolve_php(args, versions, by_version, dv, interactive)
    else:
        if args.php or args.handler:
            fp.die("--static and --proxy take no --php or --handler")
        if args.proxy is not None:
            if not 1 <= args.proxy <= 65535:
                fp.die("--proxy takes a port, 1..65535")
            args.proxy = "http://localhost:%d" % args.proxy
        # The site is born as php with the panel's defaults, then switched:
        # that is the path the panel's own forms take.
        args.base_php, args.base_handler = dv, default_handler(by_version[dv])

    args.ip = args.ip or [ips[0]]
    unknown = [ip for ip in args.ip if ip not in ips]
    if unknown:
        fp.die("ip %s is not among the panel's enabled addresses: %s"
               % (", ".join(unknown), ", ".join(ips)))

    if interactive:
        if args.access_log is None:
            args.access_log = ask_yes("Log visits (access log)?", DEFAULTS["access_log"])
        if args.rotate is None:
            args.rotate = ask_number("Rotated log files to keep", DEFAULTS["rotate"], 0, 365)
        if args.gzip_level is None and not args.no_gzip:
            args.gzip_level = ask_number("Gzip level (1..9)", DEFAULTS["gzip_level"], 1, 9)
        if args.cache_days is None:
            args.cache_days = ask_number("Static cache, days (0 = off)", DEFAULTS["cache_days"], 0, 3650)
        if args.ssl is None:
            args.ssl = ask_yes("Issue a Let's Encrypt certificate?", DEFAULTS["ssl"])
    for key in DEFAULTS:
        if getattr(args, key) is None:
            setattr(args, key, DEFAULTS[key])
    if not 1 <= args.gzip_level <= 9:
        fp.die("--gzip-level must be 1..9")


def wait_for_site(panel, domain, site_id, timeout=180):
    """Creation is queued: wait for the id to show up and the site to settle."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not site_id:
            status, data = panel.call("GET", "/sites/simple")
            if status == 200:
                for site in fp.unwrap(data):
                    if site.get("domain") == domain:
                        site_id = site.get("id")
        if site_id:
            status, data = panel.call("GET", "/sites/%s" % site_id)
            site = data.get("data") if status == 200 and isinstance(data, dict) else None
            if isinstance(site, dict) and not site.get("action") and site.get("status") == "active":
                return site
        time.sleep(3)
    fp.die("site %s was submitted but did not become active within %ds — check the panel; "
           "nginx and log settings were not applied" % (domain, timeout))


def resolves_to(name, ips):
    try:
        found = {a[4][0] for a in socket.getaddrinfo(name, None)}
    except socket.gaierror:
        return False, "does not resolve"
    if found & set(ips):
        return True, ""
    return False, "points to %s" % ", ".join(sorted(found))


def wait_for_certificate(panel, cert_id, timeout=300):
    """Issuing is queued: done when the certificate carries no pending action."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, data = panel.call("GET", "/certificates/%s" % cert_id)
        if status == 404:
            return "the panel dropped the certificate — Let's Encrypt refused it"
        cert = data.get("data") if status == 200 and isinstance(data, dict) else None
        if isinstance(cert, dict) and not cert.get("action"):
            return None if cert.get("expired_at") or cert.get("issued_at") else \
                "the panel finished without an issued certificate"
        time.sleep(5)
    return "not issued within %ds — check the panel" % timeout


def issue_certificate(panel, site_id, domain, aliases, ips):
    """Let's Encrypt for the domain and those aliases that point here; then https on.

    Returns a line for the report; never fails the whole run — the site is already there.
    """
    ok, why = resolves_to(domain, ips)
    if not ok:
        return "skipped — %s %s, not to %s" % (domain, why, ", ".join(ips))
    names, left_out = [], []
    for alias in aliases:
        (names if resolves_to(alias, ips)[0] else left_out).append(alias)

    body = {
        "type": "letsencrypt",
        "email": "admin@" + domain,
        "common_name": domain,
        "alternative_name": ",".join(names),
        "force_dns_validation": False,
        "virtualhost": site_id,
        "length": 2048,
    }
    status, data = panel.call("POST", "/certificates", body)
    if status >= 300:
        return "failed (HTTP %s): %s" % (status, fp.panel_error(data))
    cert = data.get("data") if isinstance(data, dict) else None
    cert_id = cert.get("id") if isinstance(cert, dict) else None
    if not cert_id:
        return "submitted, but the panel returned no certificate id — check the panel"
    problem = wait_for_certificate(panel, cert_id)
    if problem:
        return "failed: %s" % problem

    wait_for_site(panel, domain, site_id)
    https = dict(HTTPS, manual_changes=False, certificate=cert_id)
    status, data = panel.call("PUT", "/sites/%s" % site_id, https)
    if status >= 300:
        return "issued (id %s), but turning https on failed (HTTP %s): %s" % (
            cert_id, status, fp.panel_error(data))
    line = "issued for %s (id %s); %s" % (", ".join([domain] + names), cert_id,
                                           ", ".join(k for k in HTTPS if HTTPS[k]))
    if left_out:
        line += "; left out, dns not here: %s" % ", ".join(left_out)
    return line


def cmd_add(args):
    domain = args.domain.strip().lower().rstrip(".")
    panel = fp.panel_for(args)

    for site in fp.all_sites(panel):
        if domain in fp.site_names(site):
            print("exists: %s (site id %s, main domain %s) — nothing changed"
                  % (domain, site.get("id"), site.get("domain")))
            return 0

    resolve_choices(args, panel)

    aliases = [] if args.no_www else ["www." + domain]
    aliases += [a.strip().lower() for a in args.alias or [] if a.strip()]
    aliases = [{"name": a} for a in dict.fromkeys(aliases) if a != domain]

    # The same pre-flight the panel's wizard runs: validity and name clashes.
    status, data = panel.call("POST", "/master/domain", {"domain": domain, "aliases": aliases})
    if status >= 300:
        if fp.says_already_exists(data):
            print("ERROR: %s is already used on this panel by another panel user — "
                  "account %s cannot see it. Ask the user for another domain."
                  % (domain, panel.cfg["name"]), file=sys.stderr)
            print("panel: %s" % fp.panel_error(data), file=sys.stderr)
            return NAME_TAKEN
        fp.die("domain check failed (HTTP %s): %s" % (status, fp.panel_error(data)))

    body = {
        "aliases": aliases,
        "domain": domain,
        "email_domain": False,
        "ips": [{"ip": ip} for ip in args.ip],
        "dns_domain": None,
        "owner": fp.own_user_id(panel),
        "ssh_access": None,
        "user": None,
        "database": None,
        "type": "php",
        "handler": args.handler if args.kind == "php" else args.base_handler,
        "handler_version": args.php if args.kind == "php" else args.base_php,
        "ftp_account": None,
        "sftp_account": None,
        "backup_plan_id": None,
    }
    status, data = panel.call("PUT", "/master", body)
    if status >= 300:
        if fp.says_already_exists(data):
            print("ERROR: %s already exists under another panel user." % domain, file=sys.stderr)
            print("panel: %s" % fp.panel_error(data), file=sys.stderr)
            return NAME_TAKEN
        fp.die("site creation failed (HTTP %s): %s" % (status, fp.panel_error(data)))
    created = data.get("data") if isinstance(data, dict) else None
    site_id = created.get("id") if isinstance(created, dict) else None
    print("submitted: %s (%s, ip %s)" % (domain, backend_label(args), ", ".join(args.ip)),
          file=sys.stderr)

    site = wait_for_site(panel, domain, site_id)
    site_id = site["id"]
    if args.kind != "php":
        # Same payload as the panel's "backend" form.
        backend = {
            "index_dir": site.get("index_dir"),
            "manual_changes": False,
            "type": args.kind,
            "handler": None,
            "port": site.get("port"),
            "socket_path": site.get("socket_path"),
        }
        if args.kind == "reverse_proxy":
            backend["upstreams"] = [{"type": "host", "address": args.proxy}]
        status, data = panel.call("PUT", "/sites/backend/%s" % site_id, backend)
        if status >= 300:
            fp.die("site %s created (id %s) as php, but switching it to %s failed (HTTP %s): %s"
                   % (domain, site_id, args.kind, status, fp.panel_error(data)))
        site = wait_for_site(panel, domain, site_id)

    # Same payload as the panel's "static content" form.
    static = {
        "manual_changes": False,
        "gzip": not args.no_gzip,
        "gzip_comp_level": args.gzip_level,
        "static_file_handler": site.get("static_file_handler", True) or args.cache_days > 0,
        "static_extension": site.get("static_extension") or "",
        "static_sub_directory": site.get("static_sub_directory") or "",
        "expired": args.cache_days,
    }
    status, data = panel.call("PUT", "/sites/%s" % site_id, static)
    if status >= 300:
        fp.die("site %s created (id %s), but nginx settings failed (HTTP %s): %s"
               % (domain, site_id, status, fp.panel_error(data)))

    status, data = panel.call("GET", "/sites/%s/log_rotate" % site_id)
    logs = (data.get("data") if status == 200 and isinstance(data, dict) else None) or {}
    logging = {
        "access_log": args.access_log,
        "error_log": logs.get("error_log", True),
        "rotate": args.rotate,
        "log_period": logs.get("log_period") or "daily",
        "awstats": logs.get("awstats", False),
    }
    status, data = panel.call("PUT", "/sites/%s/log_rotate" % site_id, logging)
    if status >= 300:
        fp.die("site %s created (id %s), but log settings failed (HTTP %s): %s"
               % (domain, site_id, status, fp.panel_error(data)))

    ssl = "off"
    if args.ssl:
        wait_for_site(panel, domain, site_id)
        ssl = issue_certificate(panel, site_id, domain, [a["name"] for a in aliases], args.ip)

    print("created: %s (site id %s)" % (domain, site_id))
    print("root:    %s" % site.get("index_dir", "?"))
    print("aliases: %s" % (", ".join(a["name"] for a in aliases) or "—"))
    print("backend: %s" % backend_label(args))
    print("ip:      %s" % ", ".join(args.ip))
    print("gzip:    %s" % ("off" if args.no_gzip else "level %d" % args.gzip_level))
    print("cache:   %s" % ("off" if not args.cache_days else "%d days" % args.cache_days))
    print("logs:    access %s, rotate %d" % ("on" if args.access_log else "off", args.rotate))
    print("ssl:     %s" % ssl)
    return 0


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

    p = sub.add_parser("options", help="what `add` can choose from on this panel")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_options)

    p = sub.add_parser("add", help="create a site: php, static or reverse proxy")
    p.add_argument("domain")
    p.add_argument("--alias", action="append", metavar="NAME", help="extra alias, repeatable")
    p.add_argument("--no-www", action="store_true", help="do not add the www. alias")
    p.add_argument("--ip", action="append", metavar="IP", help="ip to bind, repeatable")
    p.add_argument("--php", metavar="VER", help="php version as the panel lists it: 83, 8.3, 74 ...")
    p.add_argument("--static", action="store_true", help="static content, no php")
    p.add_argument("--proxy", type=int, metavar="PORT",
                   help="reverse proxy to http://localhost:PORT (a node.js app, say)")
    p.add_argument("--handler", help="mpm_itk (apache module) or fcgi (fastcgi); "
                   "php_fpm and cgi too, if the version supports them")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--access-log", dest="access_log", action="store_true", default=None,
                   help="log visits (default: off)")
    g.add_argument("--no-access-log", dest="access_log", action="store_false")
    p.add_argument("--rotate", type=int, metavar="N", help="rotated log files to keep (default 0)")
    p.add_argument("--gzip-level", type=int, metavar="N", help="1..9 (default 5)")
    p.add_argument("--no-gzip", action="store_true", help="turn gzip off")
    p.add_argument("--cache-days", type=int, metavar="N", help="static cache, days (default 14, 0 = off)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ssl", dest="ssl", action="store_true", default=None,
                   help="issue a let's encrypt certificate, admin@DOMAIN (default: on)")
    g.add_argument("--no-ssl", dest="ssl", action="store_false")
    p.add_argument("--no-prompt", action="store_true",
                   help="never ask on a terminal; unset options take the defaults")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_add)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    fp.run(main)
