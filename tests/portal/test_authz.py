"""Group extraction/authorization + dropdown validation."""

import pytest


def test_groups_from_id_token(app):
    groups = app.groups_from_claims({"groups": ["a", "b"]}, None)
    assert groups == ["a", "b"]


def test_groups_userinfo_fallback(app):
    # ID token carried no groups (Okta org server behaviour); userinfo does.
    groups = app.groups_from_claims({}, {"groups": ["claude-gateway-users"]})
    assert groups == ["claude-gateway-users"]


def test_groups_union_dedup(app):
    groups = app.groups_from_claims({"groups": ["a"]}, {"groups": ["a", "c"]})
    assert groups == ["a", "c"]


def test_groups_scalar_coerced_to_list(app):
    assert app.groups_from_claims({"groups": "solo"}, None) == ["solo"]


def test_authorized_true_when_member(app):
    assert app.is_authorized(["x", "claude-gateway-users"], "claude-gateway-users")


def test_authorized_false_when_not_member(app):
    assert not app.is_authorized(["x", "y"], "claude-gateway-users")
    assert not app.is_authorized([], "claude-gateway-users")
    assert not app.is_authorized(None, "claude-gateway-users")


def test_validate_selection_accepts_configured_values(app, config):
    team, cc = app.validate_selection("platform", "CC-1000", config)
    assert (team, cc) == ("platform", "CC-1000")


def test_validate_selection_rejects_unlisted_team(app, config):
    with pytest.raises(app.SelectionError, match="team"):
        app.validate_selection("marketing", "CC-1000", config)


def test_validate_selection_rejects_unlisted_cost_center(app, config):
    with pytest.raises(app.SelectionError, match="cost_center"):
        app.validate_selection("platform", "CC-9999", config)


def test_validate_selection_rejects_missing(app, config):
    with pytest.raises(app.SelectionError):
        app.validate_selection(None, "CC-1000", config)


def test_validate_selection_rejects_injection_chars(app, config):
    # Even if someone slipped a comma/space value into the configured list, the
    # token cleanliness check (mirrors the installer's ValidatePattern) rejects
    # it. Simulate by pointing config at a dirty list.
    config.teams = ["bad team"]
    with pytest.raises(app.SelectionError, match="spaces or commas"):
        app.validate_selection("bad team", "CC-1000", config)
