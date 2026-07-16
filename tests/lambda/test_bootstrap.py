"""Bootstrap custom resource — Delete is a no-op, Create provisions the app
users once, and re-runs must never reset the live password."""

import json

from conftest import current_username


def _capture_cfn(app, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        app, "_cfn_respond",
        lambda event, ctx, status, reason="": seen.update(status=status, reason=reason),
    )
    return seen


def _event(request_type):
    return {
        "RequestType": request_type,
        "StackId": "stack", "RequestId": "req", "LogicalResourceId": "DbBootstrap",
        "ResponseURL": "https://example.invalid/cfn",
    }


def test_delete_is_a_noop_success(app, aws, fake_pg, monkeypatch):
    seen = _capture_cfn(app, monkeypatch)
    app.bootstrap_handler(_event("Delete"), None)
    assert seen["status"] == "SUCCESS"
    assert fake_pg == []  # never touched the database


def test_create_provisions_when_uninitialized(app, aws, fake_pg, monkeypatch):
    aws.sm.put_secret_value(
        SecretId=aws.app_arn, SecretString=json.dumps({"bootstrap": "pending"})
    )
    seen = _capture_cfn(app, monkeypatch)

    app.bootstrap_handler(_event("Create"), None)

    assert seen["status"] == "SUCCESS"
    # wrote a real app-user secret...
    assert current_username(aws.sm, aws.app_arn) == "gateway_app"
    # ...and actually created roles on the DB
    assert any(c.ran("CREATE ROLE") or c.ran("gateway_owner") for c in fake_pg)


def test_create_when_initialized_does_not_reset_live_password(app, aws, fake_pg, monkeypatch):
    before = aws.sm.get_secret_value(SecretId=aws.app_arn)["SecretString"]
    seen = _capture_cfn(app, monkeypatch)

    app.bootstrap_handler(_event("Update"), None)

    assert seen["status"] == "SUCCESS"
    after = aws.sm.get_secret_value(SecretId=aws.app_arn)["SecretString"]
    assert json.loads(after)["password"] == json.loads(before)["password"]


def test_bootstrap_failure_still_answers_cloudformation(app, aws, monkeypatch):
    """If the DB work throws, CFN must still get a FAILED response (else the
    stack hangs ~1h). _master_connection raises here."""
    seen = _capture_cfn(app, monkeypatch)
    aws.sm.put_secret_value(
        SecretId=aws.app_arn, SecretString=json.dumps({"bootstrap": "pending"})
    )

    def _boom(*_, **__):
        raise RuntimeError("no route to db")

    monkeypatch.setattr(app, "_master_connection", _boom)
    app.bootstrap_handler(_event("Create"), None)
    assert seen["status"] == "FAILED"
