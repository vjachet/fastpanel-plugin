#!/usr/bin/env python3
"""DNS domains of a panel account: the DNS accounts (external providers) the
panel knows, the DNS domains it holds, adding one, synchronizing its records and
deleting one.

"add" is idempotent: a DNS domain of that name is left alone. A DNS domain is
tied to a site and takes its owner from it — the panel is not told an owner
separately. With --no-site the site is left empty, as in the panel's own form.
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

DNS_ACCOUNT_NEEDED = 5  # exit code: several DNS accounts on the panel, none was named


# -- panel objects ---------------------------------------------------------


def dns_accounts(panel):
    """GET /api/dns/account — what the panel's own "new domain" form offers."""
    status, data = panel.call("GET", "/dns/account")
    if status != 200:
        fp.die("cannot list DNS accounts (HTTP %s): %s" % (status, fp.panel_error(data)))
    return [a for a in fp.unwrap(data) if isinstance(a, dict)]


def dns_domains(panel):
    status, data = panel.call("GET", "/dns/domains")
    if status != 200:
        fp.die("cannot list DNS domains (HTTP %s): %s" % (status, fp.panel_error(data)))
    return [d for d in fp.unwrap(data) if isinstance(d, dict)]


def find_dns_domain(panel, name):
    for dom in dns_domains(panel):
        if dom.get("name") == name:
            return dom
    return None


def account_label(dom):
    acc = dom.get("dns_account")
    if isinstance(acc, dict) and acc.get("name"):
        return "%s (%s, id %s)" % (acc.get("name"), acc.get("type", "?"), acc.get("id", "?"))
    return "local dns server" if not dom.get("is_delegated") else "id %s" % dom.get("delegated_to")


def account_lines(accounts):
    return ["id %-4s %-10s %s" % (a.get("id", "?"), a.get("type", "?"), a.get("name", "?"))
            for a in sorted(accounts, key=lambda a: a.get("id") or 0)]


def resolve_dns_account(panel, requested):
    """The DNS account the new domain goes to. Never guesses between two."""
    accounts = dns_accounts(panel)
    if not accounts:
        fp.die("no DNS accounts on this panel — the user adds one in the panel: "
               "Settings → DNS accounts")
    if requested:
        wanted = str(requested).strip()
        found = [a for a in accounts if wanted in (str(a.get("id")), a.get("name"))]
        if len(found) != 1:
            fp.die("DNS account %r is %s on this panel. DNS accounts:\n  %s"
                   % (requested, "ambiguous" if found else "not found",
                      "\n  ".join(account_lines(accounts))))
        return found[0]
    if len(accounts) == 1:
        return accounts[0]
    print("ERROR: several DNS accounts on this panel — say which one with --dns-account "
          "(name or id).", file=sys.stderr)
    print("Ask the user which one; do not pick one yourself. DNS accounts:", file=sys.stderr)
    for line in account_lines(accounts):
        print("  " + line, file=sys.stderr)
    sys.exit(DNS_ACCOUNT_NEEDED)


def site_detail(panel, domain):
    """The site a DNS domain is tied to: its id, owner and ips."""
    site_id = fp.find_site_id(panel, domain)
    status, data = panel.call("GET", "/sites/%s" % site_id)
    site = data.get("data") if status == 200 and isinstance(data, dict) else None
    if not isinstance(site, dict):
        fp.die("cannot read site %s (id %s, HTTP %s): %s"
               % (domain, site_id, status, fp.panel_error(data)))
    return site


def ascii_domain(name):
    """The panel keeps names in punycode: академия.рф is xn--... there."""
    name = name.strip().lower().rstrip(".")
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        fp.die("%r is not a valid domain name" % name)


def ip_value(item):
    return str(item.get("ip") or item.get("value")) if isinstance(item, dict) else str(item)


