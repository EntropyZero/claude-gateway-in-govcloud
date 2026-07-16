"""Rotation state machine — the alternating-users flip, idempotency guards,
and the RemoveFromVersionId-when-no-AWSCURRENT edge case."""

import uuid
from unittest import mock

from conftest import current_username, pending_value


def _event(arn, token, step):
    return {"SecretId": arn, "ClientRequestToken": token, "Step": step}


def _run_all_steps(app, arn, token):
    for step in ("createSecret", "setSecret", "testSecret", "finishSecret"):
        app.rotate_handler(_event(arn, token, step), None)


def test_full_rotation_flips_to_the_other_user(app, aws, fake_pg):
    token = str(uuid.uuid4())
    assert current_username(aws.sm, aws.app_arn) == "gateway_app"

    # createSecret stages the OTHER user with a fresh password
    app.rotate_handler(_event(aws.app_arn, token, "createSecret"), None)
    pending = pending_value(aws.sm, aws.app_arn, token)
    assert pending["username"] == "gateway_app_clone"
    assert pending["password"] != "pw-v1"

    # setSecret ALTERs that role's password on the DB, as master
    app.rotate_handler(_event(aws.app_arn, token, "setSecret"), None)
    assert any(c.ran("ALTER ROLE gateway_app_clone") for c in fake_pg)

    # testSecret opens a connection AS the new user
    app.rotate_handler(_event(aws.app_arn, token, "testSecret"), None)
    assert any(c.user == "gateway_app_clone" and c.ran("SELECT 1") for c in fake_pg)

    # finishSecret promotes AWSCURRENT and rolls the service
    app.rotate_handler(_event(aws.app_arn, token, "finishSecret"), None)
    assert current_username(aws.sm, aws.app_arn) == "gateway_app_clone"
    aws.ecs.update_service.assert_called_once_with(
        cluster="claude-gw-cluster", service="claude-gw-gateway",
        forceNewDeployment=True,
    )


def test_second_rotation_flips_back(app, aws, fake_pg):
    _run_all_steps(app, aws.app_arn, str(uuid.uuid4()))
    assert current_username(aws.sm, aws.app_arn) == "gateway_app_clone"
    _run_all_steps(app, aws.app_arn, str(uuid.uuid4()))
    assert current_username(aws.sm, aws.app_arn) == "gateway_app"  # alternated back


def test_createsecret_is_idempotent_on_retry(app, aws, fake_pg):
    token = str(uuid.uuid4())
    app.rotate_handler(_event(aws.app_arn, token, "createSecret"), None)
    first = pending_value(aws.sm, aws.app_arn, token)
    # a retry of the same token must NOT re-stage a new password
    app.rotate_handler(_event(aws.app_arn, token, "createSecret"), None)
    assert pending_value(aws.sm, aws.app_arn, token) == first


def test_finishsecret_retry_still_rolls_and_stays_current(app, aws, fake_pg):
    token = str(uuid.uuid4())
    _run_all_steps(app, aws.app_arn, token)
    aws.ecs.reset_mock()
    # Secrets Manager may retry finishSecret; it must no-op the label move
    # (already current) but still request a roll, and never error.
    app.rotate_handler(_event(aws.app_arn, token, "finishSecret"), None)
    assert current_username(aws.sm, aws.app_arn) == "gateway_app_clone"
    aws.ecs.update_service.assert_called_once()


def test_finishsecret_omits_remove_when_no_awscurrent(app, monkeypatch, fake_pg):
    """If no version holds AWSCURRENT, update_secret_version_stage must be
    called WITHOUT RemoveFromVersionId (passing None would fail validation)."""
    token = "pending-token"
    fake_sm = mock.MagicMock()
    fake_sm.describe_secret.return_value = {
        "VersionIdsToStages": {token: ["AWSPENDING"]}  # no AWSCURRENT anywhere
    }
    monkeypatch.setattr(app, "secretsmanager", fake_sm)
    monkeypatch.setattr(app, "ecs", mock.MagicMock())
    monkeypatch.setenv("APP_SECRET_ARN", "arn:aws-us-gov:secretsmanager:us-gov-west-1:1:secret:x")

    app.rotate_handler(
        {"SecretId": "arn:...:x", "ClientRequestToken": token, "Step": "finishSecret"},
        None,
    )
    _, kwargs = fake_sm.update_secret_version_stage.call_args
    assert kwargs["MoveToVersionId"] == token
    assert "RemoveFromVersionId" not in kwargs


def test_already_current_token_short_circuits_non_finish_steps(app, aws, fake_pg):
    """A token already staged AWSCURRENT must skip create/set/test (a
    duplicate invocation), so no spurious DB writes happen."""
    current_id = aws.sm.describe_secret(SecretId=aws.app_arn)["VersionIdsToStages"]
    token = next(v for v, st in current_id.items() if "AWSCURRENT" in st)
    app.rotate_handler(_event(aws.app_arn, token, "setSecret"), None)
    assert not any(c.ran("ALTER ROLE") for c in fake_pg)
