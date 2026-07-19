"""Structural guards for cloudformation/04-download-portal.yaml.

Text-based (no YAML/intrinsic parser needed) assertions that encode the rules
this template must not regress: the placeholder-SecretString pattern on the
OIDC secret, no fixed target-group Name, CMK on both log groups + the bucket,
S3 posture (BPA + TLS-only + KMS), and the listener-rule priority not colliding
with the Grafana rule. cfn-lint/cfn-guard pick the template up automatically;
these cover semantics those cannot see.
"""

import os
import re

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "..", "cloudformation", "04-download-portal.yaml"
)


def _text():
    with open(TEMPLATE) as f:
        return f.read()


def test_oidc_secret_uses_placeholder_string_pattern():
    t = _text()
    # The OIDC client secret is set out-of-band; it must ship with a placeholder
    # literal (not GenerateSecretString) so a later deploy does not clobber the
    # real value written by set-portal-oidc-secret.sh.
    blk = t[t.index("PortalOidcClientSecret:"):]
    blk = blk[: blk.index("PortalSessionSecret:")]
    assert "SecretString: 'REPLACE-ME" in blk
    assert "GenerateSecretString" not in blk


def test_session_secret_is_generated():
    t = _text()
    blk = t[t.index("PortalSessionSecret:"):]
    blk = blk[: blk.index("ArtifactsBucket:")]
    assert "GenerateSecretString" in blk


def test_both_secrets_are_cmk_encrypted():
    t = _text()
    # Every secret block references the imported CMK.
    for name in ("PortalOidcClientSecret", "PortalSessionSecret"):
        blk = t[t.index(name + ":"):][:600]
        assert "KmsKeyId" in blk and "-kms-key-arn" in blk, name


def test_target_group_has_no_fixed_name_and_https_healthcheck():
    t = _text()
    blk = t[t.index("PortalTargetGroup:"):]
    blk = blk[: blk.index("PortalListenerRule:")]
    assert "Protocol: HTTPS" in blk
    assert "HealthCheckProtocol: HTTPS" in blk
    assert "/portal/healthz" in blk
    # A fixed Name on a target group self-collides on a protocol-change replace.
    assert not re.search(r"^\s+Name:", blk, re.MULTILINE), "target group must not set Name"


def test_log_groups_are_cmk_encrypted():
    t = _text()
    for name in ("AuditLogGroup", "PortalLogGroup"):
        blk = t[t.index(name + ":"):][:500]
        assert "KmsKeyId" in blk and "-kms-key-arn" in blk, name


def test_audit_log_group_is_dedicated_not_activity_stream():
    t = _text()
    # The audit trail is its OWN dedicated group...
    assert "/claude/${NamePrefix}/portal-audit" in t
    # ...and is NOT wired into the sensitive activity-log stream: no
    # subscription filter / Firehose fan-out from this template (rule: never
    # widen the activity-log surface).
    assert "AWS::Logs::SubscriptionFilter" not in t
    assert "Firehose" not in t
    assert "activity-archive" not in t


def test_bucket_blocks_public_access_and_uses_cmk():
    t = _text()
    blk = t[t.index("ArtifactsBucket:"):]
    blk = blk[: blk.index("ArtifactsBucketPolicy:")]
    assert "SSEAlgorithm: aws:kms" in blk
    assert "-kms-key-arn" in blk
    for k in ("BlockPublicAcls: true", "BlockPublicPolicy: true",
              "IgnorePublicAcls: true", "RestrictPublicBuckets: true"):
        assert k in blk, k


def test_bucket_policy_denies_insecure_transport():
    t = _text()
    blk = t[t.index("ArtifactsBucketPolicy:"):][:900]
    assert "Effect: Deny" in blk
    # Deny when the request is NOT over TLS.
    assert "'aws:SecureTransport': 'false'" in blk


def test_task_role_scopes_s3_to_the_bucket_only():
    t = _text()
    blk = t[t.index("PortalTaskRole:"):]
    blk = blk[: blk.index("PortalTaskDefinition:")]
    assert "s3:GetObject" in blk
    # No ListBucket / wildcard-account access - exactly this bucket's objects.
    assert "${ArtifactsBucket.Arn}/*" in blk
    assert "s3:ListBucket" not in blk


def test_listener_rule_priority_default_does_not_collide_with_grafana():
    t = _text()
    blk = t[t.index("ListenerRulePriority:"):][:400]
    m = re.search(r"Default:\s*(\d+)", blk)
    assert m, "ListenerRulePriority needs a default"
    assert m.group(1) != "10", "Grafana already uses priority 10 on the shared listener"


def test_portal_path_pattern_covers_ui_and_callback():
    t = _text()
    assert "'/portal'" in t and "'/portal/*'" in t


def test_service_depends_on_listener_rule():
    t = _text()
    blk = t[t.index("PortalService:"):][:400]
    assert "DependsOn: PortalListenerRule" in blk
