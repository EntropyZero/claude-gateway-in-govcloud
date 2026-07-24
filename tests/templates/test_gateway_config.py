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
# Managed settings push (/managed/settings): model allowlist + optional
# group-scoped update lockdown
# ---------------------------------------------------------------------------

def _template_text():
    return open(TEMPLATE).read()


def _managed_b64_block():
    """Return the GATEWAY_MANAGED_B64 env-var YAML entry as text."""
    text = _template_text()
    # anchor on the env-var DECLARATION, not the prose mentions of the name in
    # the config-body comment above it
    m = re.search(r"^\s*- Name: GATEWAY_MANAGED_B64$", text, re.M)
    assert m, "GATEWAY_MANAGED_B64 env-var declaration not found"
    tail = text.index("Secrets:", m.start())
    return text[m.start():tail]


def _managed_policies():
    """Parse the rendered managed: block into policy dicts.

    Single unconditional `Fn::Base64: !Sub |` block scalar (the ManagedCliGroups
    variant was retired 2026-07-24 along with the parameter).
    """
    block = _managed_b64_block()
    m = re.search(r"Fn::Base64: !Sub \|\n", block)
    assert m, "GATEWAY_MANAGED_B64 should be a single Fn::Base64 !Sub block scalar"
    body_lines = block[m.end():].split("\n")
    base_indent = len(body_lines[0]) - len(body_lines[0].lstrip())
    yaml_lines = []
    for l in body_lines:
        if l.strip() == "":
            continue
        if len(l) - len(l.lstrip()) < base_indent:
            break
        yaml_lines.append(l[base_indent:])
    raw = "\n".join(yaml_lines)
    raw = raw.replace("${OpusModelId}", "claude-opus-4-8")
    raw = raw.replace("${SonnetModelId}", "claude-sonnet-4-5")
    return yaml.safe_load(raw)["managed"]["policies"]


def test_managed_cli_groups_is_fully_retired():
    """MANAGED_CLI_GROUPS / HaveManagedCli were removed when spend limits landed
    (the groups claim became a hard prerequisite instead of an opt-in). A stale
    !Ref/!If to either would fail the deploy, and a stale parameter would be a
    silently-ignored knob."""
    text = _template_text()
    for token in ("!Ref ManagedCliGroups", "HaveManagedCli"):
        assert token not in text, f"stale {token} left in the template"
    assert not re.search(r"^  ManagedCliGroups:\s*$", text, re.M), (
        "ManagedCliGroups parameter should be gone"
    )


def test_managed_b64_is_always_emitted():
    """The model allowlist must reach EVERY client, so the env var is
    unconditional - no !If, no NoValue branch."""
    block = _managed_b64_block()
    assert "AWS::NoValue" not in block, (
        "GATEWAY_MANAGED_B64 must not be droppable - the model allowlist has to "
        "be pushed on every deployment"
    )
    assert "Fn::Base64: !Sub" in block


def test_catch_all_policy_must_be_last():
    """A policy with no `match:` is a CATCH-ALL, and policy selection is
    FIRST-MATCH-WINS over a single policy - so a catch-all anywhere but the end
    shadows every policy after it.

    RUNTIME-VERIFIED against the mirrored 2.1.211 gateway (2026-07-24): with a
    catch-all ahead of another policy the gateway logs
        warn managed.policies[0] is a catch-all (match: {}) but is not the last
             entry - policies after it will never match. Move it to the end.
    and still BOOTS, so only this gate catches it.
    """
    policies = _managed_policies()
    catch_alls = [i for i, p in enumerate(policies) if "match" not in p]
    assert catch_alls, "expected a catch-all (model-allowlist) policy"
    assert catch_alls == [len(policies) - 1], (
        f"catch-all must be LAST; found at {catch_alls} of {len(policies)} "
        "policies - everything after it is dead config"
    )


def test_model_allowlist_and_lockdown_reach_every_user():
    """The catch-all carries BOTH the model allowlist and the update lockdown,
    with no `match:` - so neither depends on an Okta groups claim.

    Without the allowlist the client falls back to its own built-in model menu,
    none of whose entries the gateway serves (live symptom: every model
    unauthorized).
    """
    policies = _managed_policies()
    cli = policies[-1]["cli"]
    assert "match" not in policies[-1], (
        "the allowlist/lockdown policy must not be group-scoped - a `match:` "
        "here silently drops both for users outside those Okta groups"
    )
    # keys live INSIDE `cli` (Claude Code settings.json keys), not on the policy
    assert cli["availableModels"] == ["claude-opus-4-8", "claude-sonnet-4-5"]
    assert cli["enforceAvailableModels"] is True, (
        "without enforceAvailableModels the Default selection can still resolve "
        "to a model the gateway does not serve"
    )
    assert cli["env"]["DISABLE_UPDATES"] == "1"
    assert cli["env"]["DISABLE_AUTOUPDATER"] == "1"
    # Clients export DELTA sums by default and the sidecar's prometheus
    # translation SILENTLY drops them (counted sent, failed=0, no log -
    # reproduced on the pinned ADOT v0.43.0). Cumulative-from-the-source is
    # what makes usage metrics reach AMP at all; a deltatocumulative processor
    # in the sidecar would conflict across DesiredCount=2 relays.
    assert cli["env"]["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] == "cumulative", (
        "without cumulative temporality, claude_code_* metrics are silently "
        "dropped at prometheusremotewrite translation and never reach AMP"
    )


