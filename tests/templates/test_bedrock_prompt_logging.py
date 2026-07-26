"""Structural guard for the Bedrock prompt-logging destinations (03) and the
CMK delivery grant (01). Pins the shapes the Bedrock invocation-logging docs
prescribe - drop a condition key or the fixed log-stream name and delivery
either silently fails or the grant becomes account-wide-writable."""

import os

import yaml

HERE = os.path.dirname(__file__)


def _load(template):
    """Parse a CFN template, mapping short-form intrinsics to plain values."""
    class Loader(yaml.SafeLoader):
        pass

    def _tag(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    yaml.add_multi_constructor("!", _tag, Loader=Loader)
    path = os.path.join(HERE, "..", "..", "cloudformation", template)
    return yaml.load(open(path), Loader=Loader)


def test_prompt_log_group_is_cmk_encrypted_unconditional_and_retained():
    doc = _load("03-observability.yaml")
    lg = doc["Resources"]["BedrockPromptLogGroup"]
    # UNCONDITIONAL on purpose: a conditional fixed-name Retain group collides
    # on re-enable, and the enable switch is account-level (script-applied),
    # not a stack condition. See the template comment.
    assert "Condition" not in lg
    assert lg["DeletionPolicy"] == "Retain"
    assert "KmsKeyId" in lg["Properties"]
    assert lg["Properties"]["RetentionInDays"] == "BedrockPromptLogWindowDays"


def test_prompt_bucket_policy_has_the_docs_prescribed_conditions():
    doc = _load("03-observability.yaml")
    pol = doc["Resources"]["BedrockPromptLogsBucketPolicy"]
    assert "Condition" not in pol
    stmts = pol["Properties"]["PolicyDocument"]["Statement"]
    write = next(s for s in stmts if s["Sid"] == "AmazonBedrockLogsWrite")
    assert write["Principal"] == {"Service": "bedrock.amazonaws.com"}
    assert write["Action"] == "s3:PutObject"
    # Without BOTH conditions, any account's Bedrock delivery could write here
    # (confused-deputy). These keys come verbatim from the Bedrock docs.
    assert "aws:SourceAccount" in write["Condition"]["StringEquals"]
    assert "aws:SourceArn" in write["Condition"]["ArnLike"]
    deny = next(s for s in stmts if s["Sid"] == "DenyInsecureTransport")
    assert deny["Effect"] == "Deny"


def test_delivery_role_trust_is_condition_scoped_and_stream_is_fixed():
    doc = _load("03-observability.yaml")
    role = doc["Resources"]["BedrockPromptLoggingRole"]
    assert "Condition" not in role
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "bedrock.amazonaws.com"}
    assert "aws:SourceAccount" in trust["Condition"]["StringEquals"]
    assert "aws:SourceArn" in trust["Condition"]["ArnLike"]
    stmt = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0]
    assert set(stmt["Action"]) == {"logs:CreateLogStream", "logs:PutLogEvents"}
    # Bedrock writes ONLY this stream name; a broader resource is excess, a
    # different one breaks delivery. Pin the exact log-group segment too so a
    # wildcard can't creep in.
    assert stmt["Resource"].endswith(
        "log-group:${BedrockPromptLogGroup}:log-stream:aws/bedrock/modelinvocations")


def test_prompt_bucket_expires_and_blocks_public_access():
    doc = _load("03-observability.yaml")
    b = doc["Resources"]["BedrockPromptLogsBucket"]["Properties"]
    rule = b["LifecycleConfiguration"]["Rules"][0]
    assert rule["ExpirationInDays"] == "BedrockPromptArchiveRetentionDays"
    assert all(b["PublicAccessBlockConfiguration"].values())
    assert b["OwnershipControls"]["Rules"][0]["ObjectOwnership"] == "BucketOwnerEnforced"


def test_prompt_bucket_has_no_bucket_key():
    """An S3 Bucket Key requires the delivering service principal to also
    hold kms:Decrypt (CloudTrail-delivery precedent); 01's Bedrock grant is
    the docs-prescribed kms:GenerateDataKey ONLY, so a bucket key would
    silently break delivery of the >100KB bodies only this bucket holds."""
    doc = _load("03-observability.yaml")
    b = doc["Resources"]["BedrockPromptLogsBucket"]["Properties"]
    enc = b["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert "BucketKeyEnabled" not in enc


def test_cmk_policy_grants_bedrock_delivery_scoped_to_this_account():
    doc = _load("01-database.yaml")
    stmts = doc["Resources"]["KmsKey"]["Properties"]["KeyPolicy"]["Statement"]
    grant = next(s for s in stmts if s.get("Sid") == "BedrockInvocationLogsWrite")
    assert grant["Principal"] == {"Service": "bedrock.amazonaws.com"}
    assert grant["Action"] == "kms:GenerateDataKey"
    assert "aws:SourceAccount" in grant["Condition"]["StringEquals"]
    assert "aws:SourceArn" in grant["Condition"]["ArnLike"]
