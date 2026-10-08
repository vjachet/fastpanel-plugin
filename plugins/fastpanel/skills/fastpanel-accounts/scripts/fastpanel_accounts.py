#!/usr/bin/env python3
"""Configured panel accounts, visible users, creation, and authorized SSH keys.

Configured credentials are scrubbed; new user credentials go only to a private file.
"""

import base64
import binascii
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import warnings
from urllib.parse import quote

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "lib")
)
import fastpanel_api as fp  # noqa: E402


def cmd_list(args):
    return fp.list_accounts()


def cmd_whoami(args):
    panel = fp.panel_for(args)
    uid = fp.own_user_id(panel)
    fp.emit({"user_id": uid})
    return 0


def cmd_users(args):
    panel = fp.panel_for(args)
    users = fp.panel_users(panel)
    fp.emit({"user_ids": [u["id"] for u in users],
             "owner_ids": [u["id"] for u in users if not fp.is_admin_user(u)]})
    return 0


def cmd_account_id(args):
    cfg = fp.resolve_account(args.account)
    fp.emit({"account_ids": [fp.hashed_name(cfg)]})
    return 0


def nonnegative_int(value):
    try:
        number = int(value)
    except ValueError:
        raise fp.argparse.ArgumentTypeError("must be a non-negative integer")
    if number < 0:
        raise fp.argparse.ArgumentTypeError("must be a non-negative integer")
    return number


def find_user(panel, username):
    return next((u for u in fp.panel_users(panel)
                 if fp.user_login(u).lower() == username.lower()), None)


