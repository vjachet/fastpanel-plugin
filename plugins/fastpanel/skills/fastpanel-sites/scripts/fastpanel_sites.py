#!/usr/bin/env python3
"""Sites of a panel account: list, show one, create one (php, static or reverse proxy),
delete one."""

import hashlib
import json
import os
import re
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
            print("domain:   %s (id %s)" % (site.get("domain"), site.get("id")))
            print("aliases:  %s" % (", ".join(sorted(fp.site_names(site) - {site.get("domain")})) or "—"))
            print("root:     %s" % site.get("index_dir", "?"))
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
        # Newer panels report "83", older builds "8.3"; site backends take "83" on both.
        version = str(item.get("version") or "").strip().replace(".", "")
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


DOH_SERVERS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)


def doh_addresses(name):
    """A and AAAA of a name from public DNS-over-HTTPS — what Let's Encrypt sees.

    The local resolver can lie (a VPN or proxy with fake IPs answers with its own
    addresses), so it is only the fallback. None when no DoH server answered.
    """
    import urllib.parse
    import urllib.request
    for server in DOH_SERVERS:
        found, answered = set(), True
        for rtype in ("A", "AAAA"):
            url = "%s?%s" % (server, urllib.parse.urlencode({"name": name, "type": rtype}))
            req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except (OSError, ValueError):
                answered = False
                break
            if data.get("Status") not in (0, 3):  # NOERROR, NXDOMAIN
                answered = False
                break
            want = 1 if rtype == "A" else 28
            found |= {a.get("data") for a in data.get("Answer") or [] if a.get("type") == want}
        if answered:
            return found
    return None


def resolves_to(name, ips):
    found = doh_addresses(name)
    if found is None:
        try:
            found = {a[4][0] for a in socket.getaddrinfo(name, None)}
        except socket.gaierror:
            return False, "does not resolve"
    if not found:
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


def certificate_names(panel, cert):
    """Names an attached certificate covers; empty when there is none or it cannot be read."""
    cert_id = cert.get("id") if isinstance(cert, dict) else cert
    if not cert_id:
        return set()
    status, data = panel.call("GET", "/certificates/%s" % cert_id)
    c = data.get("data") if status == 200 and isinstance(data, dict) else None
    if not isinstance(c, dict):
        return set()
    names = {c.get("common_name")} | set(re.split(r"[\s,]+", c.get("alternative_name") or ""))
    return {n.lower() for n in names if n}


def cmd_ssl(args):
    """(Re)issue Let's Encrypt once dns is in place — e.g. an alias's A record was added later."""
    domain = args.domain.strip().lower().rstrip(".")
    panel = fp.panel_for(args)
    site_id = fp.find_site_id(panel, domain)
    if not site_id:
        fp.die("site %s not found under this panel account" % domain)
    fp.announce(panel, "issue a let's encrypt certificate for site %s" % domain)
    site = wait_for_site(panel, domain, site_id)
    main = site.get("domain")
    aliases = sorted(fp.site_names(site) - {main})
    ips = [ip_value(i) for i in site.get("ips") or []]

    wanted = {n for n in [main] + aliases if resolves_to(n, ips)[0]}
    if not wanted:
        fp.die("%s does not point to the site's ip (%s) yet — nothing to issue"
               % (main, ", ".join(ips)))
    have = certificate_names(panel, site.get("certificate"))
    if wanted <= have:
        print("covered: the site's certificate already has %s — nothing changed"
              % ", ".join(sorted(wanted)))
        missing = sorted(set(aliases) - wanted - have)
        if missing:
            print("no dns here yet: %s" % ", ".join(missing))
        return 0

    line = issue_certificate(panel, site["id"], main, aliases, ips)
    print("ssl: %s" % line)
    return 0 if line.startswith("issued for") else 1


def read_backend(panel, site_id):
    status, data = panel.call("GET", "/sites/backend/%s" % site_id)
    backend = data.get("data") if status == 200 and isinstance(data, dict) else None
    if not isinstance(backend, dict):
        fp.die("cannot read the backend of site %s (HTTP %s): %s"
               % (site_id, status, fp.panel_error(data)))
    return backend


