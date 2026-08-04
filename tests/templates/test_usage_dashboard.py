"""Structural guards for the usage dashboard's per-session accounting
(docker/grafana/dashboards/claude-code-usage.json).

The cumulative panels, range tiles, and top-users table all compute a
per-session in-range rise: counter peak in range minus a range-start
baseline, falling back to the in-range peak when the baseline is missing
(new session) or exceeds it (counter reset). Two properties are load-bearing
and have regressed before (troubleshooting.md 8.3):

- the baseline lookback must be long (7d, not 1h) so a session idle
  overnight/over a weekend does not re-count its pre-range spend, and
- the fallback must be the ``>= 0 ... or`` filter form, so a reset counter
  contributes its new value instead of a negative.

The numeric behavior was verified against a real Prometheus engine with
backfilled scenario data (idle-resumed, fresh, ended-pre-range, and
counter-reset sessions); these guards pin the query shape that passed.
"""

import json
import os
import re

DASHBOARD = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker", "grafana", "dashboards",
    "claude-code-usage.json",
)

# per-series core: ((peak - baseline) >= 0) or peak, with an anchored 7d
# baseline window (@ start() on range queries, offset $__range on instant)
CORE = re.compile(
    r"\(\(max_over_time\((?P<sel>claude_code_\w+\{[^}]*\})\[\$__range\]\) - "
    r"last_over_time\((?P=sel)\[7d\] (?:@ start\(\)|offset \$__range)\)\) >= 0\) "
    r"or max_over_time\((?P=sel)\[\$__range\]\)"
)


def _panels():
    with open(DASHBOARD) as f:
        return json.load(f)["panels"]


def _exprs(pred=lambda _p: True):
    return [
        (p, t["expr"])
        for p in _panels()
        for t in p.get("targets", [])
        if "expr" in t and pred(p)
    ]


def test_every_baseline_query_uses_the_7d_reset_safe_core():
    matched = [e for _, e in _exprs() if CORE.search(e)]
    # 7 cumulative timeseries + cost/token tiles + top-users table
    assert len(matched) == 10, f"expected 10 baseline queries, found {len(matched)}"


def test_no_short_baseline_window_remains():
    for _, e in _exprs():
        assert "[1h] @ start()" not in e and "[1h] offset" not in e, e


def test_no_zero_arithmetic_baseline_fallback_remains():
    # the old missing-baseline dance ("0 + last_over_time" / "0 * max_over_time")
    # silently counts a whole pre-range counter; the or-filter form replaced it
    for _, e in _exprs():
        assert "(0 " not in e, e


def test_cumulative_timeseries_keep_the_in_range_sample_gate():
    cumul = _exprs(lambda p: p.get("type") == "timeseries"
                   and "cumulative" in p.get("title", "").lower())
    assert len(cumul) == 7
    for p, e in cumul:
        assert re.search(r"and count_over_time\(claude_code_\w+\{[^}]*\}"
                         r"\[\$__range\] @ end\(\)\)", e), p["title"]
        # anchored baseline on the plotted form, not the instant offset form
        assert "@ start()" in e, p["title"]


def test_instant_queries_anchor_the_baseline_with_offset_not_at_start():
    # on an instant query start() == end() == eval time, so a [7d] @ start()
    # baseline window would overlap the range, resolve to the latest sample,
    # and zero the tiles; the instant form must use offset $__range
    instant = [
        (p, t["expr"])
        for p in _panels()
        for t in p.get("targets", [])
        if t.get("instant") and "last_over_time" in t.get("expr", "")
    ]
    assert len(instant) == 3  # cost tile, tokens tile, top-users table
    for p, e in instant:
        assert "[7d] offset $__range" in e, p["title"]
        assert "@ start()" not in e and "@ end()" not in e, p["title"]


def test_descriptions_state_the_7d_baseline():
    described = [
        p for p in _panels()
        if "7d" in p.get("description", "")
    ]
    # cost tile + 7 cumulative panels tell the reader the idle-resume behavior
    assert len(described) == 8, [p.get("title") for p in described]


# burn-rate per-series core: last_over_time - min_over_time over the trailing
# hour. For a monotonic-in-window counter last == max, so this reads exactly
# like the old max - min; on a same-series counter reset inside the window it
# yields the post-reset rise (last >= min structurally, never negative)
# instead of spiking by the full pre-reset value for an hour.
BURN_CORE = re.compile(
    r"last_over_time\((?P<sel>claude_code_\w+\{[^}]*\})\[1h\]\) - "
    r"min_over_time\((?P=sel)\[1h\]\)"
)


def test_burn_rate_panels_use_the_reset_safe_last_minus_min_core():
    burn = _exprs(lambda p: "burn rate" in p.get("title", "").lower()
                  and p.get("type") == "timeseries")
    assert len(burn) == 7, [p.get("title") for p, _ in burn]
    for p, e in burn:
        assert BURN_CORE.search(e), p["title"]


def test_no_reset_blind_trailing_window_delta_remains():
    # max_over_time - min_over_time over a trailing window reads a counter
    # reset as (pre-reset peak - post-reset min): the full counter value,
    # shown as a spike for exactly one window-width
    for p, e in _exprs():
        assert "max_over_time" not in e or "[1h]" not in e, p.get("title")
