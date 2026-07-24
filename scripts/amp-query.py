#!/usr/bin/env python3
"""Query AMP (Prometheus) with SigV4 and report what is actually stored.

Split out of diagnose-telemetry.sh so the signing uses botocore's full
credential chain (SSO / assumed role / instance profile) rather than raw keys.
"""
import json
import os
import sys
import urllib.parse
import urllib.error
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


def encode_query(params):
    """RFC3986 percent-encoding ONLY - never quote_plus. quote_plus turns
    spaces into '+', an ambiguous byte under SigV4: botocore signs one
    canonicalization of it, the AMP front end computes another, and the
    request 403s with SignatureDoesNotMatch. Only queries containing spaces
    (the PromQL '... by (label)' throughput calls) were affected, which is
    why SOME calls failed while name lookups succeeded.
    """
    return urllib.parse.urlencode(params, doseq=True,
                                  quote_via=urllib.parse.quote)


def q(path, params):
    url = ENDPOINT + path + "?" + encode_query(params)
    req = AWSRequest(method="GET", url=url)
    SigV4Auth(_creds, "aps", REGION).add_auth(req)
    r = urllib.request.Request(url, headers=dict(req.headers))
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        if e.code == 403 and ("does not match" in body.lower()
                              or "signature" in body.lower()):
            raise SystemExit(
                f"  [FAIL] 403 SignatureDoesNotMatch from AMP.\n"
                f"         This is a SIGNING bug in the request encoding, NOT an\n"
                f"         IAM/permissions problem - do not chase key policies.\n"
                f"         (Historic cause here: quote_plus space-encoding; fixed\n"
                f"         2026-07-24. If you see this, the URL encoding regressed.)\n"
                f"         body: {body}")
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


import time as _time

WINDOW_H = int(os.environ.get("AMP_QUERY_WINDOW_HOURS", "48"))


def names(pattern):
    """Metric names matching pattern seen in the last WINDOW_H hours.

    Explicit start/end matter: without them the label-values API uses
    server-side defaults, and instant queries only look back 5 minutes -
    both can miss BURSTY client metrics (written only while a session is
    active) while the 30s heartbeat keeps otelcol_* perpetually fresh.
    """
    now = int(_time.time())
    d = q("api/v1/label/__name__/values", {
        "match[]": '{__name__=~"%s"}' % pattern,
        "start": str(now - WINDOW_H * 3600),
        "end": str(now),
    })
    return sorted(d.get("data", []))


def ever_stored(pattern):
    """Count of series with ANY sample in the window - immune to burstiness."""
    d = q("api/v1/query", {
        "query": 'count(count_over_time({__name__=~"%s"}[%dh]))' % (pattern, WINDOW_H)})
    r = d.get("data", {}).get("result", [])
    try:
        return int(float(r[0]["value"][1])) if r else 0
    except (KeyError, IndexError, ValueError):
        return 0


def qsum_by(metric, label):
    """Instant PromQL sum(metric) by (label) -> {labelvalue: float}. {} if absent."""
    d = q("api/v1/query", {"query": "sum(%s) by (%s)" % (metric, label)})
    out = {}
    for r in (d.get("data", {}).get("result", []) or []):
        try:
            out[r["metric"].get(label, "?")] = float(r["value"][1])
        except (KeyError, IndexError, ValueError):
            pass
    return out


