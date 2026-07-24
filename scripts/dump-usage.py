#!/usr/bin/env python3
"""Dump what the gateway has persisted to Postgres, to see whether it is
capturing usage per user.

IMPORTANT - what Postgres does and does NOT hold. The gateway does NOT store
per-request token counts. It stores:

  spend             principal, period, cents    aggregate spend per user/period
                                                 (cents derived from tokens via
                                                 the model rate table)
  principal_emails  principal, email, name,      identity + the Okta groups
                    groups                        claim the gateway resolved
  spend_limits      the caps set via set-spend-limit.sh
  admin_audit       every admin API mutation

Raw per-request token/cost breakdown lives ONLY in AMP as metrics
(claude_code_token_usage etc.) - use scripts/diagnose-telemetry.sh for that.

So this dump answers two things precisely:
  1. Is the gateway metering usage per user at all?      -> `spend` populated
  2. Did it capture Okta groups for those users?          -> principal_emails.groups
     (a NULL/empty groups column here is the likely reason the Grafana
      dashboard's user_groups filter shows nothing)

Dependencies: botocore (ships with the AWS CLI - already on the box) and
pg8000. Install pg8000 offline from the repo's vendored wheels - no PyPI:
  pip install --no-index --find-links docker/db-admin/vendor pg8000

Connection reuses the gateway's own path: the app-user secret
(<NAME_PREFIX>/db-app-user, {host,port,dbname,username,password}) and TLS
verify-full against the RDS CA. Read-only - only SELECTs.

Env:
  NAME_PREFIX            (required; from deploy.env)
  AWS_REGION             (default us-gov-west-1)
  RDS_CA_BUNDLE          path to the RDS CA PEM; auto-fetched if unset and absent
  DUMP_LIMIT             max rows per table (default 50)
"""
import json
import os
import ssl
import sys
import urllib.request

REGION = os.environ.get("AWS_REGION", "us-gov-west-1")
NAME_PREFIX = os.environ.get("NAME_PREFIX", "").strip()
LIMIT = int(os.environ.get("DUMP_LIMIT", "50"))
CA_URL = ("https://truststore.pki.%s.rds.amazonaws.com/global/global-bundle.pem"
          % REGION)

if not NAME_PREFIX:
    sys.exit("FATAL: NAME_PREFIX is not set (source scripts/deploy.env)")

try:
    import botocore.session          # ships with the AWS CLI - no separate install
    import pg8000.native             # vendored in docker/db-admin/vendor/*.whl
except ImportError as e:
    sys.exit("FATAL: needs botocore (comes with the AWS CLI) + pg8000. Install "
             "pg8000 offline from the repo's vendored wheels:\n"
             "  pip install --no-index --find-links docker/db-admin/vendor pg8000\n"
             "missing: %s" % e)


def _rds_ca():
    path = os.environ.get("RDS_CA_BUNDLE", "").strip()
    if path and os.path.isfile(path):
        return path
    # cache next to this script so repeat runs don't refetch
    cached = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".rds-ca-bundle.pem")
    if os.path.isfile(cached):
        return cached
    try:
        with urllib.request.urlopen(CA_URL, timeout=30) as r:
            data = r.read()
        with open(cached, "wb") as f:
            f.write(data)
        print("[dump] fetched RDS CA bundle -> %s" % cached)
        return cached
    except Exception as e:  # noqa: BLE001
        sys.exit("FATAL: no RDS CA bundle and could not fetch %s: %s\n"
                 "       set RDS_CA_BUNDLE to a local copy" % (CA_URL, e))


