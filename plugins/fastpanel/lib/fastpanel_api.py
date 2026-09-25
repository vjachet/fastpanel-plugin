#!/usr/bin/env python3
"""Shared core for the fastpanel tools: config, login, http, small helpers.

Panel credentials never leave this process: they are read from a 0600 json
file, sent only to the panel's own /login endpoint, and scrubbed from every
error path. Nothing reaches stdout but status lines.

The config holds one panel url and an array of accounts; --account picks one.
Every tool in this plugin imports this module — see load_lib() in the scripts.
"""

import argparse
import base64
import json
import os
import re
import secrets
import ssl
import stat
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_DIR = os.environ.get(
    "FASTPANEL_CONFIG_DIR", os.path.expanduser("~/.config/fastpanel")
)
CONFIG_FILE = os.environ.get("FASTPANEL_CONFIG") or os.path.join(
    CONFIG_DIR, "config.json"
)
DB_OUT_DIR = os.path.join(CONFIG_DIR, "databases")
CACHE_DIR = os.path.expanduser("~/.cache/fastpanel")

PAGE = 100  # the panel's own ui pages the list this way
SECRETS = []  # values scrubbed from any output


def scrub(text):
    out = str(text)
    for s in SECRETS:
        if s:
            out = out.replace(s, "***")
    return out