def put_backend(panel, site, current, change):
    """PUT /sites/backend/<id> with the panel's "backend" form payload."""
    body = {
        "index_dir": site.get("index_dir"),
        "manual_changes": False,
        "type": current.get("type"),
        "handler": current.get("handler"),
        "port": current.get("port"),
        "socket_path": current.get("socket_path"),
    }
    body.update(change)
    if body["type"] == "php":
        body.setdefault("app_file", current.get("app_file") or "index.php index.html")
        body.setdefault("environment", current.get("environment") or [""])
        body.setdefault("work_dir", current.get("work_dir") or "")
    return panel.call("PUT", "/sites/backend/%s" % site["id"], body)


def upstream_addresses(backend):
    return [u.get("address") for u in backend.get("upstreams") or [] if isinstance(u, dict)]


def switch_to_proxy(panel, site, current, now, port):
    """Turn a site into a reverse proxy to http://localhost:PORT (the panel's "backend"
    form with type reverse_proxy, as add --proxy sends it). The site, its root and
    its files stay; only the nginx backend changes."""
    if not 1 <= port <= 65535:
        fp.die("--proxy takes a port, 1..65535")
    target = "http://localhost:%d" % port
    domain, site_id = site.get("domain"), site["id"]
    if current.get("type") == "reverse_proxy" and upstream_addresses(current) == [target]:
        print("unchanged: %s already proxies to %s — nothing changed" % (domain, target))
        return 0
    status, data = put_backend(panel, site, current, {
        "type": "reverse_proxy", "handler": None,
        "upstreams": [{"type": "host", "address": target}]})
    if status >= 300:
        fp.die("switching %s to a reverse proxy failed (HTTP %s): %s"
               % (domain, status, fp.panel_error(data)))
    deadline = time.time() + 180
    while True:
        after = read_backend(panel, site_id)
        if (after.get("type") == "reverse_proxy" and not after.get("action")
                and upstream_addresses(after) in ([target], [])):
            break
        if time.time() > deadline:
            fp.die("switch of %s was submitted but not applied within 180s — check the panel"
                   % domain)
        time.sleep(3)
    print("switched: %s (site id %s)" % (domain, site_id))
    print("backend: reverse proxy to %s (was %s)" % (target, now))
    return 0


# proxy-port exit codes: one per reason, so an operator can tell them apart.
EXIT_ACCESS_DENIED = 11      # the account cannot open or change the site
EXIT_DOMAIN_MISMATCH = 12    # the site id belongs to another domain
EXIT_MANUAL_CONFIG = 13      # the site's nginx config was edited by hand
EXIT_UPSTREAM_MISMATCH = 14  # no single "host" upstream on --from-port to replace
EXIT_APPLY_TIMEOUT = 15      # written, but the panel did not report it applied in time

DENIED = (401, 403, 404)


def address_port(address):
    """Port of an upstream address like http://127.0.0.1:4641 or 127.0.0.1:4641."""
    m = re.match(r"^(?:[a-z][a-z0-9+.-]*://)?(\[[^\]]*\]|[^/:]*)(?::(\d+))?(/.*)?$",
                 str(address or ""), re.I)
    return int(m.group(2)) if m and m.group(2) else None


def with_port(address, port):
    m = re.match(r"^((?:[a-z][a-z0-9+.-]*://)?(?:\[[^\]]*\]|[^/:]*)):\d+(/.*)?$",
                 str(address), re.I)
    return "%s:%d%s" % (m.group(1), port, m.group(2) or "")


def host_upstreams(backend, port):
    return [u for u in backend.get("upstreams") or []
            if isinstance(u, dict) and u.get("type") == "host" and address_port(u.get("address")) == port]


def manual_config(site, backend):
    return bool(site.get("manual_changes") or backend.get("manual_changes"))


def describe_upstreams(backend):
    rows = ["%s %s" % (u.get("type"), u.get("address")) for u in backend.get("upstreams") or []
            if isinstance(u, dict)]
    return "; ".join(rows) or "—"


def proxy_state(panel, site_id):
    """What the panel reports right now; None fields when it cannot be read."""
    status, data = panel.call("GET", "/sites/backend/%s" % site_id)
    backend = data.get("data") if status == 200 and isinstance(data, dict) else None
    return backend if isinstance(backend, dict) else None


