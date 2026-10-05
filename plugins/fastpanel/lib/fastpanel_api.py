#!/usr/bin/env python3
"""Shared core for the fastpanel tools: config, login, http, small helpers.

Panel credentials never leave this process: the login and the password are read
from a 0600 json file and sent only to the panel's own /login endpoint, the
token it returns is kept in a 0600 cache file. All three are scrubbed from
everything the process writes to stdout and stderr — including what the panel
itself reports (paths, owners), not just the error path.

The config holds one panel url and an array of accounts; --account picks one by
its "name". The login is a secret here and never serves as an identifier.

An account that sees several panel users (the administrator, login "fastpanel",
sees them all) must give whatever it creates an owner (--owner), and that owner
is never the administrator itself — see resolve_owner().
Every tool in this plugin imports this module — see load_lib() in the scripts.
"""

import argparse
import base64
import hashlib
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
SECRETS = []  # passwords and tokens: scrubbed from any output, exact match
LOGINS = []  # panel logins: scrubbed too, in any letter case

# The administrator's login is the same on every panel, so it is no secret —
# and scrubbing it would eat the word out of our own paths and script names.
ADMIN_LOGIN = "fastpanel"
OWNER_NEEDED = 4  # exit code: the account sees several panel users, the owner was not named


def scrub(text):
    out = str(text)
    for s in sorted(set(SECRETS), key=len, reverse=True):
        if s:
            out = out.replace(s, "***")
    for s in sorted(set(LOGINS), key=len, reverse=True):
        if s:
            out = re.sub(re.escape(s), "***", out, flags=re.IGNORECASE)
    return out


