"""DB admin Lambda for the Claude apps gateway (container image).

Two handlers, selected per function via ImageConfig.Command:

  app.bootstrap_handler  - CloudFormation custom resource. Connects as the
      RDS master user ONCE and creates the least-privilege application
      identities (AC-6): a NOLOGIN owner role that owns every gateway
      object, and two LOGIN users (alternating-rotation pair) that assume
      it at login. Writes the initial app secret. Idempotent.

  app.rotate_handler     - Secrets Manager rotation (alternating users).
      Each rotation flips the secret to the OTHER user with a fresh
      password, so the previous credential stays valid until the next
      rotation - no window where a running task holds a dead credential.
      finishSecret also force-rolls the ECS service so new tasks pick up
      the new credential; Secrets Manager retries a failed step, and the
      label move is idempotent, so a failed roll is retried too.

The gateway itself never sees the master credential.
"""

import json
import logging
import os
import ssl
import time
import urllib.request

import boto3
import pg8000.native

logger = logging.getLogger()
logger.setLevel(logging.INFO)

APP_USERS = ("gateway_app", "gateway_app_clone")
OWNER_ROLE = "gateway_owner"
CA_BUNDLE = "/var/task/rds-ca-bundle.pem"

secretsmanager = boto3.client("secretsmanager")
ecs = boto3.client("ecs")


def _db_params():
    return {
        "host": os.environ["PGHOST"],
        "port": int(os.environ["PGPORT"]),
        "dbname": os.environ["PGDATABASE"],
    }


def _connect(username, password):
    p = _db_params()
    ctx = ssl.create_default_context(cafile=CA_BUNDLE)
    return pg8000.native.Connection(
        user=username,
        password=password,
        host=p["host"],
        port=p["port"],
        database=p["dbname"],
        ssl_context=ctx,
        timeout=15,
    )


def _master_connection():
    master = json.loads(
        secretsmanager.get_secret_value(SecretId=os.environ["MASTER_SECRET_ARN"])[
            "SecretString"
        ]
    )
    return _connect(master["username"], master["password"])


def _random_password():
    # Alphanumeric only: ALTER ROLE cannot use bind parameters, so the
    # password is inlined as a SQL literal - keep the alphabet quote-free.
    return secretsmanager.get_random_password(
        PasswordLength=32, ExcludePunctuation=True
    )["RandomPassword"]


def _secret_dict(username, password):
    p = _db_params()
    return {
        "engine": "postgres",
        "host": p["host"],
        "port": p["port"],
        "dbname": p["dbname"],
        "username": username,
        "password": password,
    }


# --------------------------------------------------------------- bootstrap