def cmd_proxy_port(args):
    """Move one reverse-proxy upstream of a site from one local port to another.
    Every other upstream stays as it is. The site is named twice — by domain and by id —
    and both must agree before anything is written."""
    domain = args.domain.strip().lower().rstrip(".")
    for name, port in (("--from-port", args.from_port), ("--port", args.port)):
        if not 1 <= port <= 65535:
            fp.die("%s takes a port, 1..65535" % name, code=2)
    if args.from_port == args.port:
        fp.die("--from-port and --port are the same", code=2)
    panel = fp.panel_for(args)
    account = fp.hashed_name(panel.cfg)
    site_id = args.site_id
    print("account: %s" % account)
    print("site:    id %s, expected domain %s" % (site_id, domain))

    status, data = panel.call("GET", "/sites/%s" % site_id)
    site = data.get("data") if status == 200 and isinstance(data, dict) else None
    if status in DENIED or (status == 200 and not isinstance(site, dict)):
        fp.die("account %s cannot open site id %s (HTTP %s): it is not this account's site, "
               "or there is no such site" % (account, site_id, status),
               code=EXIT_ACCESS_DENIED, error_code="access_denied")
    if not isinstance(site, dict):
        fp.die("cannot read site id %s (HTTP %s): %s" % (site_id, status, fp.panel_error(data)))
    if (site.get("domain") or "").lower() != domain:
        fp.die("site id %s is %s, not %s — nothing changed"
               % (site_id, site.get("domain"), domain),
               code=EXIT_DOMAIN_MISMATCH, error_code="domain_mismatch")
    print("domain:  %s matches site id %s" % (domain, site_id))

    status, data = panel.call("GET", "/sites/backend/%s" % site_id)
    backend = data.get("data") if status == 200 and isinstance(data, dict) else None
    if status in DENIED:
        fp.die("account %s cannot read the backend of site id %s (HTTP %s)"
               % (account, site_id, status), code=EXIT_ACCESS_DENIED, error_code="access_denied")
    if not isinstance(backend, dict):
        fp.die("cannot read the backend of site id %s (HTTP %s): %s"
               % (site_id, status, fp.panel_error(data)))
    print("backend: %s; upstreams: %s" % (backend.get("type"), describe_upstreams(backend)))
    if manual_config(site, backend):
        fp.die("site %s has hand-edited nginx settings (manual_changes): a write from here "
               "would replace them — change the port by hand or reset the config in the panel"
               % domain, code=EXIT_MANUAL_CONFIG, error_code="manual_config")
    if backend.get("type") != "reverse_proxy":
        fp.die("site %s is not a reverse proxy (backend %s)" % (domain, backend.get("type")),
               code=EXIT_UPSTREAM_MISMATCH, error_code="upstream_mismatch")
    if backend.get("action") or site.get("action"):
        fp.die("site %s has a panel task in progress; run again when it is done" % domain,
               code=EXIT_APPLY_TIMEOUT, error_code="apply_timeout")

    old = host_upstreams(backend, args.from_port)
    new = host_upstreams(backend, args.port)
    if not old and len(new) == 1:
        print("unchanged: %s already proxies to %s, no upstream on port %s — nothing to do"
              % (domain, new[0].get("address"), args.from_port))
        print("ports:   %s -> %s; applied: yes (already)" % (args.from_port, args.port))
        return 0
    if len(old) != 1 or new:
        fp.die("site %s needs exactly one host upstream on port %s and none on %s; found %d and "
               "%d (upstreams: %s) — nothing changed" % (domain, args.from_port, args.port,
                                                        len(old), len(new), describe_upstreams(backend)),
               code=EXIT_UPSTREAM_MISMATCH, error_code="upstream_mismatch")
    old_addr = old[0].get("address")
    new_addr = with_port(old_addr, args.port)
    upstreams = []
    for u in backend.get("upstreams") or []:
        u = dict(u) if isinstance(u, dict) else u
        if isinstance(u, dict) and u.get("type") == "host" and address_port(u.get("address")) == args.from_port:
            u["address"] = new_addr
        upstreams.append(u)
    expected = [u.get("address") for u in upstreams if isinstance(u, dict)]
    print("replace: %s -> %s; other upstreams stay" % (old_addr, new_addr))
    if args.check:
        print("check:   ok — domain, site id and the one host upstream to replace confirmed; "
              "nothing written")
        return 0

    fp.announce(panel, "move site %s from port %s to %s" % (domain, args.from_port, args.port))
    status, data = put_backend(panel, site, backend, {
        "type": "reverse_proxy", "handler": None, "upstreams": upstreams})
    if status in DENIED:
        fp.die("account %s may not change the backend of site id %s (HTTP %s) — nothing changed"
               % (account, site_id, status), code=EXIT_ACCESS_DENIED, error_code="access_denied")
    if status >= 300:
        fp.die("writing the backend of %s failed (HTTP %s): %s"
               % (domain, status, fp.panel_error(data)))

    deadline = time.time() + args.wait
    after = None
    while True:
        after = proxy_state(panel, site_id)
        if (after and after.get("type") == "reverse_proxy" and not after.get("action")
                and upstream_addresses(after) == expected):
            break
        if time.time() > deadline:
            # Read the real state once more so the operator can decide whether to repeat.
            after = proxy_state(panel, site_id)
            if after is None:
                state = "unreadable"
            elif upstream_addresses(after) == expected:
                state = "written, panel task still pending" if after.get("action") else "applied"
            elif host_upstreams(after, args.from_port):
                state = "not written: still on port %s" % args.from_port
            else:
                state = "unexpected"
            print("state:   %s; upstreams: %s" % (state, describe_upstreams(after or {})))
            print("ports:   %s -> %s; applied: no (timeout %ss)" % (args.from_port, args.port, args.wait))
            fp.die("the panel did not report the change applied within %ss; state: %s. Repeating "
                   "the same command is safe" % (args.wait, state),
                   code=EXIT_APPLY_TIMEOUT, error_code="apply_timeout")
        time.sleep(3)
    status, data = panel.call("GET", "/sites/%s" % site_id)
    site_after = data.get("data") if status == 200 and isinstance(data, dict) else {}
    print("re-read: domain %s, site id %s, upstreams: %s"
          % ((site_after or {}).get("domain"), site_id, describe_upstreams(after)))
    print("ports:   %s -> %s; applied: yes" % (args.from_port, args.port))
    return 0


