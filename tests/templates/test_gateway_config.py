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

import json
import os
import re
import sys

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


def _load_gateway_config(scopes_line=False, admin_groups=False):
    """Parse the config body, simulating the Sub-var substitutions.

    scopes_line toggles whether the (now-unconditional) OidcScopesLine content
    is included in the parse; admin_groups toggles the SpendAdminGroupsLine
    render: False is the default empty `admin_groups: []`, True the
    HaveSpendAdminGroups branch.
    """
    raw = _extract_config_block()
    repl = "scopes: [openid, profile, email, offline_access, groups]" if scopes_line else ""
    raw = raw.replace("${OidcScopesLine}", repl)
    admin_repl = ("admin_groups: [claude-spend-admins]" if admin_groups
                  else "admin_groups: []")
    raw = raw.replace("${SpendAdminGroupsLine}", admin_repl)
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


def _managed_body():
    """Return the raw managed-block body (first !Sub list element).

    Since the MinClientVersion floor landed (2026-07-28) the env var carries a
    TWO-ARG Fn::Base64 !Sub - the body is the block scalar under `- |`, and the
    second list element supplies RequiredMinVersionLine (the same shape as
    GATEWAY_CONFIG_B64's OidcScopesLine). The loop stops at the shallower
    `- RequiredMinVersionLine` element that follows the body.
    """
    block = _managed_b64_block()
    lines = block.split("\n")
    subi = next(
        (i for i, l in enumerate(lines) if l.strip() == "- |"), None
    )
    assert subi is not None, (
        "GATEWAY_MANAGED_B64 should be a two-arg Fn::Base64 !Sub (body under '- |')"
    )
    base_indent = len(lines[subi + 1]) - len(lines[subi + 1].lstrip())
    body = []
    for l in lines[subi + 1:]:
        if l.strip() == "":
            continue
        if len(l) - len(l.lstrip()) < base_indent:
            break
        body.append(l[base_indent:])
    return "\n".join(body)