def wait_for_dns_domain(panel, name, timeout):
    """Creation is queued: wait for the domain to show up and settle."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        dom = find_dns_domain(panel, name)
        if dom and not dom.get("action"):
            return dom
        time.sleep(2)
    return None


# -- commands --------------------------------------------------------------


def cmd_accounts(args):
    panel = fp.panel_for(args)
    accounts = dns_accounts(panel)
    if not accounts:
        print("no DNS accounts on this panel")
        return 0
    for line in account_lines(accounts):
        print(line)
    return 0


def cmd_list(args):
    panel = fp.panel_for(args)
    rows = dns_domains(panel)
    if not rows:
        print("no DNS domains visible to account %s" % panel.cfg["name"])
        return 0
    width = max(len(str(d.get("name", ""))) for d in rows)
    for dom in sorted(rows, key=lambda d: str(d.get("name", ""))):
        flags = []
        if dom.get("mail_to"):
            flags.append("mail:%s" % dom["mail_to"])
        if not dom.get("enabled", True):
            flags.append("disabled")
        if dom.get("action"):
            flags.append("busy:%s" % dom["action"])
        print("%-*s  id %-5s site id %-5s %s  %s"
              % (width, dom.get("name", "?"), dom.get("id", "?"),
                 dom.get("virtualhost_id") or "—", account_label(dom), " ".join(flags)))
    return 0


def cmd_add(args):
    domain = ascii_domain(args.domain)
    panel = fp.panel_for(args)

    existing = find_dns_domain(panel, domain)
    if existing:
        print("exists: DNS domain %s (id %s) on %s — nothing changed"
              % (domain, existing.get("id"), account_label(existing)))
        return 0

    if args.no_site and args.site:
        fp.die("--no-site and --site contradict each other")
    account = resolve_dns_account(panel, args.dns_account)
    if args.no_site:
        # The site field of the panel's form is left empty: no site, no owner taken from it.
        site, site_domain, owner_id = None, None, None
        ips = args.ip or []
        if not ips:
            fp.die("without a site there is no ip to take — name it with --ip")
    else:
        site_domain = ascii_domain(args.site or domain)
        site = site_detail(panel, site_domain)
        owner_id = fp.owner_id_of(site)
        ips = args.ip or [ip_value(i) for i in site.get("ips") or []]
        if not ips:
            fp.die("site %s reports no ip addresses — name them with --ip" % site_domain)
    site_label = "site %s" % site_domain if site else "no site"

    fp.announce(panel, "create DNS domain %s on DNS account %s (%s), %s, ip %s"
                % (domain, account.get("name"), account.get("type"), site_label,
                   ", ".join(ips)), owner_id)

    body = {
        "ips": [{"ip": ip} for ip in ips],
        "name": domain,
        "delegated_to": account["id"],
        "source": "client",
        "default": False,
    }
    if site:
        body["site"] = site["id"]
    status, data = panel.call("POST", "/dns/domains", body)
    if status >= 300:
        if fp.says_already_exists(data):
            fp.die("DNS domain %s already exists on this panel under another panel user — "
                   "account %s cannot see it: %s"
                   % (domain, panel.cfg["name"], fp.panel_error(data)))
        fp.die("create failed (HTTP %s): %s" % (status, fp.panel_error(data)))

    dom = wait_for_dns_domain(panel, domain, args.wait)
    if not dom:
        fp.die("DNS domain %s was submitted but did not settle within %ds — check the panel"
               % (domain, args.wait))
    print("created: DNS domain %s (id %s) on %s, %s, ip %s"
          % (domain, dom.get("id"), account_label(dom),
             "site %s (id %s)" % (site_domain, site["id"]) if site else "no site",
             ", ".join(ip_value(i) for i in dom.get("ips") or []) or ", ".join(ips)))
    return 0


def dns_records(panel, dom):
    status, data = panel.call("GET", "/dns/domain/%s/records" % dom["id"])
    if status != 200:
        fp.die("cannot list the records of %s (HTTP %s): %s"
               % (dom.get("name"), status, fp.panel_error(data)))
    return [r for r in fp.unwrap(data) if isinstance(r, dict)]


def print_records(rows):
    width = max(len(str(r.get("name", ""))) for r in rows)
    for rec in sorted(rows, key=lambda r: (str(r.get("type", "")), str(r.get("name", "")))):
        extra = " ".join("%s=%s" % (k, rec[k]) for k in ("priority", "weight", "port")
                         if rec.get(k) not in (None, "", 0))
        print("%-6s %-*s  %s%s" % (rec.get("type", "?"), width, rec.get("name", "?"),
                                   rec.get("content", "?"), ("  " + extra) if extra else ""))


def cmd_records(args):
    domain = ascii_domain(args.domain)
    panel = fp.panel_for(args)
    dom = find_dns_domain(panel, domain)
    if not dom:
        fp.die("DNS domain %s not found under this panel account" % domain)
    rows = dns_records(panel, dom)
    if not rows:
        print("no records in DNS domain %s" % domain)
        return 0
    print_records(rows)
    return 0


def record_fqdn(name, domain):
    """A record name as the panel lists it: "test" in example.com is test.example.com."""
    name = name.strip().lower().rstrip(".")
    if name in ("", "@"):
        return domain
    return name if name == domain or name.endswith("." + domain) else "%s.%s" % (name, domain)


def find_record(rows, fqdn, rtype, content):
    for rec in rows:
        if (str(rec.get("type", "")).upper() == rtype
                and str(rec.get("name", "")).lower().rstrip(".") == fqdn
                and str(rec.get("content", "")).strip() == content):
            return rec
    return None


def cmd_record_add(args):
    """The "new record" form of a DNS domain's page."""
    domain = ascii_domain(args.domain)
    panel = fp.panel_for(args)
    rtype = args.type.upper()
    content = args.content.strip()

    dom = find_dns_domain(panel, domain)
    if not dom:
        fp.die("DNS domain %s not found under this panel account" % domain)
    if dom.get("action"):
        fp.die("DNS domain %s is busy (%s) — try again later" % (domain, dom["action"]))

    fqdn = record_fqdn(args.name, domain)
    if find_record(dns_records(panel, dom), fqdn, rtype, content):
        print("exists: %s %s %s in DNS domain %s — nothing changed"
              % (rtype, fqdn, content, domain))
        return 0

    body = {"name": args.name.strip(), "type": rtype, "content": content}
    for key in ("priority", "weight", "port"):
        if getattr(args, key) is not None:
            body[key] = getattr(args, key)
    extra = " ".join("%s=%s" % (k, body[k]) for k in ("priority", "weight", "port") if k in body)

    fp.announce(panel, "add record %s %s %s%s to DNS domain %s (id %s, %s)"
                % (rtype, fqdn, content, (" " + extra) if extra else "", domain,
                   dom.get("id"), account_label(dom)), fp.owner_id_of(dom))
    status, data = panel.call("POST", "/dns/domain/%s/records" % dom["id"], body)
    if status >= 300:
        fp.die("record was not added (HTTP %s): %s" % (status, fp.panel_error(data)))

    deadline = time.time() + args.wait
    while time.time() < deadline:
        settled = wait_for_dns_domain(panel, domain, max(1, int(deadline - time.time())))
        if settled and find_record(dns_records(panel, settled), fqdn, rtype, content):
            print("added: %s %s %s to DNS domain %s (id %s)"
                  % (rtype, fqdn, content, domain, dom.get("id")))
            return 0
        time.sleep(2)
    fp.die("record %s %s was submitted but is not listed after %ds — check the panel"
           % (rtype, fqdn, args.wait))


