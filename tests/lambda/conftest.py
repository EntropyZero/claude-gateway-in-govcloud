"""Fixtures for the db-admin Lambda tests.

Strategy: moto backs Secrets Manager (so the AWSCURRENT/AWSPENDING
version-stage logic the rotation actually manipulates is exercised for
real), while the Postgres connection and the ECS client are faked — we're
testing the rotation/bootstrap control flow, not pg8000 or ECS.
"""

import json
import os
import sys
from unittest import mock

import pytest

# app.py lives in the Lambda image build context.
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "docker", "db-admin")))

# Env the module + handlers read. Set before importing app.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-gov-west-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("PGHOST", "db.example.internal")
os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "gateway")
os.environ.setdefault("ECS_CLUSTER", "claude-gw-cluster")
os.environ.setdefault("ECS_SERVICE", "claude-gw-gateway")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import app as app_module  # noqa: E402


class FakeConn:
    """Records SQL run against it; returns preset rows for SELECTs."""

    def __init__(self, rows=None):
        self.calls = []
        self._rows = rows if rows is not None else []
        self.user = None
        self.password = None
        self.closed = False

    def run(self, sql, **params):
        self.calls.append((sql, params))
        return self._rows

    def close(self):
        self.closed = True

    def ran(self, needle):
        return any(needle in sql for sql, _ in self.calls)


@pytest.fixture
def app():
    return app_module


@pytest.fixture
def fake_pg(app, monkeypatch):
    """Patch _connect so no real Postgres is touched; capture connections.

    _master_connection() calls _connect() internally, so patching _connect
    covers both the master and app-user paths. Role-existence SELECTs return
    empty by default (→ 'role does not exist' → create it)."""
    conns = []

    def _fake_connect(user, password):
        c = FakeConn()
        c.user, c.password = user, password
        conns.append(c)
        return c

    monkeypatch.setattr(app, "_connect", _fake_connect)
    return conns


@pytest.fixture
def aws(app, monkeypatch):
    """moto Secrets Manager + a MagicMock ECS client, wired into app."""
    with mock_aws():
        region = os.environ["AWS_DEFAULT_REGION"]
        smc = boto3.client("secretsmanager", region_name=region)

        smc.create_secret(
            Name="rds-master",
            SecretString=json.dumps({"username": "gw", "password": "masterpw"}),
        )
        master_arn = smc.describe_secret(SecretId="rds-master")["ARN"]

        smc.create_secret(
            Name="app-user",
            SecretString=json.dumps(app._secret_dict("gateway_app", "pw-v1")),
        )
        app_arn = smc.describe_secret(SecretId="app-user")["ARN"]

        ecsc = mock.MagicMock()

        monkeypatch.setattr(app, "secretsmanager", smc)
        monkeypatch.setattr(app, "ecs", ecsc)
        monkeypatch.setenv("MASTER_SECRET_ARN", master_arn)
        monkeypatch.setenv("APP_SECRET_ARN", app_arn)

        yield mock.Mock(sm=smc, ecs=ecsc, master_arn=master_arn, app_arn=app_arn)


def current_username(smc, arn):
    v = smc.get_secret_value(SecretId=arn, VersionStage="AWSCURRENT")["SecretString"]
    return json.loads(v)["username"]


def pending_value(smc, arn, token):
    v = smc.get_secret_value(SecretId=arn, VersionId=token, VersionStage="AWSPENDING")
    return json.loads(v["SecretString"])