def save_user_credentials(panel, username, password, request_id=None):
    """Persist before POST: a lost response must not lose the user's password.

    Use an opaque, exclusive filename; never overwrite earlier credentials or
    expose a new panel login through its filename.
    """
    out_dir = os.path.join(fp.CONFIG_DIR, "users", fp.safe_name(panel.cfg["name"]))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    fp.check_perms(out_dir)
    fd, path = tempfile.mkstemp(prefix="user-", suffix=".json", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        record = {"url": panel.base, "username": username, "password": password}
        if request_id is not None:
            record["request_id"] = request_id
        json.dump(record, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def valid_username(username):
    if not isinstance(username, str):
        fp.die("local user request must contain a username string")
    fp.LOGINS.append(username)
    username = username.strip()
    if not username or any(c.isspace() for c in username):
        fp.die("username must be non-empty and contain no whitespace")
    if username.lower() == fp.ADMIN_LOGIN:
        fp.die("add creates ordinary users, not the panel administrator")
    return username


def request_id_arg(value):
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise fp.argparse.ArgumentTypeError("expected a local request id (32 hex characters)")
    return value


def cmd_prepare_user(args):
    """Human-only local input: no username in command arguments or output."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        fp.die("prepare-user must run in your own interactive terminal, outside the agent")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        username = valid_username(getpass.getpass("New panel login (input hidden): "))
    out_dir = os.path.join(fp.CONFIG_DIR, "user-requests")
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    fp.check_perms(out_dir)
    request_id = secrets.token_hex(16)
    path = os.path.join(out_dir, request_id + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"username": username}, fh, ensure_ascii=False)
        fh.write("\n")
    fp.emit({"request_id": request_id})
    return 0


def request_username(request_id):
    request_id = request_id_arg(request_id)
    path = os.path.join(fp.CONFIG_DIR, "user-requests", request_id + ".json")
    fp.check_perms(path)
    with open(path, encoding="utf-8") as fh:
        request = json.load(fh)
    if not isinstance(request, dict):
        fp.die("local user request must be a JSON object")
    return valid_username(request.get("username"))


def cmd_add(args):
    username = request_username(args.request)
    panel = fp.panel_for(args)
    fp.LOGINS.append(username)
    existing = find_user(panel, username)
    if existing:
        fp.emit({"status": "exists", "user_id": existing["id"]})
        return 0

    password = fp.gen_password()
    fp.SECRETS.append(password)
    save_user_credentials(panel, username, password, request_id=args.request)
    body = {"username": username, "password": password,
            "roles": "ROLE_USER", "quota": args.quota}
    status, data = panel.call("POST", "/users", body)
    if not 200 <= status < 300:
        # Another request may have created the login between GET and POST.
        # Our generated password does not belong to that existing user.
        if fp.says_already_exists(data):
            fp.die("username already exists; no password was changed. Saved credentials "
                   "are from this attempt and must not be used for that user", code=3,
                   error_code="name_conflict")
        fp.die("create failed (HTTP %s): %s. Saved credentials are from this attempt; "
               "check the user list before retrying" % (status, fp.panel_error(data)),
               error_code="create_rejected")

    # The UI polls the queue and /users after POST. Confirm via /users rather
    # than assuming that a successful POST means the background job finished.
    deadline = time.monotonic() + args.wait
    while True:
        user = find_user(panel, username)
        if user and user.get("id") is not None and not user.get("action"):
            fp.emit({"status": "created", "user_id": user["id"]})
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fp.die("creation not confirmed after %ss; keep the saved credentials and "
                   "check users before retrying — the panel may still finish the job"
                   % args.wait, error_code="operation_unconfirmed")
        time.sleep(min(2, remaining))


def public_key(raw):
    """Validate a single OpenSSH public key and identify its key material.

    Comments are not part of the identity. Private keys and authorized_keys
    options are not accepted here; the panel checks the algorithm's full payload.
    """
    raw = raw.strip()
    if "\n" in raw or "\r" in raw or "PRIVATE KEY" in raw:
        raise ValueError("expected one OpenSSH public key line, not a private key")
    parts = raw.split(None, 2)
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise ValueError("expected an OpenSSH public key: type base64 [comment]")
    try:
        blob = base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("public key has invalid base64")
    if len(blob) < 4:
        raise ValueError("public key is truncated")
    size = int.from_bytes(blob[:4], "big")
    if size != len(parts[0]) or blob[4:4 + size] != parts[0].encode("ascii") or len(blob) <= 4 + size:
        raise ValueError("public key type does not match its encoded data")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return raw, blob, parts[2] if len(parts) > 2 else "", fingerprint


def ssh_user(panel, requested):
    """Accept only numeric ids; username resolution stays inside the process."""
    wanted = fp.user_id_arg(requested)
    matches = [u for u in fp.panel_users(panel) if wanted == u.get("id")]
    if len(matches) != 1 or not fp.user_login(matches[0]):
        fp.die("target user is missing or ambiguous on this panel; check users and use its id")
    return matches[0]


def auth_keys(panel, user):
    path = "/sshd/%s/auth_keys" % quote(fp.user_login(user), safe="")
    status, data = panel.call("GET", path)
    fp.remember_identities(data)
    if status != 200:
        fp.die("cannot list SSH keys (HTTP %s): %s" % (status, fp.panel_error(data)))
    keys = fp.unwrap(data)
    if any(not isinstance(k, dict) or not isinstance(k.get("raw_key"), str) for k in keys):
        fp.die("unexpected SSH key list format; inspect ssh-keys before retrying")
    return keys


def matching_key(keys, blob):
    for key in keys:
        raw = key.get("raw_key") if isinstance(key, dict) else key
        if not isinstance(raw, str):
            continue
        try:
            if public_key(raw)[1] == blob:
                return key
        except ValueError:
            continue
    return None


def cmd_ssh_keys(args):
    panel = fp.panel_for(args)
    user = ssh_user(panel, args.user)
    fp.emit({"user_id": user["id"], "key_ids": [k["id"] for k in auth_keys(panel, user)]})
    return 0


def ssh_user_detail(panel, user):
    status, data = panel.call("GET", "/users/%s" % user["id"])
    fp.remember_identities(data)
    rows = fp.unwrap(data)
    if status != 200 or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("id") != user["id"]:
        fp.die("cannot verify target user's SSH access (HTTP %s)" % status)
    return rows[0]


def require_ssh_access(user):
    if user.get("enabled") is not True:
        fp.die("target user is disabled or its state is unknown; SSH access is not confirmed",
               error_code="user_disabled" if user.get("enabled") is False else "state_unknown")
    if user.get("ssh_access") is not True:
        fp.die("target user's SSH access is disabled or unknown; enable SSH access in the panel first",
               error_code="ssh_disabled" if user.get("ssh_access") is False else "state_unknown")


def cmd_ssh_status(args):
    panel = fp.panel_for(args)
    user = ssh_user_detail(panel, ssh_user(panel, args.user))
    keys = auth_keys(panel, user)
    active = sum(k.get("enabled") is True and not k.get("action") and k.get("type") == 1
                 for k in keys)
    ready = user.get("enabled") is True and user.get("ssh_access") is True and bool(active)
    result = {"user_id": user["id"], "ssh_ready": ready}
    if not ready:
        result["reason"] = ("user_disabled" if user.get("enabled") is False else
                            "ssh_disabled" if user.get("ssh_access") is False else
                            "state_unknown" if user.get("enabled") is not True or user.get("ssh_access") is not True else
                            "no_active_key")
    fp.emit(result)
    return 0 if ready else 1


def cmd_ssh_add(args):
    raw = args.key
    if args.key_file:
        with open(os.path.expanduser(args.key_file), encoding="utf-8") as fh:
            raw = fh.read()
    try:
        raw, blob, comment, _ = public_key(raw)
    except ValueError as exc:
        fp.die(str(exc))
    panel = fp.panel_for(args)
    user = ssh_user_detail(panel, ssh_user(panel, args.user))
    require_ssh_access(user)
    existing = matching_key(auth_keys(panel, user), blob)
    if existing is not None:
        if existing.get("enabled") is not True or existing.get("action"):
            fp.die("the key already exists but is disabled or pending; check ssh-status and ssh-keys",
                   error_code="key_unavailable")
        fp.emit({"status": "exists", "user_id": user["id"]})
        return 0
    desc = args.desc if args.desc is not None else comment
    path = "/sshd/%s/auth_key" % quote(fp.user_login(user), safe="")
    status, data = panel.call("POST", path, {"type": 1, "raw_key": raw, "desc": desc})
    if not 200 <= status < 300:
        fp.die("SSH key add failed (HTTP %s): %s; check ssh-keys before retrying"
               % (status, fp.panel_error(data)), error_code="ssh_add_rejected")
    deadline = time.monotonic() + args.wait
    while True:
        key = matching_key(auth_keys(panel, user), blob)
        if key is not None and not key.get("action") and key.get("enabled") is True:
            require_ssh_access(ssh_user_detail(panel, user))
            fp.emit({"status": "added", "user_id": user["id"]})
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fp.die("SSH key add not confirmed after %ss; check ssh-keys before retrying "
                   "— the panel may still finish the job" % args.wait, error_code="operation_unconfirmed")
        time.sleep(min(2, remaining))


def main():
    ap = fp.argparse.ArgumentParser(description="FastPanel accounts configured for this machine")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="configured accounts: opaque ids only")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("account-id", help="local account selection: return its opaque id")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_account_id)

    p = sub.add_parser("whoami", help="log in and show who this account is on the panel")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("users", help="panel users this account sees: numeric ids only "
                       "(all of them under the panel administrator)")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("add", help="create an ordinary panel user idempotently; "
                       "save generated credentials to a local mode 600 file")
    fp.add_account_arg(p)
    p.add_argument("--request", required=True, metavar="REQUEST_ID", type=request_id_arg,
                   help="opaque id from prepare-user in your own terminal")
    p.add_argument("--quota", type=nonnegative_int, default=0,
                   help="non-negative quota value as sent by the panel UI (default: 0)")
    p.add_argument("--wait", type=nonnegative_int, default=60,
                   help="seconds to wait for the user to appear (default: 60)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("prepare-user", help="local terminal only: store a new login privately; print a request id")
    p.set_defaults(func=cmd_prepare_user)

    p = sub.add_parser("ssh-keys", help="list a panel user's authorized SSH keys")
    fp.add_account_arg(p)
    p.add_argument("user", metavar="USER_ID", type=fp.user_id_arg, help="numeric panel user id")
    p.set_defaults(func=cmd_ssh_keys)

    p = sub.add_parser("ssh-status", help="check the user, SSH access and active public keys in the panel")
    fp.add_account_arg(p)
    p.add_argument("user", metavar="USER_ID", type=fp.user_id_arg, help="numeric panel user id")
    p.set_defaults(func=cmd_ssh_status)

    p = sub.add_parser("ssh-add", help="add an OpenSSH public key to a panel user idempotently")
    fp.add_account_arg(p)
    p.add_argument("user", metavar="USER_ID", type=fp.user_id_arg, help="numeric panel user id")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--key-file", metavar="PATH", help="OpenSSH public key file (.pub)")
    source.add_argument("--key", metavar="PUBLIC_KEY", help="one OpenSSH public key line")
    p.add_argument("--desc", help="key description (default: public key comment)")
    p.add_argument("--wait", type=nonnegative_int, default=60,
                   help="seconds to wait for the key to appear (default: 60)")
    p.set_defaults(func=cmd_ssh_add)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    fp.run(main)