def test_available_models_is_never_at_policy_level():
    """`availableModels` is only valid inside `cli`. At the policy level the
    gateway rejects it ("Unrecognized key(s) in object") and refuses to boot -
    binary-verified against the mirrored 2.1.211 gateway, 2026-07-24.
    """
    for policy in _managed_policies():
        assert "availableModels" not in policy, (
            f"availableModels at policy level is a BOOT FAILURE: {policy!r}"
        )
        assert "enforceAvailableModels" not in policy, (
            f"enforceAvailableModels at policy level is a BOOT FAILURE: {policy!r}"
        )


# ---------------------------------------------------------------------------
# Spend limits (admin: / enforcement: blocks)
# ---------------------------------------------------------------------------

def test_admin_block_present_and_enables_enforcement():
    """The `admin:` block is the MASTER SWITCH - the gateway only runs spend
    enforcement when admin is configured, and the config schema explicitly
    refuses `fail_closed_on_error` without it."""
    doc = _load_gateway_config()
    assert "admin" in doc, "no admin: block - spend enforcement would never run"
    admin = doc["admin"]
    assert admin["write_keys"] and admin["write_keys"][0]["id"], (
        "a write key with an id is required to mutate caps (id = audit attribution)"
    )
    assert admin["read_keys"] and admin["read_keys"][0]["id"]


def test_admin_keys_come_from_runtime_env_not_literals():
    """Keys must be injected from Secrets Manager at runtime, never baked into
    the template as literals."""
    body = _extract_config_block()
    assert "${!SPEND_ADMIN_WRITE_KEY}" in body, "write key is not a runtime env ref"
    assert "${!SPEND_ADMIN_READ_KEY}" in body, "read key is not a runtime env ref"


def test_enforcement_fails_closed():
    """Operator decision (2026-07-24): a spend-store error blocks rather than
    allowing an uncapped request. This is an availability trade - see
    om-runbooks - so it is pinned by a test."""
    doc = _load_gateway_config()
    assert doc["enforcement"]["fail_closed_on_error"] is True


def test_groups_scope_is_unconditional():
    """Per-group caps (scope_type rbac_group) resolve against the Okta groups
    claim, so the `groups` scope is now a hard prerequisite, not an opt-in."""
    text = _template_text()
    assert not re.search(r"OidcScopesLine:\s*!If", text), (
        "OidcScopesLine must no longer be conditional - per-group spend caps "
        "need the groups claim on every deployment"
    )
    doc = _load_gateway_config(scopes_line=True)
    assert "groups" in doc["oidc"]["scopes"]


def test_oidc_scopes_line_comes_from_the_sub_var():
    """The active `scopes:` line must still come from the OidcScopesLine Sub var
    (now a constant, not an !If), and the body must not hardcode a second one."""
    text = _template_text()
    assert re.search(
        r"OidcScopesLine:\s*'scopes: \[openid, profile, email, offline_access, groups\]'",
        text,
    ), "OidcScopesLine should be the constant full scopes line"
    body = _extract_config_block()
    for l in body.split("\n"):
        assert not l.strip().startswith("scopes:"), (
            f"body hardcodes a second active scopes line: {l!r}"
        )
    assert "${OidcScopesLine}" in body, "OidcScopesLine marker missing from config body"


def test_rendered_config_always_requests_group_scope():
    """Only one render exists now - it must always carry the groups scope."""
    doc = _load_gateway_config(scopes_line=True)
    assert doc["oidc"]["scopes"] == [
        "openid",
        "profile",
        "email",
        "offline_access",
        "groups",
    ]


# ---------------------------------------------------------------------------
# SSRF guard / loopback sidecar
# ---------------------------------------------------------------------------

def test_loopback_sidecar_has_ssrf_override():
    """The telemetry sidecar is reached over loopback, and the gateway BLOCKS
    loopback by default via a custom DNS lookup:

        if (range === "loopback") return !truthy(CLAUDE_GATEWAY_ALLOW_LOOPBACK)

    Config validation does NOT catch this - the static check parses "localhost"
    as a hostname, not an IP - so it fails only at runtime:
        forward to http://localhost:4318 failed: ECONNREFUSED_SSRF:
        blocked (cloud metadata / link-local): localhost -> 127.0.0.1

    RUNTIME-VERIFIED 2026-07-24: with the flag unset a loopback target is
    rejected; with it set it is allowed, while 169.254.169.254 and
    100.100.100.200 stay blocked either way.
    """
    text = _template_text()
    assert "CLAUDE_GATEWAY_ALLOW_LOOPBACK" in text, (
        "loopback forward_to without CLAUDE_GATEWAY_ALLOW_LOOPBACK - telemetry "
        "forwarding will fail at runtime with ECONNREFUSED_SSRF"
    )
    # must be paired with the sidecar it exists for: anchor on the DECLARATION
    # (not the prose above it) and require the !If gate immediately before.
    m = re.search(r"^\s*- Name: CLAUDE_GATEWAY_ALLOW_LOOPBACK$", text, re.M)
    assert m, "CLAUDE_GATEWAY_ALLOW_LOOPBACK env-var declaration not found"
    preceding = text[max(0, m.start() - 200):m.start()]
    assert "HaveTelemetry" in preceding, (
        "CLAUDE_GATEWAY_ALLOW_LOOPBACK should be gated on HaveTelemetry so the "
        "SSRF guard stays strict when there is no sidecar"
    )
    after = text[m.end():m.end() + 120]
    assert re.search(r"Value:\s*'1'", after), "override should be set to '1'"


def test_telemetry_forward_target_is_loopback():
    """Pins the pairing: a loopback forward_to is what makes the override
    necessary. If this ever becomes a non-loopback host, the override should be
    revisited rather than left enabled."""
    text = _template_text()
    assert "url: http://localhost:4318" in text