class _Scrubbed:
    """stdout/stderr stand-in: nothing leaves the process unscrubbed."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        return self._stream.write(scrub(text))

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._stream, name)


sys.stdout = _Scrubbed(sys.stdout)
sys.stderr = _Scrubbed(sys.stderr)


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
      {"login": "user2", "password": "secret2", "name": "second"}
    ]
  }
  JSON
  chmod 600 %(file)s

"url" is the panel host as you type it in the browser — just the host is enough
("panel.example.com"), https:// is assumed and the /api suffix is added by this
script, the same way for every panel. Add a port if your panel runs on one
("panel.example.com:8888").

One account is enough; add more objects to the array for more panel users or
more panels (an account may carry its own "url"). The login is never shown: an
account is referred to by a generated name like acc-1a2b3c4d (see the accounts
tool), or by "name" if you give it one — anything but the login. Set
"insecure": true only if the panel has a self-signed certificate.

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


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def name_salt():
    """Random bytes kept next to the config: without them a hashed account name
    could be checked against a guessed login."""
    path = os.path.join(os.path.dirname(CONFIG_FILE) or ".", "name-salt")
    try:
        with open(path, "rb") as fh:
            salt = fh.read()
        if salt:
            return salt
    except OSError:
        pass
    salt = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(salt)
    return salt


def hashed_name(acc):
    """Name for an account that has none: a stable hash of its panel and login.
    It stands in for the login everywhere — --account, output, file names."""
    key = "%s\n%s" % (acc.get("url") or "", acc.get("login") or "")
    return "acc-" + hashlib.sha256(name_salt() + key.encode("utf-8")).hexdigest()[:8]


def migrate_legacy(acc):
    """Accounts used to be named by their login, and so were their files. Move
    those under the current name, so no file name carries a login any more.
    Quiet on purpose: a failure here is retried on the next run."""
    old = _safe(str(acc.get("login") or ""))
    new = _safe(acc["name"])
    if not old or old == new:
        return
    try:
        os.remove(os.path.join(CACHE_DIR, "token-" + old))  # only a cache
    except OSError:
        pass
    old_dir, new_dir = os.path.join(DB_OUT_DIR, old), os.path.join(DB_OUT_DIR, new)
    if not os.path.isdir(old_dir) or os.path.islink(old_dir):
        return
    try:
        if not os.path.exists(new_dir):
            os.rename(old_dir, new_dir)
            return
        for entry in os.listdir(old_dir):
            target = os.path.join(new_dir, entry)
            n = 0
            while os.path.exists(target):  # never overwrite database credentials
                n += 1
                target = os.path.join(new_dir, "%s.old%d" % (entry, n))
            os.rename(os.path.join(old_dir, entry), target)
        os.rmdir(old_dir)
    except OSError:
        pass


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
        # scrub every login and password, not just the used one
        if acc.get("password"):
            SECRETS.append(str(acc["password"]))
        if acc.get("login") and not is_admin(acc):
            LOGINS.append(str(acc["login"]))
        acc = dict(acc)
        acc["name"] = str(acc.get("name") or "").strip()
        acc["url"] = normalize_url(acc.get("url")) or url  # per-account override, rarely needed
        acc["insecure"] = acc.get("insecure", raw.get("insecure", False))
        clean.append(acc)

    for i, acc in enumerate(clean):
        if not acc["name"]:
            acc["name"] = hashed_name(acc)
        if any(login.lower() in acc["name"].lower() for login in LOGINS):
            die('the "name" of accounts[%d] in %s contains a panel login. Give it another '
                'name yourself, in your own terminal.' % (i, CONFIG_FILE))

    seen = set()
    for acc in clean:
        if acc["name"] in seen:
            die("two accounts in %s share the name %r" % (CONFIG_FILE, acc["name"]))
        seen.add(acc["name"])
    for acc in clean:
        migrate_legacy(acc)
    return clean


def resolve_account(requested):
    """Pick which account to use. Never guesses between two of them."""
    accounts = load_config()
    requested = requested or os.environ.get("FASTPANEL_ACCOUNT")

    if requested:
        for acc in accounts:
            if requested == acc["name"]:
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


def is_admin(cfg):
    return str(cfg.get("login") or "").strip().lower() == ADMIN_LOGIN


def read_credentials(requested):
    acc = resolve_account(requested)
    missing = [k for k in ("login", "password") if not acc.get(k)]
    if missing:
        die("account %s in %s is missing: %s" % (acc["name"], CONFIG_FILE, ", ".join(missing)))
    return acc


def safe_name(name):
    """Account names become file names."""
    return _safe(name)


def truthy(val):
    return str(val).strip().lower() in ("1", "yes", "true", "on")


def list_accounts():
    """Print account names, panels and labels — never logins or passwords."""
    accounts = load_config()
    width = max(len(a["name"]) for a in accounts)
    for acc in accounts:
        missing = [k for k in ("login", "password") if not acc.get(k)]
        detail = (
            "INCOMPLETE (missing %s)" % ", ".join(missing)
            if missing
            else acc["url"]
        )
        if is_admin(acc):
            detail += "  [panel administrator]"
        note = acc.get("label") or ""
        print("%-*s  %s%s" % (width, acc["name"], detail, ("  — " + note) if note else ""))
    return 0


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the token (or a retried login) to wherever the
    answer points. The panel's api never redirects; if something does, stop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Panel:
    def __init__(self, cfg):
        self.base = cfg["url"]
        parts = urllib.parse.urlsplit(self.base)
        if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            die("the panel url of account %s is not https — the login and password would "
                "travel in the clear. Fix \"url\" in %s yourself." % (cfg["name"], CONFIG_FILE))
        self.api = self.base + "/api"
        self.cfg = cfg
        self.token = None
        self.token_path = os.path.join(CACHE_DIR, "token-" + safe_name(cfg["name"]))
        if truthy(cfg.get("insecure", False)):
            self.ctx = ssl._create_unverified_context()
        else:
            self.ctx = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.ctx), _NoRedirect
        )

    def _request(self, method, url, body=None, token=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("language", "ru")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with self.opener.open(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
            except OSError:
                raw = ""
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
        SECRETS.append(token)
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
        if token:
            SECRETS.append(token)
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
    die("cannot determine the panel user id of account %s" % panel.cfg["name"])


def panel_users(panel):
    """GET /api/users — everyone for the administrator, just ourselves otherwise."""
    status, data = panel.call("GET", "/users")
    if status != 200:
        die("cannot list panel users (HTTP %s): %s" % (status, panel_error(data)))
    return [u for u in unwrap(data) if isinstance(u, dict)]


def user_login(user):
    return str(user.get("username") or user.get("login") or "")


def is_admin_user(user):
    """The panel administrator, whatever its login is on this panel."""
    roles = user.get("roles") or []
    return user_login(user).lower() == ADMIN_LOGIN or any("ADMIN" in str(r).upper() for r in roles)


def user_label(user, names=None):
    """Login of a panel user, or the account name where the login is one of ours."""
    if names is None:
        names = {str(a.get("login") or "").lower(): a["name"] for a in load_config()}
    login = user_login(user)
    if login.lower() in names and login.lower() != ADMIN_LOGIN:
        return "account %s" % names[login.lower()]
    return login or "?"


def announce(panel, action, owner_id=None):
    """Say where this is about to happen before anything is touched: which
    panel server, under which account, for which panel user."""
    print("server:  %s" % panel.base)
    print("account: %s" % panel.cfg["name"])
    if owner_id is not None:
        who = next((user_label(u) for u in panel_users(panel) if u.get("id") == owner_id), "?")
        print("owner:   %s (user id %s)" % (who, owner_id))
    print("action:  %s" % action)
    sys.stdout.flush()


def user_lines(panel):
    """One line per panel user: id and login, or the account name where the
    login is one of ours — that one is scrubbed and cannot be typed anyway."""
    names = {str(a.get("login") or "").lower(): a["name"] for a in load_config()}
    out = []
    for user in sorted(panel_users(panel), key=lambda u: u.get("id") or 0):
        who = user_label(user, names)
        if is_admin_user(user):
            who += "  [panel administrator, cannot own anything]"
        out.append("id %-5s %s" % (user.get("id", "?"), who))
    return out


def resolve_owner(panel, requested):
    """Numeric id of the panel user the new site/database/... will belong to.

    An account that sees only itself owns what it creates, no questions asked.
    One that sees several panel users (the administrator does) must be told who
    the owner is — by panel login, user id or configured account name. The
    administrator itself never owns anything.
    """
    account = panel.cfg["name"]
    users = panel_users(panel)

    if not requested:
        if len(users) > 1:
            print("ERROR: account %s sees several panel users — say who will own this with "
                  "--owner (login, user id or account name)." % account, file=sys.stderr)
            print("Ask the user which panel user it is; do not pick one yourself. Panel users:",
                  file=sys.stderr)
            for line in user_lines(panel):
                print("  " + line, file=sys.stderr)
            sys.exit(OWNER_NEEDED)
        found = users[0] if users else {"id": own_user_id(panel)}
    else:
        wanted = str(requested).strip()
        for acc in load_config():  # a configured account name stands for its login
            if wanted == acc["name"] and acc.get("login"):
                wanted = str(acc["login"])
                break
        found = None
        for user in users:
            if wanted == user_login(user) or wanted == str(user.get("id")):
                found = user
                break
        if not found:
            die("panel user %r is not among those account %s sees. Panel users:\n  %s"
                % (requested, account, "\n  ".join(user_lines(panel))))

    if is_admin_user(found):
        die("nothing may be created under the panel administrator itself — "
            "pick an ordinary panel user for --owner")
    if not isinstance(found.get("id"), int):
        die("the panel gave no numeric id for the chosen owner")
    return found["id"]


def owner_id_of(obj):
    """Owner of a site or database as the panel reports it: a nested object,
    a bare id or an owner_id field."""
    owner = obj.get("owner")
    if isinstance(owner, dict):
        owner = owner.get("id")
    if owner is None:
        owner = obj.get("owner_id")
    if isinstance(owner, str) and owner.isdigit():
        owner = int(owner)
    return owner if isinstance(owner, int) else None


def site_of_owner(panel, domain, owner_id):
    """Id of the site with this domain — after making sure it belongs to owner_id.

    A database and the site it is attached to must have the same owner.
    """
    for site in all_sites(panel):
        if domain not in site_names(site):
            continue
        site_owner = owner_id_of(site)
        if site_owner is None:
            status, data = panel.call("GET", "/sites/%s" % site.get("id"))
            detail = data.get("data") if status == 200 and isinstance(data, dict) else None
            site_owner = owner_id_of(detail) if isinstance(detail, dict) else None
        if site_owner is None and len(panel_users(panel)) <= 1:
            site_owner = owner_id  # this account sees nobody's sites but its own
        if site_owner is None:
            die("cannot tell who owns site %s (id %s) — the panel does not say; "
                "not attaching a database to it" % (domain, site.get("id")))
        if site_owner != owner_id:
            die("site %s (id %s) belongs to panel user id %s, the database would belong to "
                "user id %s — they must have the same owner. Ask the user: another site or "
                "another owner." % (domain, site.get("id"), site_owner, owner_id))
        return site.get("id")
    die("site %s not found under this panel account" % domain)


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


def add_owner_arg(parser):
    parser.add_argument("--owner", metavar="USER",
                        help="panel user to own it: login, user id or account name; "
                             "required when the account sees several panel users")
    return parser


def panel_for(args):
    """Resolve the account, log in, hand back a ready Panel."""
    cfg = read_credentials(getattr(args, "account", None))
    panel = Panel(cfg)
    panel.auth()
    return panel


def run(main):
    """Entry point wrapper: no traceback ever carries a credential out."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        die("%s: %s" % (type(exc).__name__, exc))