def cmd_record_edit(args):
    """The record form of a DNS domain's page, saved with a new content."""
    domain = ascii_domain(args.domain)
    panel = fp.panel_for(args)
    rtype = args.type.upper()
    new = args.to.strip()

    dom = find_dns_domain(panel, domain)
    if not dom:
        fp.die("DNS domain %s not found under this panel account" % domain)
    if dom.get("action"):
        fp.die("DNS domain %s is busy (%s) — try again later" % (domain, dom["action"]))

    fqdn = record_fqdn(args.name, domain)
    rows = dns_records(panel, dom)
    same = [r for r in rows if str(r.get("type", "")).upper() == rtype
            and str(r.get("name", "")).lower().rstrip(".") == fqdn]
    if not same:
        fp.die("no %s record named %s in DNS domain %s" % (rtype, fqdn, domain))
    if args.old is not None:
        rec = find_record(same, fqdn, rtype, args.old.strip())
        if not rec:
            fp.die("no %s record %s with content %r. It has:\n  %s"
                   % (rtype, fqdn, args.old, "\n  ".join(str(r.get("content")) for r in same)))
    elif len(same) > 1:
        fp.die("%d %s records named %s — say which one with --from CONTENT:\n  %s"
               % (len(same), rtype, fqdn, "\n  ".join(str(r.get("content")) for r in same)))
    else:
        rec = same[0]
    old = str(rec.get("content", "")).strip()
    if old == new and args.priority is None:
        print("unchanged: %s %s is already %s" % (rtype, fqdn, new))
        return 0
    if find_record(same, fqdn, rtype, new) and old != new:
        fp.die("a %s record %s with content %r is already there — not making a duplicate"
               % (rtype, fqdn, new))

    body = {"name": rec.get("name"), "type": rtype, "content": new}
    for key in ("priority", "weight", "port"):
        if rec.get(key) not in (None, "", 0):
            body[key] = rec[key]
    if args.priority is not None:
        body["priority"] = args.priority

    fp.announce(panel, "change record %s %s (record id %s) of DNS domain %s (id %s, %s): %s → %s"
                % (rtype, fqdn, rec.get("id"), domain, dom.get("id"), account_label(dom),
                   old, new), fp.owner_id_of(dom))
    status, data = panel.call("PUT", "/dns/domain/records/%s" % rec["id"], body)
    if status >= 300:
        fp.die("record was not changed (HTTP %s): %s" % (status, fp.panel_error(data)))

    deadline = time.time() + args.wait
    while time.time() < deadline:
        settled = wait_for_dns_domain(panel, domain, max(1, int(deadline - time.time())))
        if settled and find_record(dns_records(panel, settled), fqdn, rtype, new):
            print("changed: %s %s of DNS domain %s (id %s): %s → %s"
                  % (rtype, fqdn, domain, dom.get("id"), old, new))
            return 0
        time.sleep(2)
    fp.die("change of %s %s was submitted but the new content is not listed after %ds — "
           "check the panel" % (rtype, fqdn, args.wait))