def die(msg, code=1):
    print("ERROR: " + scrub(msg), file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# config — one panel url, an array of accounts
# --------------------------------------------------------------------------


SETUP_HINT = """Panel credentials go into one file: %(file)s (mode 600).

Create it yourself, in your own terminal — not through an agent:

  install -d -m 700 %(dir)s
  cat > %(file)s <<'JSON'
  {
    "url": "panel.example.com",
    "accounts": [
      {"login": "user1", "password": "secret1", "label": "what this account is"},
      {"login": "user2", "password": "secret2"}
    ]
  }
  JSON
  chmod 600 %(file)s

"url" is the panel host as you type it in the browser — just the host is enough
("panel.example.com"), https:// is assumed and the /api suffix is added by this
script, the same way for every panel. Add a port if your panel runs on one
("panel.example.com:8888").

One account is enough; add more objects to the array for more panel users or
more panels (an account may carry its own "url"). An account is referred to by
its login, or by "name" if you give it one. Set "insecure": true only if the
panel has a self-signed certificate.

Check it with the accounts tool:  fastpanel_accounts.py list""" % {
    "dir": os.path.dirname(CONFIG_FILE) or ".",
    "file": CONFIG_FILE,
}


def normalize_url(value):
    """Accept a bare host, a host:port or a full url; the /api suffix is ours."""
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if "//" not in url:
        url = "https://" + url.lstrip("/")
    if url.endswith("/api"):
        url = url[: -len("/api")]
    return url.rstrip("/")


def check_perms(path):
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        die(
            "%s is group/world accessible (mode %o). Run: chmod 600 %s"
            % (path, st.st_mode & 0o777, path)
        )
    if st.st_uid != os.getuid():
        die("%s is not owned by you" % path)


def load_config():
    """{"url":..., "insecure":..., "accounts": [{name, login, password, label}]}"""
    if not os.path.exists(CONFIG_FILE):
        die("no panel config at %s.\n\n%s" % (CONFIG_FILE, SETUP_HINT))
    check_perms(CONFIG_FILE)

    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except ValueError as exc:
        die("%s is not valid json: %s" % (CONFIG_FILE, exc))

    if not isinstance(raw, dict):
        die("%s must be a json object.\n\n%s" % (CONFIG_FILE, SETUP_HINT))
    url = normalize_url(raw.get("url"))
    accounts = raw.get("accounts")
    if not url and not any(isinstance(a, dict) and a.get("url") for a in accounts or []):
        die('"url" (the panel host) is missing from %s.\n\n%s' % (CONFIG_FILE, SETUP_HINT))
    if not isinstance(accounts, list) or not accounts:
        die('"accounts" must be a non-empty array in %s.\n\n%s' % (CONFIG_FILE, SETUP_HINT))

    clean = []
    for i, acc in enumerate(accounts):
        if not isinstance(acc, dict):
            die("accounts[%d] in %s is not an object" % (i, CONFIG_FILE))
        if acc.get("password"):
            SECRETS.append(acc["password"])  # scrub every password, not just the used one
        acc = dict(acc)
        acc["name"] = str(acc.get("name") or acc.get("login") or "account%d" % i)
        acc["url"] = normalize_url(acc.get("url")) or url  # per-account override, rarely needed
        acc["insecure"] = acc.get("insecure", raw.get("insecure", False))
        clean.append(acc)

    seen = set()
    for acc in clean:
        if acc["name"] in seen:
            die("two accounts in %s share the name %r" % (CONFIG_FILE, acc["name"]))
        seen.add(acc["name"])
    return clean


def resolve_account(requested):
    """Pick which account to use. Never guesses between two of them."""
    accounts = load_config()
    requested = requested or os.environ.get("FASTPANEL_ACCOUNT")

    if requested:
        for acc in accounts:
            if requested in (acc["name"], acc.get("login")):
                return acc
        die(
            "account %r is not in %s. Known accounts: %s"
            % (requested, CONFIG_FILE, ", ".join(a["name"] for a in accounts))
        )
    if len(accounts) == 1:
        return accounts[0]
    for acc in accounts:
        if acc["name"] == "default":
            return acc
    die(
        "several accounts configured, pick one with --account: %s"
        % ", ".join(a["name"] for a in accounts)
    )


def read_credentials(requested):
    acc = resolve_account(requested)
    missing = [k for k in ("login", "password") if not acc.get(k)]
    if missing:
        die("account %s in %s is missing: %s" % (acc["name"], CONFIG_FILE, ", ".join(missing)))
    return acc


def safe_name(name):
    """Account names become file names — logins may hold @ and dots."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def truthy(val):
    return str(val).strip().lower() in ("1", "yes", "true", "on")


def list_accounts():
    """Print account names and logins — never passwords."""
    accounts = load_config()
    width = max(len(a["name"]) for a in accounts)
    for acc in accounts:
        missing = [k for k in ("login", "password") if not acc.get(k)]
        detail = (
            "INCOMPLETE (missing %s)" % ", ".join(missing)
            if missing
            else "%s  %s" % (acc["url"], acc["login"])
        )
        note = acc.get("label") or ""
        print("%-*s  %s%s" % (width, acc["name"], detail, ("  — " + note) if note else ""))
    return 0


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------


class Panel:
    def __init__(self, cfg):
        self.base = cfg["url"]
        self.api = self.base + "/api"
        self.cfg = cfg
        self.token = None
        self.token_path = os.path.join(CACHE_DIR, "token-" + safe_name(cfg["name"]))
        if truthy(cfg.get("insecure", False)):
            self.ctx = ssl._create_unverified_context()
        else:
            self.ctx = ssl.create_default_context()

    def _request(self, method, url, body=None, token=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("language", "ru")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"message": raw[:500]}
        except urllib.error.URLError as exc:
            die("cannot reach %s: %s" % (self.base, scrub(exc.reason)))

    # -- auth ------------------------------------------------------------

    def _login(self):
        status, body = self._request(
            "POST",
            self.base + "/login",
            {"username": self.cfg["login"], "password": self.cfg["password"]},
        )
        token = (body.get("data") or {}).get("token")
        if not token:
            die(
                "login failed for account %s (HTTP %s): %s"
                % (
                    self.cfg["name"],
                    status,
                    body.get("message") or "no token in response",
                )
            )
        self._cache_token(token)
        return token

    def _cache_token(self, token):
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)

    def _cached_token(self):
        try:
            st = os.stat(self.token_path)
        except OSError:
            return None
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None
        with open(self.token_path, encoding="utf-8") as fh:
            token = fh.read().strip()
        return token if token and not jwt_expired(token) else None

    def auth(self, force=False):
        self.token = None if force else self._cached_token()
        if not self.token:
            self.token = self._login()
        return self.token

    def call(self, method, path, body=None):
        """Authenticated call; one transparent re-login on 401."""
        if not self.token:
            self.auth()
        url = self.api + path
        status, data = self._request(method, url, body, self.token)
        if status == 401:
            self.auth(force=True)
            status, data = self._request(method, url, body, self.token)
        return status, data


def jwt_payload(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def jwt_expired(token, skew=60):
    exp = jwt_payload(token).get("exp")
    return bool(exp) and time.time() + skew >= float(exp)


# --------------------------------------------------------------------------
# panel objects
# --------------------------------------------------------------------------


def panel_error(data):
    """The panel reports failures either as {"message": ...} or as
    {"errors": {"<field>": "<text>"}}."""
    if not isinstance(data, dict):
        return str(data)
    errors = data.get("errors")
    if isinstance(errors, dict) and errors:
        parts = []
        for field, text in errors.items():
            if isinstance(text, (list, tuple)):
                text = "; ".join(str(t) for t in text)
            parts.append("%s: %s" % (field, text))
        return ", ".join(parts)
    if isinstance(errors, list) and errors:
        return "; ".join(str(e) for e in errors)
    return data.get("message") or json.dumps(data, ensure_ascii=False)[:500]


def says_already_exists(data):
    text = panel_error(data).lower()
    return "уже существует" in text or "already exists" in text


def unwrap(data):
    """Panel answers are either {"data": [...]} or a bare list."""
    inner = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(inner, dict) and isinstance(inner.get("data"), list):
        inner = inner["data"]
    return inner if isinstance(inner, list) else [inner] if inner else []


def own_login(panel):
    """The panel puts the login (not a number) in the token's `id` claim."""
    claims = jwt_payload(panel.token)
    for key in ("id", "username", "login", "sub"):
        val = claims.get(key)
        if isinstance(val, str) and val and not val.isdigit():
            return val
    return panel.cfg["login"]


def own_user_id(panel):
    """Numeric id of the panel user we are logged in as.

    The token carries the login, not the id (claim "id" is a string). GET /api/me
    answers directly; older panels without it fall back to matching ourselves in
    the user list: a non-admin sees only their own row there.
    """
    status, data = panel.call("GET", "/me")
    if status == 200:
        me = data.get("data") if isinstance(data, dict) else None
        if isinstance(me, dict) and isinstance(me.get("id"), int):
            return me["id"]

    login = own_login(panel)
    status, data = panel.call("GET", "/users")
    if status == 200:
        for user in unwrap(data):
            if not isinstance(user, dict):
                continue
            names = {user.get("username"), user.get("login"), user.get("email")}
            if login in names and isinstance(user.get("id"), int):
                return user["id"]

    claims = jwt_payload(panel.token)
    for key in ("user_id", "uid", "id"):
        val = claims.get(key)
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    die("cannot determine the panel user id for login %s" % login)


def all_sites(panel):
    """GET /api/sites/list — sites use their own filter[...] paging."""
    out, offset = [], 0
    while True:
        status, data = panel.call(
            "GET",
            "/sites/list?filter[type]=all&filter[order]=date"
            "&filter[offset]=%d&filter[limit]=%d" % (offset, PAGE),
        )
        if status != 200:
            die("cannot list sites (HTTP %s): %s" % (status, panel_error(data)))
        page = unwrap(data)
        out.extend(page)
        if len(page) < PAGE:
            return out
        offset += PAGE


def site_names(site):
    names = {site.get("domain"), site.get("main_domain")}
    for alias in site.get("aliases") or []:
        names.add((alias.get("name") or alias.get("domain")) if isinstance(alias, dict) else alias)
    return {n for n in names if n}


def find_site_id(panel, domain):
    # The panel's own "new database" form reads this short list: id + domain.
    status, data = panel.call("GET", "/sites/simple")
    if status == 200:
        for site in unwrap(data):
            if site.get("domain") == domain:
                return site.get("id")
    # Not the main domain of any site — look through aliases in the full list.
    for site in all_sites(panel):
        if domain in site_names(site):
            return site.get("id")
    die("site %s not found under this panel account" % domain)


def gen_password(length=24):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --------------------------------------------------------------------------
# cli plumbing shared by every tool
# --------------------------------------------------------------------------


def add_account_arg(parser):
    parser.add_argument("--account", "-A", metavar="NAME",
                        help="which configured panel account to use")
    return parser


def panel_for(args):
    """Resolve the account, log in, hand back a ready Panel."""
    cfg = read_credentials(getattr(args, "account", None))
    panel = Panel(cfg)
    panel.auth()
    return panel


def run(main):
    """Entry point wrapper: no traceback ever carries a password out."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        die("%s: %s" % (type(exc).__name__, exc))
