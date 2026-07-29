"""Structural guard for the ALB access-log Athena table (05-log-analytics).

The table's input.regex is the AWS-documented ALB recipe translated out of
its Hive string literal (their \\\\s is \\s here) - a translation worth
pinning: a wrong escape level parses zero rows at query time and nothing
fails at deploy. So these tests run the EXACT regex the template ships
against real-format ALB access-log lines (per the access-log-entry format in
the ELB docs, current through the conn_trace_id field) and assert the
capture groups land in the declared columns.
"""

import os
import re

import yaml

HERE = os.path.dirname(__file__)


def _load(template):
    """Parse a CFN template, mapping short-form intrinsics to plain values."""
    class Loader(yaml.SafeLoader):
        pass

    def _tag(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    yaml.add_multi_constructor("!", _tag, Loader=Loader)
    path = os.path.join(HERE, "..", "..", "cloudformation", template)
    return yaml.load(open(path), Loader=Loader)


def _table():
    doc = _load("05-log-analytics.yaml")
    return doc["Resources"]["AlbAccessLogsTable"]["Properties"]["TableInput"]


def _columns():
    return [c["Name"] for c in _table()["StorageDescriptor"]["Columns"]]


def _regex():
    return _table()["StorageDescriptor"]["SerdeInfo"]["Parameters"]["input.regex"]


# A modern HTTPS entry through this gateway's ALB: waf+forward actions, all
# fields populated, conn_trace_id present (TID_...). Field values are the
# ELB docs' examples adapted to this deployment's shape (GovCloud ARNs,
# /v1/messages path).
HTTPS_LINE = (
    'https 2026-07-28T22:23:00.186641Z app/claude-gw-alb/50dc6c495c0c9188 '
    '10.20.30.40:2817 10.0.0.1:8443 0.000 0.086 0.000 200 200 573 1043 '
    '"POST https://claude-gw.example.com:443/v1/messages?beta=true HTTP/1.1" '
    '"claude-cli/2.1.220 (external, cli)" ECDHE-RSA-AES128-GCM-SHA256 TLSv1.2 '
    'arn:aws-us-gov:elasticloadbalancing:us-gov-west-1:123456789012:targetgroup/claude-tg/73e2d6bc24d8a067 '
    '"Root=1-58337281-1d84f3d73c47ec4e58577259" "claude-gw.example.com" '
    '"arn:aws-us-gov:acm:us-gov-west-1:123456789012:certificate/12345678-1234-1234-1234-123456789012" '
    '0 2026-07-28T22:22:48.364000Z "waf,forward" "-" "-" "10.0.0.1:8443" "200" '
    '"-" "-" TID_dc57cebed65b444ebc8177bb698fe166'
)

# The sparse/edge values the regex must also survive: a request that never
# reached a target ("-" for target ip:port, -1 times, ELB-generated 460,
# "-" status lists) plus an unknown trailing field for the catch-all tail.
NO_TARGET_LINE = (
    'https 2026-07-28T22:23:00.186641Z app/claude-gw-alb/50dc6c495c0c9188 '
    '10.20.30.40:2817 - -1 -1 -1 460 - 573 293 '
    '"POST https://claude-gw.example.com:443/v1/messages HTTP/1.1" '
    '"claude-cli/2.1.220 (external, cli)" ECDHE-RSA-AES128-GCM-SHA256 TLSv1.2 '
    'arn:aws-us-gov:elasticloadbalancing:us-gov-west-1:123456789012:targetgroup/claude-tg/73e2d6bc24d8a067 '
    '"Root=1-58337281-1d84f3d73c47ec4e58577259" "claude-gw.example.com" '
    '"arn:aws-us-gov:acm:us-gov-west-1:123456789012:certificate/12345678-1234-1234-1234-123456789012" '
    '0 2026-07-28T22:22:48.364000Z "forward" "-" "-" "-" "-" '
    '"-" "-" TID_dc57cebed65b444ebc8177bb698fe166 some-future-field'
)


def test_regex_is_raw_not_hive_escaped():
    # The docs' DDL carries \\s inside a Hive string literal; the Glue table
    # parameter is raw, so a literal backslash-backslash here means the
    # translation regressed and \s stopped meaning whitespace.
    assert r"\\s" not in _regex()
    assert r"[^\s]" in _regex()


def test_regex_keeps_the_future_proofing_tail():
    # AWS: keep the trailing ?( .*)? so newly added log fields extend lines
    # without breaking the parse.
    assert _regex().endswith("?( .*)?")


def test_group_count_covers_every_column():
    assert re.compile(_regex()).groups >= len(_columns())


def test_https_line_parses_into_the_declared_columns():
    m = re.fullmatch(_regex(), HTTPS_LINE)
    assert m, "current-format HTTPS log line did not match input.regex"
    row = dict(zip(_columns(), m.groups()))
    assert row["type"] == "https"
    assert row["time"] == "2026-07-28T22:23:00.186641Z"
    assert row["client_ip"] == "10.20.30.40"
    assert row["client_port"] == "2817"
    assert row["target_ip"] == "10.0.0.1"
    assert row["target_port"] == "8443"
    assert row["elb_status_code"] == "200"
    assert row["target_status_code"] == "200"
    assert row["request_verb"] == "POST"
    assert row["request_url"] == "https://claude-gw.example.com:443/v1/messages?beta=true"
    assert row["request_proto"] == "HTTP/1.1"
    assert row["user_agent"] == "claude-cli/2.1.220 (external, cli)"
    assert row["ssl_protocol"] == "TLSv1.2"
    assert row["domain_name"] == "claude-gw.example.com"
    assert row["actions_executed"] == "waf,forward"
    assert row["target_status_code_list"] == "200"
    assert row["conn_trace_id"] == "TID_dc57cebed65b444ebc8177bb698fe166"


def test_no_target_line_parses_and_tail_absorbs_future_fields():
    m = re.fullmatch(_regex(), NO_TARGET_LINE)
    assert m, "no-target log line did not match input.regex"
    row = dict(zip(_columns(), m.groups()))
    assert row["elb_status_code"] == "460"
    assert row["target_status_code"] == "-"
    assert row["conn_trace_id"] == "TID_dc57cebed65b444ebc8177bb698fe166"
    # The unknown trailing field must land in the catch-all group (beyond the
    # declared columns), not shift any column.
    assert " some-future-field" in m.groups()[len(_columns()):]


def test_projection_is_enabled_with_the_delivery_date_layout():
    params = _table()["Parameters"]
    assert params["projection.enabled"] == "true"
    assert params["projection.day.type"] == "date"
    assert params["projection.day.format"] == "yyyy/MM/dd"
    assert params["projection.day.interval.unit"] == "DAYS"
    # ${!day} is CFN's literal-escape: after Sub, partition projection sees
    # ${day}. A plain ${day} in the template would instead fail the deploy
    # (no such parameter) or, worse, substitute something.
    tmpl = params["storage.location.template"]
    assert tmpl.endswith("/elasticloadbalancing/${AWS::Region}/${!day}")
    loc = _table()["StorageDescriptor"]["Location"]
    assert loc.endswith("/elasticloadbalancing/${AWS::Region}/")


def test_partition_key_is_day_string():
    assert _table()["PartitionKeys"] == [{"Name": "day", "Type": "string"}]