def cmd_record_delete(args):
    """The "delete" button of a record on a DNS domain's page. Not undoable."""
    domain = ascii_domain(args.domain)
    panel = fp.panel_for(args)
    rtype = args.type.upper()

    dom = find_dns_domain(panel, domain)
    if not dom:
        fp.die("DNS domain %s not found under this panel account" % domain)
    if dom.get("action"):
        fp.die("DNS domain %s is busy (%s) — try again later" % (domain, dom["action"]))

    fqdn = record_fqdn(args.name, domain)
    same = [r for r in dns_records(panel, dom) if str(r.get("type", "")).upper() == rtype
            and str(r.get("name", "")).lower().rstrip(".") == fqdn]
    if not same:
        fp.die("no %s record named %s in DNS domain %s" % (rtype, fqdn, domain))
    if args.content is not None:
        rec = find_record(same, fqdn, rtype, args.content.strip())
        if not rec:
            fp.die("no %s record %s with content %r. It has:\n  %s"
                   % (rtype, fqdn, args.content,
                      "\n  ".join(str(r.get("content")) for r in same)))
    elif len(same) > 1:
        fp.die("%d %s records named %s — say which one with --content CONTENT:\n  %s"
               % (len(same), rtype, fqdn, "\n  ".join(str(r.get("content")) for r in same)))
    else:
        rec = same[0]
    content = str(rec.get("content", "")).strip()

    fp.announce(panel, "delete record %s %s (record id %s) of DNS domain %s (id %s, %s)"
                % (rtype, fqdn, rec.get("id"), domain, dom.get("id"), account_label(dom)),
                fp.owner_id_of(dom))
    print_records([rec])

    # The code is tied to this very record, so nothing but what was shown can go.
    seen = json.dumps([dom.get("id"), rec.get("id"), rtype, fqdn, content])
    code = hashlib.sha256(seen.encode()).hexdigest()[:8]
    if not args.confirm:
        print("dry run: nothing deleted. Show the lines above to the user; once they agree, "
              "repeat the same command with --confirm %s" % code)
        return 0
    if args.confirm != code:
        fp.die("--confirm %s does not match what is there now (expected %s) — the record "
               "changed since it was shown; show it to the user again" % (args.confirm, code))

    status, data = panel.call("DELETE", "/dns/domain/records/%s" % rec["id"])
    if status >= 300:
        fp.die("record was not deleted (HTTP %s): %s" % (status, fp.panel_error(data)))

    deadline = time.time() + args.wait
    while time.time() < deadline:
        settled = wait_for_dns_domain(panel, domain, max(1, int(deadline - time.time())))
        if settled and not any(r.get("id") == rec.get("id") for r in dns_records(panel, settled)):
            print("deleted: record %s %s %s of DNS domain %s (id %s)"
                  % (rtype, fqdn, content, domain, dom.get("id")))
            return 0
        time.sleep(2)
    fp.die("delete of %s %s was submitted but the record is still listed after %ds — "
           "check the panel" % (rtype, fqdn, args.wait))


