"""Unit tests for scripts/check-collector-config.py.

The telemetry break it detects: a deployed collector whose `metrics` pipeline
is missing the `otlp` receiver silently drops client claude_code_* metrics
(reproduced against the pinned ADOT v0.43.0). The checker reads AOT_CONFIG_CONTENT
on stdin and exits 0 (otlp present), 3 (missing), or 2 (unparseable). These pin
those exit codes so the diagnosis can't regress.
"""
import os
import subprocess
import sys

CHECKER = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "check-collector-config.py"
)

GOOD = """
service:
  pipelines:
    metrics:
      receivers: [otlp, prometheus]
      exporters: [prometheusremotewrite]
    logs:
      receivers: [otlp]
      exporters: [awscloudwatchlogs]
"""

BAD = """
service:
  pipelines:
    metrics:
      receivers: [prometheus]
      exporters: [prometheusremotewrite]
    logs:
      receivers: [otlp]
      exporters: [awscloudwatchlogs]
"""


def _run(stdin):
    return subprocess.run(
        [sys.executable, CHECKER],
        input=stdin, capture_output=True, text=True,
    )


def test_otlp_present_in_metrics_exits_zero():
    r = _run(GOOD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "metrics pipeline includes `otlp`" in r.stdout


def test_otlp_missing_from_metrics_exits_three():
    r = _run(BAD)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "FOUND IT" in r.stdout
    # the actual receivers list is shown as evidence, not just a verdict
    assert "['prometheus']" in r.stdout


def test_empty_input_skips_cleanly():
    for blank in ("", "None", "null", "   \n"):
        r = _run(blank)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "skip" in r.stdout.lower()


def test_no_pipelines_is_a_warn_not_a_crash():
    r = _run("service:\n  extensions: [health_check]\n")
    assert r.returncode == 2, r.stdout + r.stderr


def test_unparseable_yaml_is_fatal_not_silent():
    r = _run("service:\n  pipelines:\n  : : : not yaml\n    - [\n")
    assert r.returncode != 0