def _connect():
    sm = botocore.session.get_session().create_client(
        "secretsmanager", region_name=REGION)
    try:
        raw = sm.get_secret_value(SecretId="%s/db-app-user" % NAME_PREFIX)["SecretString"]
    except Exception as e:  # noqa: BLE001
        sys.exit("FATAL: cannot read %s/db-app-user: %s" % (NAME_PREFIX, e))
    s = json.loads(raw)
    if "username" not in s:
        sys.exit("FATAL: app secret is still {\"bootstrap\":\"pending\"} - "
                 "DbBootstrap has not run")
    ctx = ssl.create_default_context(cafile=_rds_ca())  # verify-full
    try:
        return pg8000.native.Connection(
            user=s["username"], password=s["password"],
            host=s["host"], port=int(s["port"]), database=s["dbname"],
            ssl_context=ctx, timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        sys.exit("FATAL: cannot connect to %s:%s/%s: %s\n"
                 "       (in-VPC host or bastion required; RDS is not public)"
                 % (s.get("host"), s.get("port"), s.get("dbname"), e))


def _q(conn, sql):
    try:
        return conn.run(sql)
    except Exception as e:  # noqa: BLE001
        print("  [warn] query failed: %s" % e)
        return None


def main():
    conn = _connect()
    print("[dump] connected to the gateway store (read-only)\n")

    # 1. Is the gateway metering usage at all?
    total = _q(conn, "SELECT count(*), coalesce(sum(cents),0) FROM spend")
    if total:
        n, cents = total[0]
        print("=== spend (usage metering) ===")
        print("  %d row(s), %.2f USD total metered\n" % (n, (cents or 0) / 100.0))
        if n == 0:
            print("  -> EMPTY. The gateway has metered no usage. Either no inference")
            print("     has run through it, or the spend meter has no rate for the")
            print("     served models (gateway logs: \"spend meter has no exact rates")
            print("     for model\"). This is the token-perspective answer: nothing")
            print("     is being captured.\n")
        else:
            rows = _q(conn,
                      "SELECT s.principal, coalesce(e.email,'?') email, "
                      "s.period, round((s.cents/100.0)::numeric,2) usd "
                      "FROM spend s LEFT JOIN principal_emails e "
                      "ON e.principal=s.principal "
                      "ORDER BY s.cents DESC LIMIT %d" % LIMIT)
            print("  %-24s %-26s %-10s %s" % ("principal", "email", "period", "USD"))
            for r in rows or []:
                print("  %-24s %-26s %-10s %s" % tuple(str(x) for x in r))
            print()

    # 2. Identity + groups - directly explains a missing user_groups label
    ident = _q(conn, "SELECT count(*), "
                     "count(*) FILTER (WHERE groups IS NOT NULL "
                     "AND groups::text NOT IN ('[]','null')) "
                     "FROM principal_emails")
    if ident:
        n, withg = ident[0]
        print("=== principal_emails (identity / Okta groups) ===")
        print("  %d principal(s) seen, %d with a non-empty groups claim\n" % (n, withg))
        rows = _q(conn, "SELECT principal, coalesce(email,'?'), "
                        "coalesce(groups::text,'NULL') FROM principal_emails "
                        "ORDER BY updated_at DESC LIMIT %d" % LIMIT)
        print("  %-24s %-26s %s" % ("principal", "email", "groups"))
        for r in rows or []:
            print("  %-24s %-26s %s" % tuple(str(x) for x in r))
        print()
        if n > 0 and withg == 0:
            print("  -> No principal has a groups claim. The gateway is NOT receiving")
            print("     Okta groups, so:")
            print("       * per-GROUP spend caps (rbac_group) match nobody")
            print("       * the Grafana dashboard's user_groups filter excludes all")
            print("         series -> panels look empty even when metrics are flowing")
            print("     Fix at the Okta side (groups claim on the token); see")
            print("     docs/okta-request-email.md.\n")

    # 3. Caps + audit, for completeness
    caps = _q(conn, "SELECT scope_type, coalesce(scope_id,''), "
                    "round(amount/100.0,2), period FROM spend_limits "
                    "ORDER BY scope_type LIMIT %d" % LIMIT)
    print("=== spend_limits (configured caps) ===")
    if caps:
        for r in caps:
            print("  %-12s %-24s %8s USD / %s" % tuple(str(x) for x in r))
    print("  (%d cap row(s))\n" % (len(caps) if caps else 0))

    audit = _q(conn, "SELECT count(*) FROM admin_audit")
    if audit:
        print("=== admin_audit ===")
        print("  %d admin mutation(s) recorded\n" % audit[0][0])

    conn.close()


if __name__ == "__main__":
    main()
