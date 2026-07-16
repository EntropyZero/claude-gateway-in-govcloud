"""Pure helpers + the _app_secret_is_initialized error-propagation contract."""

import json

import pytest
from botocore.exceptions import ClientError


def test_other_user_alternates(app):
    assert app._other_user("gateway_app") == "gateway_app_clone"
    assert app._other_user("gateway_app_clone") == "gateway_app"


def test_secret_dict_shape(app):
    d = app._secret_dict("gateway_app", "secret")
    assert d["engine"] == "postgres"
    assert d["username"] == "gateway_app"
    assert d["password"] == "secret"
    assert d["host"] == "db.example.internal"
    assert d["dbname"] == "gateway"


def test_initialized_true_for_real_value(app, aws):
    assert app._app_secret_is_initialized(aws.app_arn) is True


def test_initialized_false_for_placeholder(app, aws):
    aws.sm.put_secret_value(
        SecretId=aws.app_arn, SecretString=json.dumps({"bootstrap": "pending"})
    )
    assert app._app_secret_is_initialized(aws.app_arn) is False


def test_initialized_false_for_missing_secret(app, aws):
    missing = "arn:aws-us-gov:secretsmanager:us-gov-west-1:1:secret:nope-abc"
    assert app._app_secret_is_initialized(missing) is False


def test_initialized_propagates_other_errors(app, monkeypatch):
    """A throttle / AccessDenied must NOT be swallowed as 'uninitialized' —
    that would let bootstrap reset live passwords underneath running tasks."""
    import app as _app

    def _boom(**_):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "GetSecretValue",
        )

    fake = monkeypatch
    fake.setattr(_app.secretsmanager, "get_secret_value", _boom)
    with pytest.raises(ClientError):
        _app._app_secret_is_initialized("arn:...:x")