def cmd_show(args):
    """One DNS domain as the panel lists it: every plain field, DNS account, owner."""
    domain = ascii_domain(args.domain)
    panel = fp.panel_for(args)
    dom = find_dns_domain(panel, domain)
    if not dom:
        fp.die("DNS domain %s not found under this panel account" % domain)
    owner_id = fp.owner_id_of(dom)
    if owner_id is not None:
        who = next((fp.user_label(u) for u in fp.panel_users(panel) if u.get("id") == owner_id),
                   "?")
        print("%-16s %s (user id %s)" % ("owner:", who, owner_id))
    print("%-16s %s" % ("dns account:", account_label(dom)))
    for key in sorted(dom):
        if key not in ("owner", "dns_account") and not isinstance(dom[key], (dict, list)):
            print("%-16s %s" % (key + ":", dom[key]))
    return 0


def cmd_mailto(args):
    """The mail service choice on a DNS domain's page: the panel rewrites the
    domain's mail records for that service."""
    domain = ascii_domain(args.domain)
    panel = fp.panel_for(args)

    dom = find_dns_domain(panel, domain)
    if not dom:
        fp.die("DNS domain %s not found under this panel account" % domain)
    if dom.get("action"):
        fp.die("DNS domain %s is busy (%s) — try again later" % (domain, dom["action"]))

    fp.announce(panel, "point the mail of DNS domain %s (id %s, %s) to %s"
                % (domain, dom.get("id"), account_label(dom), args.service),
                fp.owner_id_of(dom))
    status, data = panel.call("PUT", "/dns/domains/%s/mailto" % dom["id"],
                              {"mail_to": args.service})
    if status >= 300:
        fp.die("mail service change failed (HTTP %s): %s" % (status, fp.panel_error(data)))

    if not wait_for_dns_domain(panel, domain, args.wait):
        fp.die("mail service change of %s was submitted but did not finish within %ds — "
               "check the panel" % (domain, args.wait))
    print("mail of DNS domain %s (id %s) now goes to %s" % (domain, dom.get("id"), args.service))
    return 0


