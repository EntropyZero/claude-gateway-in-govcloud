"""Group extraction/authorization + config parsing + dropdown validation."""

import pytest

from portal.authz import groups_from_claims, is_authorized
from portal.config import Config
from portal.selection import SelectionError, validate_cost_center, validate_selection


def test_groups_from_id_token():
    groups = groups_from_claims({"groups": ["a", "b"]}, None)
    assert groups == ["a", "b"]


def test_groups_userinfo_fallback():
    # ID token carried no groups (Okta org server behaviour); userinfo does.
    groups = groups_from_claims({}, {"groups": ["claude-gateway-users"]})
    assert groups == ["claude-gateway-users"]


def test_groups_union_dedup():
    groups = groups_from_claims({"groups": ["a"]}, {"groups": ["a", "c"]})
    assert groups == ["a", "c"]


def test_groups_scalar_coerced_to_list():
    assert groups_from_claims({"groups": "solo"}, None) == ["solo"]


def test_authorized_true_when_member():
    assert is_authorized(["x", "claude-gateway-users"], ["claude-gateway-users"])


def test_authorized_false_when_not_member():
    assert not is_authorized(["x", "y"], ["claude-gateway-users"])
    assert not is_authorized([], ["claude-gateway-users"])
    assert not is_authorized(None, ["claude-gateway-users"])


# --- multiple access groups (any-of) --------------------------------------

def test_authorized_true_when_member_of_any_group():
    allowed = ["claude-gateway-users", "platform-eng", "contractors"]
    assert is_authorized(["everyone", "platform-eng"], allowed)   # 2nd
    assert is_authorized(["contractors"], allowed)                # last
    assert is_authorized(["claude-gateway-users"], allowed)       # first


def test_authorized_false_when_member_of_no_listed_group():
    allowed = ["claude-gateway-users", "platform-eng"]
    assert not is_authorized(["everyone", "some-other-team"], allowed)
    assert not is_authorized([], allowed)
    assert not is_authorized(None, allowed)


def test_authorized_scalar_access_group_still_works():
    # A single group passed as a bare string is coerced, never iterated as
    # characters (regression guard against set("solo")).
    assert is_authorized(["solo"], "solo")
    assert not is_authorized(["s", "o", "l"], "solo")


def test_access_group_single_value_parses_to_one_element(env):
    env["ACCESS_GROUP"] = "claude-gateway-users"
    assert Config(env).access_groups == ["claude-gateway-users"]


def test_access_group_comma_list_parses_and_trims(env):
    env["ACCESS_GROUP"] = "claude-gateway-users, platform-eng ,contractors"
    assert Config(env).access_groups == [
        "claude-gateway-users", "platform-eng", "contractors"]


def test_access_group_empty_fails_fast(env):
    env["ACCESS_GROUP"] = "   "
    with pytest.raises(ValueError):
        Config(env)


def test_cost_center_teams_parses_mapping_ordered(config):
    assert config.cost_center_teams == {
        "CC-1000": ["platform", "data"], "CC-2000": ["security"]}
    assert config.cost_centers == ["CC-1000", "CC-2000"]


def test_cost_center_teams_trims_whitespace(env):
    env["PORTAL_COST_CENTER_TEAMS"] = " CC-1000 : platform | data , CC-2000:security "
    assert Config(env).cost_center_teams == {
        "CC-1000": ["platform", "data"], "CC-2000": ["security"]}


@pytest.mark.parametrize("raw", [
    "",                              # empty mapping = dead portal, fail fast
    "CC-1000",                       # no teams separator
    "CC-1000:",                      # empty team list
    ":platform",                     # empty cost center
    "CC-1000:plat form",             # space inside a team
    "CC-1000:a:b",                   # reserved delimiter inside a team
    "CC-1000:platform,CC-1000:data", # duplicate cost center
    "CC-1000:platform|platform",     # duplicate team within a cost center
])
def test_cost_center_teams_malformed_fails_boot(env, raw):
    env["PORTAL_COST_CENTER_TEAMS"] = raw
    with pytest.raises(ValueError, match="PORTAL_COST_CENTER_TEAMS"):
        Config(env)


def test_validate_selection_accepts_configured_pair(config):
    team, cc = validate_selection("platform", "CC-1000", config)
    assert (team, cc) == ("platform", "CC-1000")


def test_validate_selection_rejects_unlisted_team(config):
    with pytest.raises(SelectionError, match="team"):
        validate_selection("marketing", "CC-1000", config)


def test_validate_selection_rejects_team_of_other_cost_center(config):
    # 'security' is a real team - but it belongs to CC-2000, not CC-1000. The
    # pairing is what's validated, not membership in a global list.
    with pytest.raises(SelectionError, match="not an allowed value for"):
        validate_selection("security", "CC-1000", config)


def test_validate_selection_rejects_unlisted_cost_center(config):
    with pytest.raises(SelectionError, match="cost_center"):
        validate_selection("platform", "CC-9999", config)


def test_validate_selection_rejects_missing(config):
    with pytest.raises(SelectionError):
        validate_selection(None, "CC-1000", config)


def test_validate_selection_rejects_injection_chars(config):
    # Even if someone slipped a comma/space value into the configured mapping,
    # the token cleanliness check (mirrors the installer's ValidatePattern)
    # rejects it. Simulate by pointing config at a dirty mapping.
    config.cost_center_teams = {"CC-1000": ["bad team"]}
    with pytest.raises(SelectionError, match="spaces or commas"):
        validate_selection("bad team", "CC-1000", config)


def test_validate_cost_center_accepts_configured_value(config):
    assert validate_cost_center("CC-2000", config) == "CC-2000"


def test_validate_cost_center_rejects_unlisted_and_missing(config):
    with pytest.raises(SelectionError):
        validate_cost_center("CC-9999", config)
    with pytest.raises(SelectionError):
        validate_cost_center(None, config)


# --- new v2 config knobs ---------------------------------------------------


def test_spend_read_key_optional_and_stripped(env):
    env.pop("SPEND_READ_KEY", None)
    assert Config(env).spend_read_key == ""
    env["SPEND_READ_KEY"] = "  k-123  "
    assert Config(env).spend_read_key == "k-123"


def test_user_guide_key_defaults(env):
    env.pop("USER_GUIDE_KEY", None)
    assert Config(env).user_guide_key == "docs/user-manual.pdf"
    env["USER_GUIDE_KEY"] = "docs/other.pdf"
    assert Config(env).user_guide_key == "docs/other.pdf"


def test_admin_groups_default_empty(env):
    env.pop("PORTAL_ADMIN_GROUP", None)
    assert Config(env).admin_groups == []