def collector_pipeline_report():
    """When the receiver IS wired to metrics but claude_code_* still don't land,
    the collector's OWN throughput counters (already in AMP as otelcol_*)
    pinpoint where the points are lost between OTLP-accept and remote_write."""
    acc = qsum_by("otelcol_receiver_accepted_metric_points", "receiver")
    ref = qsum_by("otelcol_receiver_refused_metric_points", "receiver")
    sent = qsum_by("otelcol_exporter_sent_metric_points", "exporter")
    failed = qsum_by("otelcol_exporter_send_failed_metric_points", "exporter")
    xlate = qsum_by("otelcol_exporter_prometheusremotewrite_failed_translations",
                    "exporter")
    if not acc and not sent:
        print("  [skip] no otelcol_*_metric_points counters in AMP - cannot trace")
        print("         the pipeline (older ADOT, or self-metrics scrape not landing)")
        return
    otlp_acc = acc.get("otlp", 0.0)
    otlp_ref = ref.get("otlp", 0.0)
    rw_sent = sent.get("prometheusremotewrite", sum(sent.values()) if sent else 0.0)
    rw_fail = failed.get("prometheusremotewrite",
                         sum(failed.values()) if failed else 0.0)
    xl_fail = sum(xlate.values()) if xlate else 0.0
    print("  receiver accepted : otlp=%.0f  prometheus=%.0f"
          % (otlp_acc, acc.get("prometheus", 0.0)))
    print("  receiver refused  : otlp=%.0f" % otlp_ref)
    print("  remote_write      : sent=%.0f  failed=%.0f" % (rw_sent, rw_fail))
    print("  FAILED TRANSLATIONS: %.0f   <- the silent-drop counter" % xl_fail)
    print()
    if xl_fail > 0:
        print("  [verdict] FOUND IT: prometheusremotewrite is DROPPING points at")
        print("            TRANSLATION. Dropped points are STILL counted in")
        print("            sent_metric_points with send_failed=0 and NOTHING is")
        print("            logged (reproduced on ADOT v0.43.0) - which is why every")
        print("            other counter looked healthy. The canonical cause is")
        print("            DELTA temporality sums, which Prometheus cannot")
        print("            represent; the otelcol_* self-metrics are cumulative,")
        print("            which is why THEY land. Fix at the SOURCE: the gateway")
        print("            now pushes")
        print("            OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=")
        print("            cumulative via /managed/settings (02, 2026-07-24) -")
        print("            redeploy 02, then clients pick it up on their next")
        print("            settings fetch. Do NOT add a deltatocumulative")
        print("            processor instead: with DesiredCount=2 the two sidecars")
        print("            would rebuild conflicting cumulative sums for the same")
        print("            series.")
        return
    if otlp_acc == 0:
        print("  [verdict] The OTLP receiver has accepted ZERO metric points, even")
        print("            though it is wired to the metrics pipeline. The client")
        print("            metric DATA is not arriving at :4318. So either the gateway")
        print("            is not relaying client metrics (only its own activity LOG,")
        print("            which explains logs working), or clients POST /v1/metrics")
        print("            with no metric points. Check the gateway startup line lists")
        print("            `metrics` under signals enabled, and that a real session has")
        print("            produced usage. If otlp_ref > 0, they arrive but are refused")
        print("            (payload/format) - check the sidecar logs.")
    elif rw_fail > 0:
        print("  [verdict] OTLP metrics ARE accepted, but remote_write is FAILING for")
        print("            some/all points. AMP is rejecting them. The prime suspect is")
        print("            DELTA temporality: Prometheus/AMP need CUMULATIVE, and the")
        print("            otelcol_* self-metrics ARE cumulative (which is why they")
        print("            land while claude_code_* do not). Confirm in the SIDECAR")
        print("            LOGS (exact remote_write error), then set")
        print("            OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative")
        print("            on the client, or add a deltatocumulative processor.")
    else:
        print("  [verdict] OTLP metrics accepted AND remote_write succeeding, yet no")
        print("            claude_code_* by name. The points are dropped BETWEEN accept")
        print("            and export - query otelcol_processor_dropped_metric_points")
        print("            and inspect the attributes/cardinality + batch processors,")
        print("            or a metric-name rewrite. Otherwise re-check the name filter.")


client = names("claude_code.*")
selfm = names("otelcol.*")
client_ever = ever_stored("claude_code.+")
target_info = ever_stored("target_info")

print(f"  window: last {WINDOW_H}h (AMP_QUERY_WINDOW_HOURS to change)")
print(f"  otelcol_* series (collector heartbeat) : {len(selfm)} metric name(s)")
print(f"  claude_code_* series (CLIENT usage)    : {len(client)} metric name(s)")
print(f"  claude_code_* series with ANY sample   : {client_ever} (burst-proof)")
print(f"  target_info series (client payloads)   : {target_info}")
for n in client[:12]:
    print(f"      {n}")

if client_ever and not client:
    print("\n  [note] samples EXIST in the window but the name lookup missed them -")
    print("         trust the burst-proof count; the data is in AMP.")
client = client or (["claude_code_cost_usage"] if client_ever else [])

if not selfm and not client:
    print("\n[verdict] AMP holds NOTHING. remote_write is not landing at all - "
          "check the sidecar container logs for export errors.")
elif not client:
    print("\n[verdict] The write path is HEALTHY (otelcol_* present) but NO client")
    print("          usage metrics are in AMP. Tracing the collector pipeline from")
    print("          its own throughput counters (already in AMP):\n")
    collector_pipeline_report()
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
