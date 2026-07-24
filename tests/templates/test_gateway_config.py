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


def _managed_policies(groups=None):
    """Parse one rendered variant of the managed: block into policy dicts.

    groups=None models the default render (ManagedCliGroups unset -> the
    else-branch !Sub); a list models the HaveManagedCli render. Both branches
    are block scalars under `- !Sub |` inside the Fn::Base64 !If.
    """
    block = _managed_b64_block()
    subs = [m.end() for m in re.finditer(r"- !Sub \|\n", block)]
    assert len(subs) == 2, f"expected 2 !Sub branches in GATEWAY_MANAGED_B64, got {len(subs)}"
    # branch 0 = HaveManagedCli (with groups), branch 1 = default (without)
    start = subs[0] if groups else subs[1]
    body_lines = block[start:].split("\n")
    base_indent = len(body_lines[0]) - len(body_lines[0].lstrip())
    yaml_lines = []
    for l in body_lines:
        if l.strip() == "":
            continue
        if len(l) - len(l.lstrip()) < base_indent:
            break
        yaml_lines.append(l[base_indent:])
    raw = "\n".join(yaml_lines)
    raw = raw.replace("${ManagedCliGroups}", ", ".join(groups or []))
    raw = raw.replace("${OpusModelId}", "claude-opus-4-8")
    raw = raw.replace("${SonnetModelId}", "claude-sonnet-4-5")
    return yaml.safe_load(raw)["managed"]["policies"]


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


def test_managed_b64_is_always_emitted():
    """The model allowlist must reach EVERY client, so the env var is
    unconditional - only its body varies on HaveManagedCli."""
    block = _managed_b64_block()
    assert "AWS::NoValue" not in block, (
        "GATEWAY_MANAGED_B64 must not be dropped when ManagedCliGroups is unset - "
        "the model allowlist has to be pushed regardless"
    )
    assert "Fn::Base64: !If" in block, "GATEWAY_MANAGED_B64 body should be an !If over two !Subs"
    assert "HaveManagedCli" in block, "GATEWAY_MANAGED_B64 body not keyed on HaveManagedCli"


@pytest.mark.parametrize("groups", [None, ["grp-a", "grp-b"]])
def test_catch_all_policy_must_be_last(groups):
    """A policy with no `match:` is a CATCH-ALL, and policy selection is
    FIRST-MATCH-WINS over a single policy - so a catch-all anywhere but the end
    shadows every policy after it.

    RUNTIME-VERIFIED against the mirrored 2.1.211 gateway (2026-07-24): with the
    catch-all first the gateway logs
        warn managed.policies[0] is a catch-all (match: {}) but is not the last
             entry - policies after it will never match. Move it to the end.
    and the group-scoped update lockdown becomes unreachable for everyone. It
    still BOOTS, so only this gate catches it.
    """
    policies = _managed_policies(groups)
    catch_alls = [i for i, p in enumerate(policies) if "match" not in p]
    assert catch_alls, "expected exactly one catch-all (model-allowlist) policy"
    assert catch_alls == [len(policies) - 1], (
        f"catch-all policy must be LAST; found at index(es) {catch_alls} of "
        f"{len(policies)} policies - everything after it is dead config"
    )


@pytest.mark.parametrize("groups", [None, ["grp-a", "grp-b"]])
def test_model_allowlist_is_pushed_to_every_user(groups):
    """The model-allowlist policy has NO `match:`, so it applies to every
    authenticated user - no Okta groups claim required.

    Without this the client falls back to its own built-in model menu, none of
    whose entries the gateway serves (live symptom: every model unauthorized).
    Group members still receive it: the gateway merges the catch-all's `cli` as
    a BASE into each earlier policy (runtime-verified - "after merge with
    catch-all base - changed keys: availableModels, enforceAvailableModels").
    """
    policies = _managed_policies(groups)
    allow = policies[-1]
    assert "match" not in allow, (
        "the model-allowlist policy must not be group-scoped - a `match:` here "
        "silently drops the allowlist for users outside those Okta groups"
    )
    cli = allow["cli"]
    # keys live INSIDE `cli` (the passthrough settings blob), not on the policy
    assert cli["availableModels"] == ["claude-opus-4-8", "claude-sonnet-4-5"]
    assert cli["enforceAvailableModels"] is True, (
        "without enforceAvailableModels the Default selection can still resolve "
        "to a model the gateway does not serve"
    )


def test_available_models_is_never_at_policy_level():
    """`availableModels` is only valid inside `cli`. At the policy level the
    gateway rejects it ("Unrecognized key(s) in object") and refuses to boot -
    binary-verified against the mirrored 2.1.211 gateway, 2026-07-24.
    """
    for groups in (None, ["grp-a"]):
        for policy in _managed_policies(groups):
            assert "availableModels" not in policy, (
                f"availableModels at policy level is a BOOT FAILURE: {policy!r}"
            )
            assert "enforceAvailableModels" not in policy, (
                f"enforceAvailableModels at policy level is a BOOT FAILURE: {policy!r}"
            )


def test_update_lockdown_policy_matches_groups_only_when_configured():
    """The update-lockdown policy is group-scoped and appears only when
    ManagedCliGroups is set; the default render carries the allowlist alone.

    It must come BEFORE the catch-all (see test_catch_all_policy_must_be_last),
    otherwise it is never reached.
    """
    assert len(_managed_policies(None)) == 1, (
        "default render should carry only the model-allowlist policy"
    )
    policies = _managed_policies(["grp-a", "grp-b"])
    assert len(policies) == 2, "HaveManagedCli render should add the lockdown policy"
    lockdown = policies[0]
    assert lockdown["match"]["groups"] == ["grp-a", "grp-b"], lockdown["match"]
    env = lockdown["cli"]["env"]
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