def _ensure_roles(conn, app_passwords):
    """Create owner + app roles idempotently; grant the minimum.

    Objects must be owned by OWNER_ROLE regardless of which app user
    created them (rotation alternates users), so both users get
    `SET role` at login. The app users can therefore do exactly what the
    owner role can - DDL/DML inside the gateway database - and nothing
    instance-wide (no CREATEROLE, no rds_superuser, no pgaudit tampering).
    """
    dbname = _db_params()["dbname"]
    q_db = '"' + dbname.replace('"', '""') + '"'

    conn.run(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{OWNER_ROLE}') THEN
            CREATE ROLE {OWNER_ROLE} NOLOGIN;
          END IF;
        END $$;
        """
    )
    for user, password in app_passwords.items():
        conn.run(
            f"""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{user}') THEN
                CREATE ROLE {user} LOGIN PASSWORD '{password}' IN ROLE {OWNER_ROLE};
              ELSE
                ALTER ROLE {user} WITH LOGIN PASSWORD '{password}';
                GRANT {OWNER_ROLE} TO {user};
              END IF;
            END $$;
            """
        )
        conn.run(f"ALTER ROLE {user} IN DATABASE {q_db} SET role = '{OWNER_ROLE}'")

    # CREATE on the database too: the gateway's migrations are opaque
    # (closed binary) and may create schemas or trusted extensions.
    conn.run(f"GRANT CONNECT, TEMPORARY, CREATE ON DATABASE {q_db} TO {OWNER_ROLE}")
    conn.run(f"GRANT ALL ON SCHEMA public TO {OWNER_ROLE}")


def _adopt_existing_objects(conn):
    """Hand master-owned objects in schema public to the owner role.

    Existing deployments ran the gateway AS the master user, so its tables
    are master-owned; without this, the app users get 'permission denied'
    on every pre-existing table after the switch. Idempotent (after
    adoption nothing in public is owned by current_user/master).
    """
    conn.run(
        f"""
        DO $$
        DECLARE obj record;
        BEGIN
          FOR obj IN
            SELECT format('ALTER TABLE public.%I OWNER TO {OWNER_ROLE}', tablename) AS cmd
              FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user
            UNION ALL
            SELECT format('ALTER SEQUENCE public.%I OWNER TO {OWNER_ROLE}', sequencename)
              FROM pg_sequences WHERE schemaname = 'public' AND sequenceowner = current_user
            UNION ALL
            SELECT format('ALTER VIEW public.%I OWNER TO {OWNER_ROLE}', viewname)
              FROM pg_views WHERE schemaname = 'public' AND viewowner = current_user
          LOOP
            EXECUTE obj.cmd;
          END LOOP;
        END $$;
        """
    )


def _app_secret_is_initialized(secret_arn):
    # Only a definitive "no usable value" counts as uninitialized. Any
    # other API failure must PROPAGATE - treating a throttle/permission
    # blip as "uninitialized" would reset live passwords underneath
    # running tasks.
    try:
        value = secretsmanager.get_secret_value(SecretId=secret_arn)["SecretString"]
    except secretsmanager.exceptions.ResourceNotFoundException:
        return False
    try:
        return "username" in json.loads(value)
    except ValueError:
        return False


def _cfn_respond(event, context, status, reason=""):
    body = json.dumps(
        {
            "Status": status,
            "Reason": (reason or "see CloudWatch log stream")[:3800]
            + f" (log: {context.log_stream_name})",
            "PhysicalResourceId": event.get(
                "PhysicalResourceId",
                f"{os.environ.get('APP_SECRET_ARN', 'db-app-user')}-bootstrap",
            ),
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
        }
    ).encode()
    req = urllib.request.Request(
        event["ResponseURL"], data=body, method="PUT",
        headers={"Content-Type": ""},
    )
    # A dropped response leaves CloudFormation hanging for ~an hour -
    # retry transient failures before giving up.
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=30)
            return
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
            time.sleep(5)


def bootstrap_handler(event, context):
    logger.info("bootstrap %s", event.get("RequestType"))
    try:
        if event.get("RequestType") == "Delete":
            # Roles stay in the database (the DB usually outlives or dies
            # with the whole deployment); nothing to clean up here.
            _cfn_respond(event, context, "SUCCESS")
            return

        secret_arn = os.environ["APP_SECRET_ARN"]
        initialized = _app_secret_is_initialized(secret_arn)

        if initialized:
            # Live credentials exist - make roles/grants converge but do
            # NOT reset passwords out from under running tasks.
            current = json.loads(
                secretsmanager.get_secret_value(SecretId=secret_arn)["SecretString"]
            )
            conn = _master_connection()
            try:
                # Converge roles/grants. Re-asserting the LIVE user's
                # current password is a no-op; the inactive user's
                # password is only set if the role doesn't exist yet
                # (the next rotation resets it anyway).
                _ensure_roles(conn, {current["username"]: current["password"]})
                other = _other_user(current["username"])
                exists = conn.run(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :u", u=other
                )
                if not exists:
                    _ensure_roles(conn, {other: _random_password()})
                _adopt_existing_objects(conn)
            finally:
                conn.close()
        else:
            passwords = {u: _random_password() for u in APP_USERS}
            conn = _master_connection()
            try:
                _ensure_roles(conn, passwords)
                _adopt_existing_objects(conn)
            finally:
                conn.close()
            secretsmanager.put_secret_value(
                SecretId=secret_arn,
                SecretString=json.dumps(
                    _secret_dict(APP_USERS[0], passwords[APP_USERS[0]])
                ),
            )
        _cfn_respond(event, context, "SUCCESS")
    except Exception as exc:  # noqa: BLE001 - must always answer CFN
        logger.exception("bootstrap failed")
        try:
            _cfn_respond(event, context, "FAILED", reason=str(exc))
        except Exception:  # noqa: BLE001 - nothing left to do but log
            logger.exception("could not deliver FAILED response to CloudFormation")


# ---------------------------------------------------------------- rotation

def _other_user(username):
    return APP_USERS[1] if username == APP_USERS[0] else APP_USERS[0]


def _pending(secret_arn, token):
    return json.loads(
        secretsmanager.get_secret_value(
            SecretId=secret_arn, VersionId=token, VersionStage="AWSPENDING"
        )["SecretString"]
    )


def rotate_handler(event, context):
    secret_arn = event["SecretId"]
    token = event["ClientRequestToken"]
    step = event["Step"]
    logger.info("rotation step %s", step)

    meta = secretsmanager.describe_secret(SecretId=secret_arn)
    stages = meta["VersionIdsToStages"].get(token, [])
    if "AWSCURRENT" in stages and step != "finishSecret":
        logger.info("version already current - nothing to do")
        return

    if step == "createSecret":
        try:
            _pending(secret_arn, token)
            return  # idempotent retry
        except secretsmanager.exceptions.ResourceNotFoundException:
            pass
        current = json.loads(
            secretsmanager.get_secret_value(
                SecretId=secret_arn, VersionStage="AWSCURRENT"
            )["SecretString"]
        )
        secretsmanager.put_secret_value(
            SecretId=secret_arn,
            ClientRequestToken=token,
            SecretString=json.dumps(
                _secret_dict(_other_user(current["username"]), _random_password())
            ),
            VersionStages=["AWSPENDING"],
        )

    elif step == "setSecret":
        pending = _pending(secret_arn, token)
        conn = _master_connection()
        try:
            conn.run(
                f"ALTER ROLE {pending['username']} WITH PASSWORD '{pending['password']}'"
            )
        finally:
            conn.close()

    elif step == "testSecret":
        pending = _pending(secret_arn, token)
        conn = _connect(pending["username"], pending["password"])
        try:
            conn.run("SELECT 1")
        finally:
            conn.close()

    elif step == "finishSecret":
        # Move AWSCURRENT (idempotent), then roll the service so tasks
        # fetch the new credential. Raising on a failed roll makes
        # Secrets Manager retry this step - the label move no-ops then.
        current_version = next(
            (
                vid
                for vid, st in meta["VersionIdsToStages"].items()
                if "AWSCURRENT" in st
            ),
            None,
        )
        if current_version != token:
            kwargs = {
                "SecretId": secret_arn,
                "VersionStage": "AWSCURRENT",
                "MoveToVersionId": token,
            }
            if current_version:  # no AWSCURRENT anywhere: plain attach
                kwargs["RemoveFromVersionId"] = current_version
            secretsmanager.update_secret_version_stage(**kwargs)
        ecs.update_service(
            cluster=os.environ["ECS_CLUSTER"],
            service=os.environ["ECS_SERVICE"],
            forceNewDeployment=True,
        )
        logger.info("rotation finished; service roll requested")

    else:
        raise ValueError(f"unknown rotation step: {step}")
