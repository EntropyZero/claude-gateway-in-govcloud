#!/usr/bin/env python3
"""Read a collector AOT_CONFIG_CONTENT (YAML) on stdin and report, factually,
which receivers each pipeline uses.

The telemetry break we chase is: client OTLP metrics are dropped because the
collector's `metrics` pipeline is missing the `otlp` receiver. This prints the
actual receivers per pipeline so you can see it rather than take anyone's word.
Exit code: 0 = otlp present in the metrics pipeline, 3 = missing, 2 = could not
parse. No verdict beyond what the parsed config literally says.
"""
import sys

try:
    import yaml
except ImportError:
    sys.exit("FATAL: pyyaml not available (pip install pyyaml)")

raw = sys.stdin.read()
if not raw.strip() or raw.strip() in ("None", "null"):
    print("  [skip] no AOT_CONFIG_CONTENT on the task (telemetry sidecar not "
          "deployed?)")
    sys.exit(0)

try:
    cfg = yaml.safe_load(raw)
except Exception as e:  # noqa: BLE001
    sys.exit("FATAL: could not parse AOT_CONFIG_CONTENT as YAML: %s" % e)

pipelines = (((cfg or {}).get("service") or {}).get("pipelines")) or {}
if not pipelines:
    print("  [warn] no service.pipelines in the deployed config")
    sys.exit(2)

print("  deployed collector pipelines (receivers -> exporters):")
for name in sorted(pipelines):
    p = pipelines[name] or {}
    rec = p.get("receivers") or []
    exp = p.get("exporters") or []
    print("    %-8s receivers=%s  exporters=%s" % (name, rec, exp))
print()

metrics = pipelines.get("metrics") or {}
mrec = metrics.get("receivers") or []
if "otlp" in mrec:
    print("  [OK] the metrics pipeline includes `otlp` - client usage metrics")
    print("       (claude_code_*) are accepted. If AMP still lacks them, the")
    print("       loss is downstream (export/query), not the receiver wiring.")
    sys.exit(0)
else:
    print("  [FOUND IT] the metrics pipeline receivers are %s - NO `otlp`." % mrec)
    print("       Client OTLP metrics POSTed to :4318/v1/metrics are dropped")
    print("       (the receiver never registers the metrics route; POST -> 404),")
    print("       while the prometheus self-scrape still feeds otelcol_* to AMP")
    print("       and otlp still feeds the logs pipeline. Reproduced against the")
    print("       pinned ADOT v0.43.0. Fix: the repo template already has")
    print("       `receivers: [otlp, prometheus]` - re-run deploy-gateway.sh to")
    print("       push it (config is a task-def env var; no image rebuild).")
    sys.exit(3)