def cmd_backend(args):
    """Switch an existing site's php handler and/or version, or turn it into a reverse proxy."""
    domain = args.domain.strip().lower().rstrip(".")
    panel = fp.panel_for(args)
    site_id = fp.find_site_id(panel, domain)
    if args.proxy is not None and (args.handler or args.php):
        fp.die("--proxy takes no --php or --handler")
    fp.announce(panel, "switch site %s to a reverse proxy on port %s" % (domain, args.proxy)
                if args.proxy is not None else "switch the php backend of site %s" % domain)
    site = wait_for_site(panel, domain, site_id)
    current = read_backend(panel, site_id)
    cur_type = current.get("type")
    cur_handler, cur_php = current.get("handler"), str(current.get("handler_version") or "")
    now = "%s, %s" % (php_label(cur_php), cur_handler) if cur_type == "php" else (
        "reverse proxy to %s" % ", ".join(upstream_addresses(current))
        if cur_type == "reverse_proxy" else cur_type)
    print("now:     %s" % now, file=sys.stderr)
    if args.proxy is not None:
        return switch_to_proxy(panel, site, current, now, args.proxy)

    versions = php_versions(panel_settings(panel))
    by_version = {v: h for v, h, _ in versions}
    dv = default_version(versions)
    interactive = sys.stdin.isatty() and not args.no_prompt
    if not args.handler and not args.php:
        if not interactive:
            fp.die("say what to switch to: --handler and/or --php")
        itk = dv if "mpm_itk" in by_version.get(dv, []) else None
        options = [("mpm_itk", "Apache module (mpm_itk), %s" % php_label(itk))] if itk else []
        options.append(("fcgi", "FastCGI (fcgi), choose the version"))
        args.handler = pick("PHP mode:", options, options[0][0])
        if args.handler == "mpm_itk":
            args.php = itk
    # Whatever is not asked for stays as it is, when the new pair allows it.
    if not args.php and cur_type == "php" and args.handler in by_version.get(cur_php, []):
        args.php = cur_php
    if not args.handler and cur_type == "php" and cur_handler in by_version.get(
            (args.php or "").replace(".", ""), []):
        args.handler = cur_handler
    resolve_php(args, versions, by_version, dv, interactive)

    if cur_type == "php" and (args.handler, args.php) == (cur_handler, cur_php):
        print("unchanged: %s already runs %s — nothing changed" % (site.get("domain"), now))
        return 0
    status, data = put_backend(panel, site, current, {
        "type": "php", "handler": args.handler, "handler_version": args.php})
    if status >= 300:
        fp.die("switching %s failed (HTTP %s): %s"
               % (site.get("domain"), status, fp.panel_error(data)))
    # The switch is queued and the site's action flag shows up late: wait for the
    # backend itself to report the new pair.
    deadline = time.time() + 180
    while True:
        after = read_backend(panel, site_id)
        if (after.get("handler"), str(after.get("handler_version") or "")) == (
                args.handler, args.php) and not after.get("action"):
            break
        if time.time() > deadline:
            fp.die("switch of %s was submitted but not applied within 180s — check the panel"
                   % site.get("domain"))
        time.sleep(3)
    print("switched: %s (site id %s)" % (site.get("domain"), site_id))
    print("backend: %s, %s (was %s)" % (php_label(str(after.get("handler_version") or "")),
                                        after.get("handler"), now))
    return 0


