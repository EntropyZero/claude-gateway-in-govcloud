"""Regression guard for the AMP SigV4 signing bug (2026-07-24).

urlencode's default quote_plus turns spaces into '+', an ambiguous byte under
SigV4: botocore signs one canonicalization, the AMP front end computes
another, and the request 403s with SignatureDoesNotMatch. Only the PromQL
queries containing spaces ("sum(...) by (label)") were affected, which made it
look like an intermittent permissions problem.

These tests import amp-query.py with a stub environment (static creds, fake
endpoint - nothing touches the network at import) and pin the encoding
properties of the exact URLs it builds.
"""
import importlib.util
import os
import sys
import urllib.parse

import pytest

SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "amp-query.py"
)


@pytest.fixture(scope="module")
def ampq():
    env = {
        "OBSERVABILITY_AMP_ENDPOINT": "https://aps.example.test/workspaces/ws-x/",
        "AWS_ACCESS_KEY_ID": "AKIATESTTESTTESTTEST",
        "AWS_SECRET_ACCESS_KEY": "testsecret",
        "AWS_REGION": "us-gov-west-1",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("ampq", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        # the module runs its query flow at top level when executed as a
        # script; as an import target we only need the helpers, so stop it
        # from reaching the network by executing only up to the first call.
        src = open(SCRIPT).read()
        cut = src.index('client = names(')
        exec(compile(src[:cut], SCRIPT, "exec"), mod.__dict__)
        yield mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_spaces_are_percent20_never_plus(ampq):
    qs = ampq.encode_query(
        {"query": "sum(otelcol_receiver_accepted_metric_points) by (receiver)"}
    )
    assert "+" not in qs, (
        "quote_plus space-encoding regressed - '+' is ambiguous under SigV4 "
        "and produces 403 SignatureDoesNotMatch on AMP"
    )
    assert "%20" in qs


def test_promql_specials_roundtrip(ampq):
    params = {
        "match[]": '{__name__=~"claude_code.+"}',
        "query": 'count(count_over_time({__name__=~"x"}[48h])) by (a)',
        "start": "1750000000",
    }
    qs = ampq.encode_query(params)
    assert "+" not in qs
    back = dict(urllib.parse.parse_qsl(qs, keep_blank_values=True))
    assert back == params, "encoding must round-trip losslessly"


def test_signing_uses_the_same_bytes_that_are_sent(ampq):
    """The URL botocore signs and the URL urllib sends must be byte-identical -
    that identity is what makes the signature verifiable server-side."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    qs = ampq.encode_query({"query": "sum(x) by (receiver)"})
    url = "https://aps.example.test/workspaces/ws-x/api/v1/query?" + qs
    req = AWSRequest(method="GET", url=url)
    SigV4Auth(ampq._creds, "aps", "us-gov-west-1").add_auth(req)
    assert req.url == url, "botocore mutated the URL after signing"
    assert "Authorization" in req.headers
