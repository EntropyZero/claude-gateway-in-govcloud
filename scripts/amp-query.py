#!/usr/bin/env python3
"""Query AMP (Prometheus) with SigV4 and report what is actually stored.

Split out of diagnose-telemetry.sh so the signing uses botocore's full
credential chain (SSO / assumed role / instance profile) rather than raw keys.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

try:
    import botocore.session
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
except ImportError:
    print("  [skip] botocore not importable - `pip install botocore` to enable "
          "the AMP checks")
    sys.exit(0)

ENDPOINT = (os.environ.get("OBSERVABILITY_AMP_ENDPOINT") or "").strip()
REGION = os.environ.get("AWS_REGION", "us-gov-west-1")
if not ENDPOINT:
    print("  [skip] OBSERVABILITY_AMP_ENDPOINT is not set in deploy.env "
          "(stack 03 persists it) - cannot query AMP")
    sys.exit(0)
if not ENDPOINT.endswith("/"):
    ENDPOINT += "/"

_sess = botocore.session.get_session()
_creds = _sess.get_credentials()
if _creds is None:
    print("  [skip] no AWS credentials resolved")
    sys.exit(0)
_creds = _creds.get_frozen_credentials()


def q(path, params):
    url = ENDPOINT + path + "?" + urllib.parse.urlencode(params, doseq=True)
    req = AWSRequest(method="GET", url=url)
    SigV4Auth(_creds, "aps", REGION).add_auth(req)
    r = urllib.request.Request(url, headers=dict(req.headers))
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        if e.code == 403:
            raise SystemExit(
                f"  [FAIL] 403 from AMP. In likelihood order:\n"
                f"         1. your caller lacks aps:QueryMetrics/GetSeries/GetLabels\n"
                f"         2. CMK trap - querying a CMK-encrypted workspace needs the\n"
                f"            CALLER to hold kms:Decrypt (scoped\n"
                f"            kms:ViaService=aps.{REGION}.amazonaws.com); the aps.*\n"
                f"            service grant is NOT enough. This exact trap broke\n"
                f"            Grafana on 2026-07-23 - same 403, server-side.\n"
                f"         3. expired/wrong credentials for this account\n"
                f"         This is an OPERATOR-role gap and says nothing about whether\n"
                f"         metrics are being ingested.\n"
                f"         body: {body}")
        raise SystemExit(f"  [FAIL] HTTP {e.code} from AMP: {body}")
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"  [FAIL] cannot reach AMP: {e}\n"
                         f"         (no-NAT VPC? this needs the aps-workspaces "
                         f"interface endpoint or run it from in-VPC)")


def names(pattern):
    d = q("api/v1/label/__name__/values", {"match[]": '{__name__=~"%s"}' % pattern})
    return sorted(d.get("data", []))


client = names("claude_code.*")
selfm = names("otelcol.*")

print(f"  otelcol_* series (collector heartbeat) : {len(selfm)} metric name(s)")
print(f"  claude_code_* series (CLIENT usage)    : {len(client)} metric name(s)")
for n in client[:12]:
    print(f"      {n}")

if not selfm and not client:
    print("\n[verdict] AMP holds NOTHING. remote_write is not landing at all - "
          "check the sidecar container logs for export errors.")
elif not client:
    print("\n[verdict] The write path is HEALTHY (otelcol_* present) but NO client")
    print("          usage metrics have been ingested. Clients reach the ALB, so the")
    print("          break is gateway-relay -> sidecar for the METRICS signal only:")
    print("            - confirm the gateway startup line lists metrics:")
    print("                telemetry relay: N destination(s), signals enabled: metrics,logs")
    print("            - check sidecar logs for OTLP receive/export errors")
    sys.exit(0)
else:
    # Client data exists - so an empty dashboard is a QUERY problem, not ingestion.
    print("\n[verdict] Client metrics ARE in AMP. An empty Grafana dashboard is now a")
    print("          QUERY problem, not ingestion. The dashboard filters on")
    print("          team / cost_center / user_groups - checking those labels exist:")
    probe = "claude_code_cost_usage" if "claude_code_cost_usage" in client else client[0]
    d = q("api/v1/series", {"match[]": probe})
    series = d.get("data", [])
    labels = sorted({k for s in series for k in s})
    print(f"          {probe}: {len(series)} series, labels: {', '.join(labels) or '(none)'}")
    for want in ("team", "cost_center", "user_groups", "user_email"):
        mark = "OK " if want in labels else "MISSING"
        print(f"            {mark:8} {want}")
    missing = [w for w in ("team", "cost_center", "user_groups")
               if w not in labels]
    if missing:
        print(f"\n          -> {', '.join(missing)} absent, so the dashboard's")
        print("             =~\"$var\" filters exclude every series. Fix the source of")
        print("             those labels (installer OTEL_RESOURCE_ATTRIBUTES for")
        print("             team/cost_center; gateway-stamped user_groups) or relax")
        print("             the dashboard filters.")