def cmd_add(args):
    domain = args.domain.strip().lower().rstrip(".")
    panel = fp.panel_for(args)

    for site in fp.all_sites(panel):
        if domain in fp.site_names(site):
            print("exists: %s (site id %s, main domain %s) — nothing changed"
                  % (domain, site.get("id"), site.get("domain")))
            return 0

    owner_id = fp.resolve_owner(panel, args.owner)
    resolve_choices(args, panel)
    fp.announce(panel, "create site %s (%s)" % (domain, backend_label(args)), owner_id)

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
        "owner": owner_id,
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
        change = {"type": args.kind, "handler": None}
        if args.kind == "reverse_proxy":
            change["upstreams"] = [{"type": "host", "address": args.proxy}]
        status, data = put_backend(panel, site, read_backend(panel, site_id), change)
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


def main_domain_site(panel, name):
    """The site whose main domain is exactly this name. An alias does not count:
    deleting by alias would take down a site the user did not name."""
    try:
        domain = name.strip().lower().rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        fp.die("%r is not a valid domain name" % name)
    by_alias = None
    for site in fp.all_sites(panel):
        if site.get("domain") == domain:
            return site
        if domain in fp.site_names(site):
            by_alias = site
    if by_alias:
        fp.die("%s is an alias of site %s (id %s), not a site of its own — name the site by "
               "its main domain" % (domain, by_alias.get("domain"), by_alias.get("id")))
    fp.die("site %s not found under this panel account" % domain)


def site_resources(panel, site):
    """GET /api/sites/<id>/resources — what the panel's "delete site" dialog lists."""
    status, data = panel.call("GET", "/sites/%s/resources" % site["id"])
    if status != 200:
        fp.die("cannot read the resources of site %s (id %s, HTTP %s): %s"
               % (site.get("domain"), site["id"], status, fp.panel_error(data)))
    res = data.get("data") if isinstance(data, dict) and "data" in data else data
    return res if isinstance(res, dict) else {"resources": res}


def resource_name(item):
    if not isinstance(item, dict):
        return str(item)
    for key in ("name", "domain", "login", "address"):
        if item.get(key):
            return "%s (id %s)" % (item[key], item.get("id", "?"))
    return "id %s" % item.get("id", "?")


def print_resources(res):
    for key in sorted(res):
        val = res[key]
        if isinstance(val, list):
            print("%-16s %s" % (key + ":", ", ".join(resource_name(i) for i in val) or "—"))
        else:
            print("%-16s %s" % (key + ":", val if val not in (None, "") else "—"))


def cmd_resources(args):
    panel = fp.panel_for(args)
    site = main_domain_site(panel, args.domain)
    print("site:            %s (id %s)" % (site.get("domain"), site.get("id")))
    print("root:            %s" % site.get("index_dir", "?"))
    print_resources(site_resources(panel, site))
    return 0


def delete_code(site_id, res, body):
    """Ties a real delete to the dry run the user was shown: the code changes
    when the attached resources or the ticks do."""
    seen = json.dumps([site_id, res, body], sort_keys=True, default=str)
    return hashlib.sha256(seen.encode()).hexdigest()[:8]


