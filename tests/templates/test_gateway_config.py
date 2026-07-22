"""Structural guard for the embedded gateway config in 02-gateway.yaml.

The gateway's YAML config is carried as a base64 Fn::Sub block in the
GATEWAY_CONFIG_B64 task-definition env var, so cfn-lint cannot see inside it.
This test extracts that block, parses it as YAML (CFN substitutions
neutralised), and asserts the model routing matches the gateway's config
schema - specifically that `upstream_model` is an OBJECT keyed by the
upstream's name, not a bare string. A string there boots-crashes the task
with "Expected object, received string" (regression: the first deploy hit it).
See https://code.claude.com/docs/en/claude-apps-gateway-config#models
"""

import os
import re

import pytest
import yaml

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "..", "cloudformation", "02-gateway.yaml"
)


def _extract_config_block():
    """Return the raw GATEWAY_CONFIG_B64 config body (first !Sub list element).

    The env var now carries a TWO-ARG Fn::Base64 !Sub - the config body is the
    block scalar under `- |`, and a second list element supplies OidcScopesLine
    (empty vs the `scopes:` line, conditional on HaveManagedCli). We slice out
    just the body block scalar; the loop stops at the shallower `- OidcScopesLine`
    element that follows it.
    """
    lines = open(TEMPLATE).read().split("\n")
    start = next(i for i, l in enumerate(lines) if "GATEWAY_CONFIG_B64" in l)
    subi = next(i for i in range(start, start + 12) if lines[i].strip() == "- |")
    base_indent = len(lines[subi + 1]) - len(lines[subi + 1].lstrip())
    block = []
    for l in lines[subi + 1:]:
        if l.strip() == "":
            block.append("")
            continue
        if len(l) - len(l.lstrip()) < base_indent:
            break
        block.append(l[base_indent:])
    return "\n".join(block)


def _load_gateway_config(scopes_line=False):
    """Parse the config body, simulating the OidcScopesLine substitution.

    scopes_line=False models the default (ManagedCliGroups unset) render - no
    `scopes:` line; scopes_line=True models the HaveManagedCli render.
    """
    raw = _extract_config_block()
    repl = "scopes: [openid, profile, email, offline_access, groups]" if scopes_line else ""
    raw = raw.replace("${OidcScopesLine}", repl)
    # Neutralise CFN substitutions: ${!VAR} is runtime env expansion, ${VAR}
    # / ${AWS::X} is deploy-time substitution - both become opaque scalars.
    raw = re.sub(r"\$\{![^}]+\}", "RUNTIME_PLACEHOLDER", raw)
    raw = re.sub(r"\$\{[^}]+\}", "CFN_PLACEHOLDER", raw)
    return yaml.safe_load(raw)


def _assert_upstream_model_objects(doc):
    """upstream_model must be an object keyed by an existing upstream name."""
    names = {u.get("name", u["provider"]) for u in doc["upstreams"]}
    for m in doc["models"]:
        um = m["upstream_model"]
        assert isinstance(um, dict), (
            f"model {m.get('id')!r}: upstream_model must be an object "
            f"(got {type(um).__name__}) - a string fails schema validation"
        )
        assert set(um) <= names, (
            f"model {m.get('id')!r}: upstream_model keys {set(um)} must be a "
            f"subset of upstream names {names}"
        )


def test_embedded_config_parses_as_yaml():
    doc = _load_gateway_config()
    assert "upstreams" in doc and "models" in doc


def test_upstream_model_is_object_keyed_by_upstream():
    _assert_upstream_model_objects(_load_gateway_config())


def test_bedrock_upstream_key_present():
    # The single unnamed bedrock upstream keys on its provider string.
    doc = _load_gateway_config()
    for m in doc["models"]:
        assert "bedrock" in m["upstream_model"], f"{m.get('id')}: no bedrock mapping"


def test_check_rejects_string_upstream_model():
    """The gate must fail on the exact regression: a string upstream_model."""
    bad = {
        "upstreams": [{"provider": "bedrock"}],
        "models": [{"id": "x", "upstream_model": "us-gov.anthropic.claude-opus-4-8"}],
    }
    with pytest.raises(AssertionError):
        _assert_upstream_model_objects(bad)


def _okta_issuer_pattern(template):
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "cloudformation", template
    )
    text = open(path).read()
    # the OktaIssuer parameter block's AllowedPattern
    blk = text[text.index("OktaIssuer:"):]
    m = re.search(r"AllowedPattern:\s*'([^']+)'", blk)
    assert m, f"{template}: OktaIssuer has no AllowedPattern"
    return m.group(1)


def test_okta_issuer_pattern_identical_across_stacks():
    # The same OKTA_ISSUER env var feeds every stack, so a value that passes
    # 02 must pass 03 and 04 (and vice versa). Divergence let a trailing-slash
    # issuer deploy the gateway but break Grafana's derived /oauth2/v1 URLs.
    p02 = _okta_issuer_pattern("02-gateway.yaml")
    p03 = _okta_issuer_pattern("03-observability.yaml")
    p04 = _okta_issuer_pattern("04-download-portal.yaml")
    assert p02 == p03 == p04, (
        f"OktaIssuer patterns diverge:\n 02: {p02}\n 03: {p03}\n 04: {p04}"
    )