def cmd_sync(args):
    """The "synchronize" button of the panel's DNS domain list."""
    domain = ascii_domain(args.domain)
    panel = fp.panel_for(args)

    dom = find_dns_domain(panel, domain)
    if not dom:
        fp.die("DNS domain %s not found under this panel account" % domain)
    if dom.get("action"):
        fp.die("DNS domain %s is busy (%s) — try again later" % (domain, dom["action"]))

    fp.announce(panel, "synchronize the records of DNS domain %s (id %s) with %s"
                % (domain, dom.get("id"), account_label(dom)), fp.owner_id_of(dom))
    status, data = panel.call("PUT", "/dns/domains/%s/refresh" % dom["id"], {})
    if status >= 300:
        fp.die("synchronization failed (HTTP %s): %s" % (status, fp.panel_error(data)))

    if not wait_for_dns_domain(panel, domain, args.wait):
        fp.die("synchronization of %s was submitted but did not finish within %ds — "
               "check the panel" % (domain, args.wait))
    print("synchronized: DNS domain %s (id %s) with %s"
          % (domain, dom.get("id"), account_label(dom)))
    return 0


def cmd_delete(args):
    """The "delete" button of the panel's DNS domain list. Not undoable."""
    domain = ascii_domain(args.domain)
    panel = fp.panel_for(args)

    dom = find_dns_domain(panel, domain)
    if not dom:
        fp.die("DNS domain %s not found under this panel account" % domain)
    if dom.get("action"):
        fp.die("DNS domain %s is busy (%s) — try again later" % (domain, dom["action"]))

    where = ("from the panel and its zone from %s" % account_label(dom)
             if args.from_provider else
             "from the panel only, its zone stays on %s" % account_label(dom))
    fp.announce(panel, "delete DNS domain %s (id %s) %s" % (domain, dom.get("id"), where),
                fp.owner_id_of(dom))
    print("site id: %s" % (dom.get("virtualhost_id") or "—"))
    rows = dns_records(panel, dom)
    print("records: %d" % len(rows))
    if rows:
        print_records(rows)
    # Ties a real delete to the dry run the user was shown: the code changes
    # when the records or the choice about the provider do.
    seen = json.dumps([dom.get("id"), args.from_provider,
                       sorted((str(r.get("type")), str(r.get("name")), str(r.get("content")))
                              for r in rows)])
    code = hashlib.sha256(seen.encode()).hexdigest()[:8]
    if not args.confirm:
        print("dry run: nothing deleted. Show the lines above to the user; once they agree, "
              "repeat the same command with --confirm %s" % code)
        return 0
    if args.confirm != code:
        fp.die("--confirm %s does not match: the domain's records or the choice about the "
               "provider differ from the dry run it came from. Run without --confirm again, "
               "show the output to the user and ask" % args.confirm)

    status, data = panel.call("DELETE", "/dns/domains/%s?removeFromProvider=%s"
                              % (dom["id"], "true" if args.from_provider else "false"))
    if status >= 300:
        fp.die("delete of %s failed (HTTP %s): %s" % (domain, status, fp.panel_error(data)))

    # Deletion is queued: wait for the domain to leave the list.
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if not find_dns_domain(panel, domain):
            print("deleted: DNS domain %s (id %s) %s" % (domain, dom.get("id"), where))
            return 0
        time.sleep(2)
    fp.die("delete of %s was submitted but the domain is still listed after %ds — "
           "check the panel" % (domain, args.wait))