def cmd_delete(args):
    """The "delete" button of a site, with the ticks of its dialog. Not undoable."""
    panel = fp.panel_for(args)
    site = main_domain_site(panel, args.domain)
    domain, site_id = site.get("domain"), site["id"]
    dns = args.dns or args.dns_provider
    removed = ["the site with its files"]
    removed += ["its databases"] if args.databases else []
    removed += ["its DNS domains" + (" and their zones at the DNS provider"
                                     if args.dns_provider else "")] if dns else []
    removed += ["its mail domains"] if args.mail else []
    removed += ["its subdomain sites"] if args.subdomains else []

    fp.announce(panel, "delete site %s (id %s): %s" % (domain, site_id, ", ".join(removed)),
                fp.owner_id_of(site))
    body = {
        "remove_databases": args.databases,
        "remove_dns_domains": dns,
        "remove_email_domains": args.mail,
        "remove_sub_domains": args.subdomains,
        "remove_dns_domains_from_provider": args.dns_provider,
    }
    res = site_resources(panel, site)
    print("root:            %s" % site.get("index_dir", "?"))
    print_resources(res)
    code = delete_code(site_id, res, body)
    if not args.confirm:
        print("dry run: nothing deleted. Show the lines above to the user; once they agree, "
              "repeat the same command with --confirm %s" % code)
        return 0
    if args.confirm != code:
        fp.die("--confirm %s does not match: the site's attached resources or the chosen "
               "options differ from the dry run it came from. Run without --confirm again, "
               "show the output to the user and ask" % args.confirm)

    status, data = panel.call("PUT", "/sites/%s/delete" % site_id, body)
    if status >= 300:
        fp.die("delete of %s failed (HTTP %s): %s" % (domain, status, fp.panel_error(data)))

    # Deletion is queued: wait for the site to leave the list.
    deadline = time.time() + args.wait
    while time.time() < deadline:
        status, data = panel.call("GET", "/sites/simple")
        if status == 200 and not any(s.get("id") == site_id for s in fp.unwrap(data)):
            print("deleted: site %s (id %s): %s" % (domain, site_id, ", ".join(removed)))
            return 0
        time.sleep(3)
    fp.die("delete of %s was submitted but the site is still listed after %ds — check the panel"
           % (domain, args.wait))


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

    p = sub.add_parser("backend", help="switch a site's php handler and/or version, "
                       "or turn it into a reverse proxy")
    p.add_argument("domain")
    p.add_argument("--handler", help="mpm_itk (apache module) or fcgi (fastcgi); "
                   "php_fpm and cgi too, if the version supports them")
    p.add_argument("--php", metavar="VER", help="php version as the panel lists it: 83, 8.3, 56 ...")
    p.add_argument("--proxy", type=int, metavar="PORT",
                   help="turn the site into a reverse proxy to http://localhost:PORT; "
                        "the site and its files stay")
    p.add_argument("--no-prompt", action="store_true", help="never ask on a terminal")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_backend)

    p = sub.add_parser("proxy-port", help="move one reverse-proxy upstream of a site from one "
                       "local port to another; other upstreams stay")
    p.add_argument("domain", help="main domain of the site; must match --site-id")
    p.add_argument("--site-id", type=int, required=True, metavar="ID", help="panel site id")
    p.add_argument("--from-port", type=int, required=True, metavar="PORT",
                   help="the port the one host upstream to replace uses now")
    p.add_argument("--port", type=int, required=True, metavar="PORT", help="the new port")
    p.add_argument("--check", action="store_true",
                   help="only verify domain, id and the upstream to replace; write nothing")
    p.add_argument("--wait", type=int, default=180,
                   help="seconds to wait for the panel to apply the change (default: 180)")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_proxy_port)

    p = sub.add_parser("ssl", help="(re)issue let's encrypt for a site once its dns is in place")
    p.add_argument("domain")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_ssl)

    p = sub.add_parser("resources", help="what is attached to a site: databases, dns, mail ...")
    p.add_argument("domain")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_resources)

    p = sub.add_parser("delete", help="delete a site; without --confirm only shows what would go")
    p.add_argument("domain", help="main domain of the site, not an alias")
    p.add_argument("--databases", action="store_true", help="delete the site's databases too")
    p.add_argument("--dns", action="store_true", help="delete the site's DNS domains too")
    p.add_argument("--dns-provider", action="store_true",
                   help="delete the DNS domains and their zones at the DNS provider")
    p.add_argument("--mail", action="store_true", help="delete the site's mail domains too")
    p.add_argument("--subdomains", action="store_true", help="delete the site's subdomain sites too")
    p.add_argument("--confirm", metavar="CODE",
                   help="really delete: the code printed by the same command without --confirm")
    p.add_argument("--wait", type=int, default=180,
                   help="seconds to wait for the site to disappear (default: 180)")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_delete)

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
    fp.add_owner_arg(p)
    p.set_defaults(func=cmd_add)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    fp.run(main)