def test_okta_issuer_pattern_rejects_trailing_slash_and_scheme_less():
    pat = _okta_issuer_pattern("02-gateway.yaml")
    assert re.fullmatch(pat, "https://your-org.okta.com")               # org
    assert re.fullmatch(pat, "https://your-org.okta.com/oauth2/default")  # custom
    assert not re.fullmatch(pat, "https://your-org.okta.com/")          # trailing slash
    assert not re.fullmatch(pat, "your-org.okta.com")                   # no scheme
    assert not re.fullmatch(pat, "http://your-org.okta.com")            # not https


# ---------------------------------------------------------------------------
# Managed CLI lockdown (ManagedCliGroups -> /managed/settings update lockdown)
# ---------------------------------------------------------------------------

def _template_text():
    return open(TEMPLATE).read()


def _managed_b64_block():
    """Return the GATEWAY_MANAGED_B64 env-var YAML (the !If entry) as text."""
    text = _template_text()
    i = text.index("GATEWAY_MANAGED_B64")
    # backtrack to the `- !If` that opens this list entry
    head = text.rindex("- !If", 0, i)
    # cut at the next `- Name:`/`Secrets:` sibling for a bounded slice
    tail = text.index("Secrets:", i)
    return text[head:tail]


def test_managed_cli_param_and_condition_exist():
    text = _template_text()
    # parameter present with an empty default (opt-in)
    assert re.search(r"^  ManagedCliGroups:\s*$", text, re.M), "ManagedCliGroups param missing"
    pblk = text[text.index("ManagedCliGroups:"):]
    assert re.search(r"Default:\s*''", pblk[:400]), "ManagedCliGroups should default to ''"
    # condition present and keyed on the parameter being non-empty
    assert re.search(
        r"HaveManagedCli:\s*!Not\s*\[!Equals\s*\[!Ref ManagedCliGroups,\s*''\]\]", text
    ), "HaveManagedCli condition missing or not keyed on ManagedCliGroups != ''"


def test_managed_b64_is_conditional_on_have_managed_cli():
    block = _managed_b64_block()
    # the env var is gated by the HaveManagedCli !If, NoValue on the else branch
    assert "HaveManagedCli" in block, "GATEWAY_MANAGED_B64 not gated by HaveManagedCli"
    assert "AWS::NoValue" in block, "GATEWAY_MANAGED_B64 !If has no NoValue else branch"
    assert "Fn::Base64:" in block, "GATEWAY_MANAGED_B64 value should be Fn::Base64"


def test_managed_b64_block_disables_updates_and_matches_groups():
    """The rendered managed: block must lock updates and key on the group list."""
    block = _managed_b64_block()
    # pull out the Fn::Base64 !Sub | body and parse it as YAML
    subi = block.index("Fn::Base64: !Sub |")
    body_lines = block[subi:].split("\n")[1:]
    base_indent = len(body_lines[0]) - len(body_lines[0].lstrip())
    yaml_lines = []
    for l in body_lines:
        if l.strip() == "":
            continue
        if len(l) - len(l.lstrip()) < base_indent:
            break
        yaml_lines.append(l[base_indent:])
    raw = "\n".join(yaml_lines).replace("${ManagedCliGroups}", "grp-a, grp-b")
    doc = yaml.safe_load(raw)
    policy = doc["managed"]["policies"][0]
    assert policy["match"]["groups"] == ["grp-a", "grp-b"], policy["match"]
    env = policy["cli"]["env"]
    assert env["DISABLE_UPDATES"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"


def test_oidc_scopes_line_is_conditional_not_hardcoded():
    """The active `scopes:` line must come from the OidcScopesLine !If, and the
    body block must NOT hardcode a live (uncommented) scopes line."""
    text = _template_text()
    # the Sub var is wired to HaveManagedCli with the full groups-scope line
    assert re.search(
        r"OidcScopesLine:\s*!If", text
    ), "OidcScopesLine is not driven by an !If"
    m = re.search(
        r"OidcScopesLine:\s*!If\s*\n\s*-\s*HaveManagedCli\s*\n\s*-\s*'scopes: \[openid, profile, email, offline_access, groups\]'\s*\n\s*-\s*''",
        text,
    )
    assert m, "OidcScopesLine !If wiring (HaveManagedCli -> scopes line / '') not found"
    # the config body itself must not carry an active `scopes:` line - only the
    # injected marker and comments. Any bare `scopes:` (not '#', not the Sub var)
    # would mean the scope is unconditionally requested.
    body = _extract_config_block()
    for l in body.split("\n"):
        s = l.strip()
        assert not (s.startswith("scopes:")), f"body hardcodes an active scopes line: {l!r}"
    assert "${OidcScopesLine}" in body, "OidcScopesLine marker missing from config body"


def test_both_scope_variants_parse_valid_yaml():
    """Both rendered variants (managed on/off) must be valid YAML, differing
    only by the presence of the oidc.scopes list."""
    off = _load_gateway_config(scopes_line=False)
    on = _load_gateway_config(scopes_line=True)
    assert "scopes" not in off["oidc"], "default render should omit oidc.scopes"
    assert on["oidc"]["scopes"] == [
        "openid",
        "profile",
        "email",
        "offline_access",
        "groups",
    ]