def _managed_policies(min_version=True, log_prompts=False, log_responses=False,
                      claude_md=None, disable_recaps=False, marketplaces=None,
                      plugins=None):
    """Parse the rendered managed: block into policy dicts.

    min_version toggles the RequiredMinVersionLine render: True is the
    HaveMinClientVersion branch (the deploy-gateway.sh default - it always
    passes CLAUDE_VERSION unless MIN_CLIENT_VERSION=none), False the
    disabled branch (a YAML comment line, so the block must stay parseable).
    log_prompts / log_responses toggle the two capture flags the same way;
    False is the template default for both (capture is opt-in). The
    responses line reproduces the template's three-way !If: own flag on ->
    "1"; off with prompts on -> pinned "0"; both off -> comment.
    claude_md, when set, is the RAW rules text: it is JSON-string-encoded
    here exactly as deploy-gateway.sh's json_string_from_file does before
    rendering the ManagedClaudeMdLine enabled branch; None is the disabled
    branch (a YAML comment line).
    disable_recaps toggles the AwaySummaryLine render the same way
    (DisableSessionRecaps='true' vs the default comment line).
    marketplaces / plugins, when set, are dicts encoded to the compact
    single-line JSON that common.sh's managed_marketplaces_json /
    managed_plugins_json produce for the two parameters; None is the
    disabled branch (a YAML comment line).
    """
    raw = _managed_body()
    repl = ("requiredMinimumVersion: '2.1.207'" if min_version
            else "# requiredMinimumVersion: not set (MinClientVersion empty)")
    raw = raw.replace("${RequiredMinVersionLine}", repl)
    repl = ("claudeMd: " + json.dumps(claude_md) if claude_md is not None
            else "# claudeMd: not set (CLAUDE_MD_FILE empty)")
    raw = raw.replace("${ManagedClaudeMdLine}", repl)
    repl = ("awaySummaryEnabled: false" if disable_recaps
            else "# awaySummaryEnabled: default (DisableSessionRecaps=false)")
    raw = raw.replace("${AwaySummaryLine}", repl)
    repl = ("extraKnownMarketplaces: "
            + json.dumps(marketplaces, separators=(",", ":"))
            if marketplaces is not None
            else "# extraKnownMarketplaces: not set (PLUGIN_MARKETPLACE_NAME empty)")
    raw = raw.replace("${ExtraMarketplacesLine}", repl)
    repl = ("enabledPlugins: " + json.dumps(plugins, separators=(",", ":"))
            if plugins is not None
            else "# enabledPlugins: not set (MANAGED_PLUGINS empty)")
    raw = raw.replace("${EnabledPluginsLine}", repl)
    repl = ('OTEL_LOG_USER_PROMPTS: "1"' if log_prompts
            else "# OTEL_LOG_USER_PROMPTS: not enabled (LogUserPrompts=false)")
    raw = raw.replace("${LogUserPromptsLine}", repl)
    repl = ('OTEL_LOG_ASSISTANT_RESPONSES: "1"' if log_responses
            else ('OTEL_LOG_ASSISTANT_RESPONSES: "0"' if log_prompts
                  else "# OTEL_LOG_ASSISTANT_RESPONSES: default (capture off)"))
    raw = raw.replace("${LogAssistantResponsesLine}", repl)
    raw = raw.replace("${OpusModelId}", "claude-opus-4-8")
    raw = raw.replace("${SonnetModelId}", "claude-sonnet-5")
    raw = raw.replace("${HaikuModelId}", "claude-sonnet-4-5")
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
    unconditional - no NoValue branch. (The RequiredMinVersionLine Sub var
    carries an !If, but both branches are strings, so the env var itself is
    always emitted.)"""
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
    # keys live INSIDE `cli` (Claude Code settings.json keys), not on the
    # policy. Order matters for readers: Opus (the default) first, then the
    # Sonnet tier, then the small/fast haiku-role model (Sonnet 4.5 - GovCloud
    # has no Haiku family).
    assert cli["availableModels"] == [
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-sonnet-4-5",
    ]
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


def test_tool_denies_and_small_model_override_reach_every_user():
    """The catch-all also carries the web/MCP tool denies and the
    small/fast-model override (added 2026-07-24).

    - A BARE tool name in `permissions.deny` removes the tool from the model's
      context entirely - stronger than a scoped `WebFetch(domain:*)` deny,
      which only blocks matching calls - and `mcp__*` covers every tool of
      every MCP server. Managed-scope denies union across scopes and cannot be
      re-allowed, so none of this is user-overridable. WebSearch is server-side
      and Bedrock does not expose it anyway (defense-in-depth). `Agent`
      (subagents) is deliberately NOT denied (decision 2026-07-24), and
      `WebFetch` was deliberately REMOVED from the deny list (decision
      2026-08-07, commit "SB -- Allowing WebFetch": clients fetch locally
      through the Zscaler egress); this gate pins the exact deny list so a
      silent widening or narrowing fails loudly.
    - ANTHROPIC_DEFAULT_HAIKU_MODEL must be the GATEWAY-facing haiku-role ID
      (HaikuModelId - Sonnet 4.5, the same value as its availableModels
      entry), NOT the Bedrock inference profile ID - the client asks the
      gateway, and the gateway's `models:` block does the Bedrock mapping.
      Without the override, background / small-model calls request a
      Haiku-family model that neither GovCloud nor this gateway serves.
    """
    cli = _managed_policies()[-1]["cli"]
    assert cli["permissions"]["deny"] == ["WebSearch", "mcp__*"], (
        "the managed deny list must be exactly ['WebSearch', 'mcp__*'] - "
        "Agent (subagents) stays allowed by decision (2026-07-24), WebFetch "
        "by decision (2026-08-07)"
    )
    assert cli["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-sonnet-4-5", (
        "small/fast model must be the gateway-served haiku-role ID "
        "(HaikuModelId, same as its availableModels entry), or background "
        "calls request an unserved model"
    )
    assert cli["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] in cli["availableModels"], (
        "the haiku-role override must reference a model the allowlist carries"
    )


def test_required_minimum_version_inside_cli_and_conditional():
    """The minimum-client-version floor (added 2026-07-28).

    - The key must sit INSIDE `cli` (it is a Claude Code settings.json key;
      BOOT-VERIFIED against the mirrored 2.1.211 AND downloaded 2.1.207
      gateway binaries: inside cli boots, a typo'd key fails boot with
      "unknown settings key").
    - It renders from the RequiredMinVersionLine Sub var, gated on
      HaveMinClientVersion, and the disabled branch must be a YAML COMMENT so
      the block parses either way (never a dangling key).
    - The value is single-quoted and comes from the MinClientVersion
      parameter, never a hardcoded version.
    """
    # enabled branch: key lands inside the catch-all policy's cli block
    cli = _managed_policies(min_version=True)[-1]["cli"]
    assert cli["requiredMinimumVersion"] == "2.1.207"
    # disabled branch: block still parses, key absent everywhere
    for policy in _managed_policies(min_version=False):
        assert "requiredMinimumVersion" not in policy
        assert "requiredMinimumVersion" not in policy.get("cli", {})
    # never at policy level (unrecognized key there = boot failure)
    for policy in _managed_policies(min_version=True):
        assert "requiredMinimumVersion" not in policy, (
            f"requiredMinimumVersion at policy level is a BOOT FAILURE: {policy!r}"
        )
    text = _template_text()
    assert re.search(r"RequiredMinVersionLine:\s*!If", text), (
        "RequiredMinVersionLine should be conditional on HaveMinClientVersion"
    )
    assert "HaveMinClientVersion" in text
    assert "requiredMinimumVersion: '${MinClientVersion}'" in text, (
        "the floor must come from the MinClientVersion parameter"
    )
    # pin the disabled branch too - _managed_policies constructs the comment
    # string itself, so only this raw-text check fails if the !If else-branch
    # drifts to something that is not a full-line YAML comment
    assert "- '# requiredMinimumVersion: not set (MinClientVersion empty)'" in text, (
        "the HaveMinClientVersion else-branch must stay a full-line YAML "
        "comment - an empty string or dangling key breaks the rendered block"
    )
    # the marker itself sits inside the cli: block of the body
    body = _managed_body()
    cli_ix = body.index("- cli:")
    marker_ix = body.index("${RequiredMinVersionLine}")
    assert cli_ix < marker_ix, "RequiredMinVersionLine must render inside cli:"


def test_managed_claude_md_inside_cli_and_conditional():
    """Organization-wide Claude rules push (claudeMd, added 2026-07-30).

    - `claudeMd` must sit INSIDE `cli` (it is a managed-settings key;
      BINARY-VERIFIED against the mirrored 2.1.211 gateway 2026-07-30:
      inside cli it boots and /managed/settings serves the content verbatim;
      a typo'd key in the same spot fails boot with "unknown settings key").
    - The content arrives as a SINGLE-LINE JSON string literal (produced by
      json_string_from_file) rendered after `claudeMd: ` - a JSON string is
      a valid YAML double-quoted scalar, so arbitrary markdown (newlines,
      quotes, ${...}, YAML-special leading characters) must round-trip.
    - It renders from the ManagedClaudeMdLine Sub var, gated on
      HaveManagedClaudeMd; the disabled branch must be a full-line YAML
      COMMENT so the block parses either way.
    """
    # $VAR (no brace) is legal rules content; ${VAR} is NOT - the gateway
    # expands ${NAME} in config values as env vars, so the deploy script and
    # the parameter pattern both reject it (see the rejects-raw test below)
    rules = ('# Org rules\n- no "secrets" on a CLI (`ps` shows $VAR)\n'
             '- path C:\\temp\n#not a comment\n')
    cli = _managed_policies(claude_md=rules)[-1]["cli"]
    assert cli["claudeMd"] == rules, "claudeMd must round-trip the raw markdown"
    # disabled branch (the default): block still parses, key absent everywhere
    for policy in _managed_policies(claude_md=None):
        assert "claudeMd" not in policy
        assert "claudeMd" not in policy.get("cli", {})
    # never at policy level (unrecognized key there = boot failure)
    for policy in _managed_policies(claude_md=rules):
        assert "claudeMd" not in policy, (
            f"claudeMd at policy level is a BOOT FAILURE: {policy!r}"
        )
    text = _template_text()
    assert re.search(r"ManagedClaudeMdLine:\s*!If", text), (
        "ManagedClaudeMdLine should be conditional on HaveManagedClaudeMd"
    )
    assert "HaveManagedClaudeMd" in text
    assert "claudeMd: ${ManagedClaudeMd}" in text, (
        "the rules content must come from the ManagedClaudeMd parameter"
    )
    assert "- '# claudeMd: not set (CLAUDE_MD_FILE empty)'" in text, (
        "the HaveManagedClaudeMd else-branch must stay a full-line YAML "
        "comment - an empty string or dangling key breaks the rendered block"
    )
    # the marker itself sits inside the cli: block of the body
    body = _managed_body()
    cli_ix = body.index("- cli:")
    marker_ix = body.index("${ManagedClaudeMdLine}")
    assert cli_ix < marker_ix, "ManagedClaudeMdLine must render inside cli:"


def test_managed_claude_md_parameter_rejects_raw_markdown():
    r"""The ManagedClaudeMd parameter must only accept the encoded shape: empty,
    or one double-quoted single-line JSON string. Raw multi-line markdown
    pasted into the parameter (bypassing deploy-gateway.sh) would render an
    unparseable managed block - a gateway BOOT LOOP - so the AllowedPattern
    has to catch it at deploy time. The character classes exclude every
    line-break character (all five Java line terminators - LF, CR, NEL,
    LS, PS - see the template comment), which is what rejects the
    multi-line case. The pattern must ALSO reject any `${` inside the
    content: the gateway expands ${NAME} in config values as env vars
    after YAML parsing - boot failure when undefined, silent substitution
    when defined, no escape syntax (all binary-verified against the
    mirrored 2.1.211 gateway 2026-07-30). Excluding the non-LF
    terminators is part of that guard too: Java's `.` skips all five, so
    with an LF-only class a CR before the `${` would blind the lookahead
    in CFN while this Python suite still saw it - divergence in the
    dangerous direction.

    The pattern string itself is pinned EXACTLY: CloudFormation evaluates
    AllowedPattern with Java's regex engine, whose per-iteration recursion
    on alternation-group stars StackOverflowErrors (an opaque
    InternalFailure) at ~2550 chars with the naive `(\\.|[^"\\\n])*` body,
    making MaxLength 4096 unreachable. The unrolled-possessive form matches
    the same language iteratively (JVM-calibrated to 4096 chars at a 64 KB
    stack, 2026-07-30 - see the template comment). Python cannot exercise
    the Java stack behavior, so any pattern change must re-run that JVM
    verification - hence the exact-string pin. Possessive quantifiers need
    Python >= 3.11.5 (gh-106052 fixed possessive-group-star matching in
    3.11.5) - the floor assert below makes an older interpreter fail
    loudly instead of silently mis-validating."""
    assert sys.version_info >= (3, 11, 5), (
        "Python >= 3.11.5 required: possessive quantifiers arrived in 3.11 "
        "and gh-106052 (possessive group-star mismatch) was fixed in 3.11.5"
    )
    text = _template_text()
    blk = text[text.index("ManagedClaudeMd:"):]
    m = re.search(r"AllowedPattern:\s*'([^']+)'", blk)
    assert m, "ManagedClaudeMd has no AllowedPattern"
    pat = m.group(1)
    # The expected pattern is built from pieces so no literal
    # backslash-u sequence (or raw control character) appears in this
    # source file; the result is byte-identical to the template text.
    bs = chr(92)
    line_terms = bs + "u0085" + bs + "u2028" + bs + "u2029"
    body_char = '[^"' + bs * 2 + bs + "n" + bs + "r" + line_terms + "]"
    esc_char = "[^" + bs + "n" + bs + "r" + line_terms + "]"
    expected = (
        "((?!.*" + bs + "$" + bs + '{)"' + body_char + "*+("
        + bs * 2 + esc_char + body_char + '*+)*+")?'
    )
    assert pat == expected, (
        "ManagedClaudeMd AllowedPattern changed: the unrolled-possessive "
        "shape is load-bearing (CFN Java-regex StackOverflow at ~2550 chars "
        "otherwise), and the char classes must exclude ALL FIVE Java line "
        "terminators (a CR/NEL/LS/PS blinds the `${` lookahead in Java and "
        "is itself a YAML line break); re-run the JVM stack verification "
        "before repinning"
    )
    assert re.fullmatch(pat, "")  # empty = feature off
    assert re.fullmatch(pat, json.dumps('# Rules\n- with "quotes" and $VAR\n'))
    # Full-length values must be accepted: plain filler and the
    # all-escape-pairs worst case, both at the 4096-char MaxLength.
    assert re.fullmatch(pat, '"' + "x" * 4094 + '"')
    assert re.fullmatch(pat, '"' + "\\n" * 2047 + '"')
    assert re.fullmatch(pat, json.dumps("# Rules\n" + "- rule with $ {x}\n" * 150))
    for bad in (
        "# Raw markdown\n- not encoded",              # raw multi-line
        "single line without quotes",                  # unquoted
        '"opens but never closes',                     # not a closed string
        json.dumps("uses ${VAR} in a shell example"),  # gateway env expansion
        '"a "b" c"',        # unescaped inner quotes - not a JSON string
        '"abc\\"',          # trailing backslash escapes the closing quote
        '"real\nnewline"',  # quoted but multi-line - broken rendered YAML
        '"' + "x" * 4080 + '${A}"',   # ${ near the end of a max-length value
        '"' + "x" * 4095,             # max-length but never closed
        '"' + "x" * 4093 + '\\"',     # max-length, trailing backslash
        '"a' + chr(0x0D) + 'b"',            # raw CR - a YAML line break too
        '"a' + chr(0x0D) + '${X}"',         # CR blinds Java's `.*` lookahead
        '"a' + chr(0x85) + '${X}"',         # NEL, same bypass
        '"a' + chr(0x2028) + '${X}"',       # LS, same bypass
        '"a' + chr(0x2029) + '${X}"',       # PS, same bypass
        '"a' + chr(92) + chr(0x0D) + 'b"',  # backslash-CR is not an escape pair
    ):
        assert not re.fullmatch(pat, bad), f"AllowedPattern accepts {bad!r}"


def test_deploy_script_encodes_and_bounds_claude_md_file():
    """deploy-gateway.sh must encode CLAUDE_MD_FILE via json_string_from_file
    (the common.sh helper the bats suite pins), refuse a missing file, refuse
    an encoded value over the 4096-char CFN parameter limit, and pass the
    result through as ManagedClaudeMd (empty when CLAUDE_MD_FILE is unset)."""
    script = open(os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "deploy-gateway.sh"
    )).read()
    assert 'json_string_from_file "$CLAUDE_MD_FILE"' in script, (
        "deploy-gateway.sh must encode CLAUDE_MD_FILE with json_string_from_file"
    )
    assert "ManagedClaudeMd=${MANAGED_CLAUDE_MD}" in script, (
        "deploy-gateway.sh must pass ManagedClaudeMd through"
    )
    assert re.search(r'-gt 4096\b', script), (
        "deploy-gateway.sh must enforce the 4096-char CFN parameter limit "
        "on the ENCODED value (the CFN-side MaxLength would fail with an "
        "opaque error instead of naming CLAUDE_MD_FILE)"
    )
    assert re.search(r'\[ ! -f "\$CLAUDE_MD_FILE" \]', script), (
        "deploy-gateway.sh must fail closed on a missing CLAUDE_MD_FILE"
    )
    assert "grep -Fq '${' \"$CLAUDE_MD_FILE\"" in script, (
        "deploy-gateway.sh must reject rules content containing '${' - the "
        "gateway expands ${NAME} in config values as env vars (boot failure "
        "when undefined, silent substitution when defined; no escape syntax)"
    )
    assert re.search(r'\[ ! -s "\$CLAUDE_MD_FILE" \]', script), (
        "deploy-gateway.sh must reject an EMPTY CLAUDE_MD_FILE - it would "
        "encode to '\"\"' (a non-empty parameter) and push empty managed "
        "memory to every client instead of no claudeMd key"
    )
    common = open(os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "common.sh"
    )).read()
    assert "json_string_from_file()" in common, (
        "json_string_from_file must live in common.sh (reused, bats-tested)"
    )


def test_away_summary_disable_inside_cli_and_conditional():
    """Session-recap disable (awaySummaryEnabled, added 2026-08-07).

    - `awaySummaryEnabled` must sit INSIDE `cli` (it is a Claude Code
      settings.json key; BOOT-VERIFIED against the mirrored 2.1.211 and
      2.1.220 gateways 2026-08-07: inside cli it boots, a typo'd variant
      fails boot with "unknown settings key"). One key covers both recap
      surfaces: the away-session recap and the remote-recap variant, whose
      client-side gate checks `awaySummaryEnabled === false` (verified in
      the 2.1.211/2.1.220 client bundles).
    - It renders from the AwaySummaryLine Sub var, gated on
      WantDisableSessionRecaps; the disabled branch (the default) must be a
      full-line YAML comment so the block parses either way.
    """
    cli = _managed_policies(disable_recaps=True)[-1]["cli"]
    assert cli["awaySummaryEnabled"] is False
    # default: block still parses, key absent everywhere
    for policy in _managed_policies(disable_recaps=False):
        assert "awaySummaryEnabled" not in policy
        assert "awaySummaryEnabled" not in policy.get("cli", {})
    # never at policy level (unrecognized key there = boot failure)
    for policy in _managed_policies(disable_recaps=True):
        assert "awaySummaryEnabled" not in policy, (
            f"awaySummaryEnabled at policy level is a BOOT FAILURE: {policy!r}"
        )
    text = _template_text()
    assert re.search(r"AwaySummaryLine:\s*!If", text), (
        "AwaySummaryLine should be conditional on WantDisableSessionRecaps"
    )
    assert "WantDisableSessionRecaps" in text
    assert "- 'awaySummaryEnabled: false'" in text
    assert "- '# awaySummaryEnabled: default (DisableSessionRecaps=false)'" in text, (
        "the WantDisableSessionRecaps else-branch must stay a full-line YAML "
        "comment - an empty string or dangling key breaks the rendered block"
    )
    # the parameter default is the safe side (recaps stay on)
    m = re.search(r"^  DisableSessionRecaps:\n(?:.*\n)*?    Default: '(\w+)'",
                  text, re.M)
    assert m and m.group(1) == "false", (
        "DisableSessionRecaps must default to 'false'"
    )
    # the marker sits inside the cli: block of the body
    body = _managed_body()
    assert body.index("- cli:") < body.index("${AwaySummaryLine}")


def test_managed_plugin_push_inside_cli_and_conditional():
    """Enterprise skill push (extraKnownMarketplaces + enabledPlugins, added
    2026-08-07). Skills have NO direct managed-settings distribution key -
    they ship inside a PLUGIN from an org-hosted marketplace, registered via
    extraKnownMarketplaces and force-installed via enabledPlugins.

    - Both keys must sit INSIDE `cli` (Claude Code settings.json keys;
      BOOT-VERIFIED against the mirrored 2.1.211 and 2.1.220 gateways
      2026-08-07 with the exact flow-style JSON values common.sh renders).
    - Both values are MAPS, not arrays (the client settings schema:
      enabledPlugins is a record of "plugin@marketplace" -> bool,
      extraKnownMarketplaces a record of name -> {source: {...}}).
    - Each renders from its own Sub var, gated on its own Have* condition
      (a marketplace push without force-installed plugins is legitimate);
      the disabled branches must be full-line YAML comments.
    """
    mkt = {"org-plugins": {
        "source": {"source": "github", "repo": "example-org/claude-plugins"},
        "autoUpdate": True,
    }}
    plugins = {"org-skills@org-plugins": True}
    cli = _managed_policies(marketplaces=mkt, plugins=plugins)[-1]["cli"]
    assert cli["extraKnownMarketplaces"] == mkt, (
        "extraKnownMarketplaces must round-trip the composed JSON as a YAML map"
    )
    assert cli["enabledPlugins"] == plugins, (
        "enabledPlugins must round-trip the composed JSON as a YAML map"
    )
    # marketplace-only render (no forced plugins) is a valid combination
    cli = _managed_policies(marketplaces=mkt)[-1]["cli"]
    assert cli["extraKnownMarketplaces"] == mkt
    assert "enabledPlugins" not in cli
    # default: block still parses, keys absent everywhere
    for policy in _managed_policies():
        for key in ("extraKnownMarketplaces", "enabledPlugins"):
            assert key not in policy
            assert key not in policy.get("cli", {})
    # never at policy level (unrecognized key there = boot failure)
    for policy in _managed_policies(marketplaces=mkt, plugins=plugins):
        for key in ("extraKnownMarketplaces", "enabledPlugins"):
            assert key not in policy, (
                f"{key} at policy level is a BOOT FAILURE: {policy!r}"
            )
    text = _template_text()
    for var, cond, param, off in (
        ("ExtraMarketplacesLine", "HaveManagedExtraMarketplaces",
         "ManagedExtraMarketplaces",
         "- '# extraKnownMarketplaces: not set (PLUGIN_MARKETPLACE_NAME empty)'"),
        ("EnabledPluginsLine", "HaveManagedEnabledPlugins",
         "ManagedEnabledPlugins",
         "- '# enabledPlugins: not set (MANAGED_PLUGINS empty)'"),
    ):
        assert re.search(var + r":\s*!If", text), (
            f"{var} should be conditional on {cond}"
        )
        assert cond in text
        assert "${" + param + "}" in text, (
            f"the value must come from the {param} parameter"
        )
        assert off in text, (
            f"the {cond} else-branch must stay a full-line YAML comment"
        )
    body = _managed_body()
    cli_ix = body.index("- cli:")
    for marker in ("${ExtraMarketplacesLine}", "${EnabledPluginsLine}"):
        assert cli_ix < body.index(marker), f"{marker} must render inside cli:"


def test_managed_plugin_parameters_reject_dollar_and_line_breaks():
    """Both plugin-push parameters accept only the composed shape: empty, or
    one single-line JSON object. Char-class-only patterns ON PURPOSE -
    iterative in Java's regex engine, none of the alternation-group
    StackOverflow risk pinned on ManagedClaudeMd - and they exclude `$`
    OUTRIGHT (the gateway env-expands ${NAME} in config values, and no
    legitimate marketplace name/repo/URL contains `$`) plus all five Java
    line terminators (each is a YAML line break -> gateway boot loop)."""
    text = _template_text()
    for param in ("ManagedExtraMarketplaces", "ManagedEnabledPlugins"):
        blk = text[text.index(param + ":"):]
        m = re.search(r"AllowedPattern:\s*'([^']+)'", blk)
        assert m, f"{param} has no AllowedPattern"
        pat = m.group(1)
        # built from pieces like the ManagedClaudeMd pin, so no literal
        # backslash-u sequence appears in this source file; the result is
        # byte-identical to the template text
        bs = chr(92)
        expected = ("(" + bs + "{[^$" + bs + "n" + bs + "r" + bs + "u0085"
                    + bs + "u2028" + bs + "u2029]*" + bs + "})?")
        assert pat == expected, (
            f"{param} AllowedPattern changed: keep it a bare char class "
            "(no alternation groups - Java-regex StackOverflow class) that "
            "excludes '$' and ALL FIVE Java line terminators"
        )
        assert re.fullmatch(pat, "")  # empty = feature off
        assert re.fullmatch(
            pat, '{"org-plugins":{"source":{"source":"github",'
                 '"repo":"example-org/claude-plugins"},"autoUpdate":true}}'
        )
        assert re.fullmatch(pat, '{"org-skills@org-plugins":true}')
        for bad in (
            "extraKnownMarketplaces:\n  org: {}",   # raw YAML, multi-line
            '{"a":true}\n{"b":true}',               # embedded newline
            '{"repo":"x/${VAR}"}',                  # gateway env expansion
            '{"url":"https://h/$x"}',               # bare $ - still excluded
            '"just-a-string"',                      # not an object
            '{"a"' + chr(0x0D) + ':true}',          # raw CR
            '{"a"' + chr(0x85) + ':true}',          # NEL
            '{"a"' + chr(0x2028) + ':true}',        # LS
            '{"a"' + chr(0x2029) + ':true}',        # PS
        ):
            assert not re.fullmatch(pat, bad), (
                f"{param} AllowedPattern accepts {bad!r}"
            )


def test_deploy_script_wires_recap_and_plugin_push():
    """deploy-gateway.sh must validate DISABLE_SESSION_RECAPS, compose the two
    plugin-push JSON values via the common.sh helpers (never inline), refuse
    MANAGED_PLUGINS without PLUGIN_MARKETPLACE_NAME (a force-installed plugin
    from an unregistered marketplace can never install), and pass all three
    parameters through."""
    script = open(os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "deploy-gateway.sh"
    )).read()
    assert "DisableSessionRecaps=${DISABLE_SESSION_RECAPS:-false}" in script, (
        "deploy-gateway.sh must pass DisableSessionRecaps (default false)"
    )
    assert "ManagedExtraMarketplaces=${MANAGED_EXTRA_MARKETPLACES}" in script
    assert "ManagedEnabledPlugins=${MANAGED_ENABLED_PLUGINS}" in script
    assert "managed_marketplaces_json" in script, (
        "the marketplace JSON must come from the common.sh composer"
    )
    assert "managed_plugins_json" in script, (
        "the plugins JSON must come from the common.sh composer"
    )
    # the recap flag is validated with a named error that actually ABORTS -
    # pin the exit inside the case's catch-all branch, not just the case line
    m = re.search(
        r'case "\$\{DISABLE_SESSION_RECAPS:-false\}" in\n(.*?)\nesac',
        script, re.S,
    )
    assert m, "DISABLE_SESSION_RECAPS must be validated with a case statement"
    assert re.search(r"\*\)\n(?:.*\n)*?\s*exit 1", m.group(1)), (
        "the DISABLE_SESSION_RECAPS catch-all branch must exit 1 - a warning "
        "there would deploy a typo'd value to an opaque CFN error"
    )
    # pairing guard: plugins without a marketplace is refused WITH an exit
    m = re.search(
        r'elif \[ -n "\$\{MANAGED_PLUGINS:-\}" \]; then\n((?:.*\n)*?)fi',
        script,
    )
    assert m, "MANAGED_PLUGINS without PLUGIN_MARKETPLACE_NAME must fail closed"
    assert "exit 1" in m.group(1), (
        "the MANAGED_PLUGINS-without-marketplace branch must exit 1"
    )
    # a marketplace name without a location is refused before composing
    assert re.search(
        r'\[ -z "\$\{PLUGIN_MARKETPLACE_LOCATION:-\}" \]\s*;?\s*then\n'
        r"(?:.*\n)*?\s*exit 1",
        script,
    ), "PLUGIN_MARKETPLACE_NAME without PLUGIN_MARKETPLACE_LOCATION must exit 1"
    # the 1024-char CFN MaxLength is pre-checked with an error naming the vars
    assert re.search(r"-gt 1024\b(?:.*\n)*?\s*exit 1", script), (
        "the composed JSON values must be bounded against the 1024-char "
        "parameter MaxLength with a named fatal, not left to an opaque CFN error"
    )
    # pin the composer ARGUMENT ORDER - swapping REF and AUTO_UPDATE would
    # pass every other gate and ship autoUpdate as a git ref
    assert re.search(
        r'managed_marketplaces_json \\\n'
        r'\s*"\$PLUGIN_MARKETPLACE_NAME" \\\n'
        r'\s*"\$\{PLUGIN_MARKETPLACE_SOURCE:-github\}" \\\n'
        r'\s*"\$PLUGIN_MARKETPLACE_LOCATION" \\\n'
        r'\s*"\$\{PLUGIN_MARKETPLACE_REF:-\}" \\\n'
        r'\s*"\$\{PLUGIN_MARKETPLACE_AUTO_UPDATE:-true\}"',
        script,
    ), ("managed_marketplaces_json must be called as NAME SOURCE LOCATION "
        "REF AUTO_UPDATE (defaults github / empty ref / autoUpdate on)")
    assert re.search(
        r'managed_plugins_json \\\n'
        r'\s*"\$PLUGIN_MARKETPLACE_NAME" "\$MANAGED_PLUGINS"',
        script,
    ), "managed_plugins_json must be called as MARKETPLACE PLUGINS_CSV"
    common = open(os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "common.sh"
    )).read()
    assert "managed_marketplaces_json()" in common
    assert "managed_plugins_json()" in common


def test_log_user_prompts_is_opt_in_and_conditional():
    """OTEL_LOG_USER_PROMPTS (prompt-content capture, added 2026-07-29).

    - OFF is the default and must STAY the default: the env key renders only
      when LogUserPrompts='true'; the disabled branch is a full-line YAML
      comment so the env: block parses either way.
    - When enabled the value is exactly "1" inside the catch-all policy's
      cli.env - the same managed push every other client env var rides.
    - deploy-gateway.sh must refuse LOG_USER_PROMPTS=true without
      FORWARD_ACTIVITY_LOGS=true: prompt text travels inside the activity
      stream, so without forwarding it is silently dropped at the gateway
      while the operator believes prompts are being captured.
    - Response capture (LogAssistantResponses) is its OWN opt-in,
      independent of prompts. When responses are OFF while prompts are ON,
      OTEL_LOG_ASSISTANT_RESPONSES must be pinned to "0": unset, that knob
      FALLS BACK to OTEL_LOG_USER_PROMPTS (clients >= 2.1.193, per the
      monitoring docs), so omitting the pin silently un-redacts assistant
      response text the prompts flag never claimed to capture.
    """
    # both off (the default): block parses, keys absent everywhere
    for policy in _managed_policies(log_prompts=False, log_responses=False):
        env = policy.get("cli", {}).get("env", {})
        assert "OTEL_LOG_USER_PROMPTS" not in env
        assert "OTEL_LOG_ASSISTANT_RESPONSES" not in env
    # prompts only: response fallback explicitly pinned off
    cli = _managed_policies(log_prompts=True)[-1]["cli"]
    assert cli["env"]["OTEL_LOG_USER_PROMPTS"] == "1"
    assert cli["env"]["OTEL_LOG_ASSISTANT_RESPONSES"] == "0", (
        "without the explicit '0', OTEL_LOG_ASSISTANT_RESPONSES falls back "
        "to OTEL_LOG_USER_PROMPTS and captures assistant responses too"
    )
    # responses only: valid independently of prompts
    cli = _managed_policies(log_prompts=False, log_responses=True)[-1]["cli"]
    assert "OTEL_LOG_USER_PROMPTS" not in cli["env"]
    assert cli["env"]["OTEL_LOG_ASSISTANT_RESPONSES"] == "1"
    # both on
    cli = _managed_policies(log_prompts=True, log_responses=True)[-1]["cli"]
    assert cli["env"]["OTEL_LOG_USER_PROMPTS"] == "1"
    assert cli["env"]["OTEL_LOG_ASSISTANT_RESPONSES"] == "1"
    text = _template_text()
    assert re.search(r"LogUserPromptsLine:\s*!If", text), (
        "LogUserPromptsLine should be conditional on WantLogUserPrompts"
    )
    assert "WantLogUserPrompts" in text
    assert re.search(r"LogAssistantResponsesLine:\s*!If", text), (
        "LogAssistantResponsesLine should be conditional on "
        "WantLogAssistantResponses"
    )
    assert "WantLogAssistantResponses" in text
    assert "- '# OTEL_LOG_USER_PROMPTS: not enabled (LogUserPrompts=false)'" in text, (
        "the WantLogUserPrompts else-branch must stay a full-line YAML comment"
    )
    assert "- '# OTEL_LOG_ASSISTANT_RESPONSES: default (capture off)'" in text, (
        "the both-off else-branch must stay a full-line YAML comment"
    )
    # the parameter defaults are the safe side
    for param in ("LogUserPrompts", "LogAssistantResponses"):
        m = re.search(r"^  %s:\n(?:.*\n)*?    Default: '(\w+)'" % param,
                      text, re.M)
        assert m and m.group(1) == "false", f"{param} must default to 'false'"
    # the markers sit inside the env: block of the managed body
    body = _managed_body()
    env_ix = body.index("env:")
    for marker in ("${LogUserPromptsLine}", "${LogAssistantResponsesLine}"):
        assert env_ix < body.index(marker), (
            f"{marker} must render inside cli.env:"
        )
    # the deploy script refuses capture without the activity stream (the
    # guard spans lines, hence re.S), and passes both parameters
    script = open(os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "deploy-gateway.sh"
    )).read()
    assert re.search(
        r'LOG_USER_PROMPTS.*=.*"true".*\|\|.*LOG_ASSISTANT_RESPONSES.*=.*"true"'
        r'.*&&.*FORWARD_ACTIVITY_LOGS.*!=.*"true"',
        script, re.S,
    ), "deploy-gateway.sh must gate both capture flags on FORWARD_ACTIVITY_LOGS"
    assert "LogUserPrompts=${LOG_USER_PROMPTS:-false}" in script, (
        "deploy-gateway.sh must pass LogUserPrompts (default false)"
    )
    assert "LogAssistantResponses=${LOG_ASSISTANT_RESPONSES:-false}" in script, (
        "deploy-gateway.sh must pass LogAssistantResponses (default false)"
    )


def test_min_client_version_parameter_rejects_non_semver():
    """The value ships to every client's startup version check, and the client
    strips (fails open on) an invalid value - so a malformed version would
    silently disable enforcement. Both guards pin X.Y.Z: the template's
    AllowedPattern (direct deploys) and deploy-gateway.sh's fatal check
    (which names the deploy.env variable)."""
    text = _template_text()
    blk = text[text.index("MinClientVersion:"):]
    m = re.search(r"AllowedPattern:\s*'([^']+)'", blk)
    assert m, "MinClientVersion has no AllowedPattern"
    pat = m.group(1)
    assert re.fullmatch(pat, "2.1.207")
    assert re.fullmatch(pat, "")  # empty = feature off
    for bad in ("2.1", "v2.1.207", "2.1.207-rc1", "none", "latest"):
        assert not re.fullmatch(pat, bad), f"AllowedPattern accepts {bad!r}"
    script = open(os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "deploy-gateway.sh"
    )).read()
    assert 'MIN_CLIENT_VERSION="${MIN_CLIENT_VERSION:-${CLAUDE_VERSION:-}}"' in script, (
        "deploy-gateway.sh must default the floor to CLAUDE_VERSION"
    )
    assert "MinClientVersion=${MIN_CLIENT_VERSION}" in script, (
        "deploy-gateway.sh must pass MinClientVersion through"
    )


def test_managed_block_model_ids_are_parameterized_not_hardcoded():
    """The managed block must reference the model CFN parameters via raw
    ${...} Sub markers. _managed_policies() neutralizes those markers with
    str.replace, so a regression that hardcodes a literal ID (violating the
    no-hardcoded-values rule and silently diverging from an overridden
    deploy.env) would parse identically and pass every other gate - only a
    raw-text assertion can fail on it. (The models: block gets the same
    protection from test_gateway_serves_exactly_three_models.)"""
    block = _managed_b64_block()
    assert (
        "availableModels: ['${OpusModelId}', '${SonnetModelId}', '${HaikuModelId}']"
        in block
    ), "availableModels must be built from the three model CFN parameters"
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL: '${HaikuModelId}'" in block, (
        "the small/fast override must follow the HaikuModelId parameter"
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


def test_gateway_serves_exactly_three_models():
    """The gateway's `models:` list carries the three configured roles (Opus
    default, Sonnet tier, small/fast haiku-role Sonnet 4.5). The pushed
    availableModels allowlist repeats them by construction, so a menu entry
    the gateway does not serve - the original live failure - can only appear
    if these fall out of sync."""
    doc = _load_gateway_config()
    assert len(doc["models"]) == 3, (
        f"expected 3 model entries (Opus/Sonnet/Haiku-role), got "
        f"{len(doc['models'])}"
    )
    body = _extract_config_block()
    for param in ("${OpusModelId}", "${SonnetModelId}", "${HaikuModelId}"):
        assert f"- id: {param}" in body, f"models: is missing an entry for {param}"
    for param in (
        "${OpusBedrockModelId}",
        "${SonnetBedrockModelId}",
        "${HaikuBedrockModelId}",
    ):
        assert f"bedrock: {param}" in body, f"no Bedrock mapping for {param}"


def test_bedrock_iam_and_endpoint_policies_cover_all_three_models():
    """Both Bedrock scoping layers - the task-role policy and the
    bedrock-runtime VPC endpoint policy - must enumerate every configured
    model: its inference-profile ARN plus the derived foundation-model ARN
    (profile ID minus the us-gov. geo prefix). Nothing else gates this
    (no cfn-guard rule covers it), so an omitted model fails only at runtime
    as an AccessDenied on invoke."""
    text = _template_text()
    for param in ("OpusBedrockModelId", "SonnetBedrockModelId", "HaikuBedrockModelId"):
        profile_refs = text.count(f"inference-profile/${{{param}}}")
        assert profile_refs == 2, (
            f"{param}: expected the inference-profile ARN in BOTH the task-role "
            f"policy and the endpoint policy, found {profile_refs}"
        )
        derived_refs = text.count(f"!Join ['', !Split ['us-gov.', !Ref {param}]]")
        assert derived_refs == 2, (
            f"{param}: expected the derived foundation-model ARN in BOTH "
            f"policies, found {derived_refs}"
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


# ---------------------------------------------------------------------------
# admin_groups (bearer-token spend admin - the portal admin page's credential)


def test_admin_groups_renders_both_ways_and_validates():
    """SpendAdminGroupsLine must render an explicit empty list when the
    parameter is unset (never a dangling placeholder line) and a populated
    flow list when set. RUNTIME-VERIFIED (gateway 2.1.220, live store,
    2026-07-25): admin_groups members get WRITE via their own session token
    (Authorization: Bearer), admin_audit records oidc:<sub>, and a valid
    token WITHOUT the group is refused 401."""
    doc = _load_gateway_config()
    assert doc["admin"]["admin_groups"] == [], (
        "default render must be an explicit empty list - bearer admin off"
    )
    doc = _load_gateway_config(admin_groups=True)
    assert doc["admin"]["admin_groups"] == ["claude-spend-admins"]


def test_admin_groups_line_is_conditional_on_parameter():
    """The Sub var must be the HaveSpendAdminGroups !If (empty param = feature
    off), and the marker must sit INSIDE the admin: block in the body."""
    text = _template_text()
    assert re.search(r"SpendAdminGroupsLine:\s*!If", text), (
        "SpendAdminGroupsLine should be conditional on HaveSpendAdminGroups"
    )
    assert "HaveSpendAdminGroups" in text
    body = _extract_config_block()
    admin_ix = body.index("\nadmin:")
    marker_ix = body.index("${SpendAdminGroupsLine}")
    enforcement_ix = body.index("\nenforcement:")
    assert admin_ix < marker_ix < enforcement_ix, (
        "admin_groups must render inside the admin: block"
    )