def main():
    ap = fp.argparse.ArgumentParser(description="FastPanel DNS domains of a panel account")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("accounts", help="DNS accounts (external providers) set up on the panel")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("list", help="DNS domains visible to this account")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="create a DNS domain if it does not exist yet")
    p.add_argument("domain", help="domain name")
    fp.add_account_arg(p)
    p.add_argument("--dns-account", metavar="NAME",
                   help="DNS account to put the domain on: name or id; required when the "
                        "panel has several")
    p.add_argument("--site", metavar="DOMAIN",
                   help="site the DNS domain is tied to (default: the site of the same name)")
    p.add_argument("--no-site", action="store_true",
                   help="leave the site empty: a DNS domain tied to no site (needs --ip)")
    p.add_argument("--ip", action="append", metavar="IP",
                   help="ip for the records, repeatable (default: the site's ips)")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the domain to appear (default: 60)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("records", help="records of a DNS domain as the panel holds them")
    p.add_argument("domain", help="domain name")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_records)

    p = sub.add_parser("record-add", help="add a record to a DNS domain if it is not there yet")
    p.add_argument("domain", help="domain name")
    p.add_argument("name", help="record name as typed in the panel's form, e.g. www")
    p.add_argument("type", help="record type: A, AAAA, CNAME, MX, TXT, ...")
    p.add_argument("content", help="record content")
    fp.add_account_arg(p)
    p.add_argument("--priority", type=int, help="priority (MX, SRV)")
    p.add_argument("--weight", type=int, help="weight (SRV)")
    p.add_argument("--port", type=int, help="port (SRV)")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the record to appear (default: 60)")
    p.set_defaults(func=cmd_record_add)

    p = sub.add_parser("record-edit", help="change the content of a DNS domain's record")
    p.add_argument("domain", help="domain name")
    p.add_argument("name", help="record name, e.g. www")
    p.add_argument("type", help="record type: A, AAAA, CNAME, MX, TXT, ...")
    p.add_argument("--to", required=True, metavar="CONTENT", help="new content")
    p.add_argument("--from", dest="old", metavar="CONTENT",
                   help="current content: which record, when several share the name and type")
    fp.add_account_arg(p)
    p.add_argument("--priority", type=int, help="new priority (MX, SRV)")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the change to appear (default: 60)")
    p.set_defaults(func=cmd_record_edit)

    p = sub.add_parser("record-delete",
                       help="show a record of a DNS domain; delete it with --confirm")
    p.add_argument("domain", help="domain name")
    p.add_argument("name", help="record name, e.g. www")
    p.add_argument("type", help="record type: A, AAAA, CNAME, MX, TXT, ...")
    p.add_argument("--content", metavar="CONTENT",
                   help="content: which record, when several share the name and type")
    fp.add_account_arg(p)
    p.add_argument("--confirm", metavar="CODE",
                   help="the code the dry run printed; without it nothing is deleted")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the record to disappear (default: 60)")
    p.set_defaults(func=cmd_record_delete)

    p = sub.add_parser("show", help="one DNS domain as the panel lists it")
    p.add_argument("domain", help="domain name")
    fp.add_account_arg(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("mailto", help="point a DNS domain's mail to a mail service")
    p.add_argument("domain", help="domain name")
    p.add_argument("service", help="mail service as the panel names it, e.g. google")
    fp.add_account_arg(p)
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the change to finish (default: 60)")
    p.set_defaults(func=cmd_mailto)

    p = sub.add_parser("sync", help="synchronize a DNS domain's records with its DNS account")
    p.add_argument("domain", help="domain name")
    fp.add_account_arg(p)
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the synchronization to finish (default: 60)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("delete", help="delete a DNS domain; without --confirm only shows what would go")
    p.add_argument("domain", help="domain name")
    fp.add_account_arg(p)
    p.add_argument("--from-provider", action="store_true",
                   help="delete the zone at the DNS provider too (default: panel only)")
    p.add_argument("--confirm", metavar="CODE",
                   help="really delete: the code printed by the same command without --confirm")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the domain to disappear (default: 60)")
    p.set_defaults(func=cmd_delete)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    fp.run(main)
