# Rules — CloudFormation / IaC

- **The ALB and the RDS instance must never be replaced by a routine update.**
  A new ALB mints a new DNS name (client DNS resubmission + fingerprint
  re-publish); a new RDS instance is an *empty* database, not a restore. Both
  are protected three ways — deletion protection, fixed physical names, and a
  **stack policy** (set by the deploy scripts) denying `Update:Replace` /
  `Update:Delete`. Do not remove any of those layers.

- **Cross-stack export values are locked while imported.** 01 exports the CMK,
  DB endpoint, master-secret ARN, and client SG to 02; 02 exports SGs, the
  listener, and cluster to 03. You cannot change an exported value in place
  while a downstream stack imports it. **Encryption-at-rest choices and
  resource names are therefore day-one decisions** — changing the RDS storage
  CMK on an existing deployment requires a teardown + data restore, not an
  update. Say so loudly in any change that touches them.

- **Target groups carry no fixed `Name`** (a protocol change replaces them,
  and a fixed name self-collides mid-update). When the target serves TLS, set
  `HealthCheckProtocol: HTTPS` **explicitly** — ALB health checks default to
  HTTP regardless of the target-group protocol, so omitting it means no target
  ever goes healthy.
  ```yaml
  TargetGroup:                    # good
    Properties:
      Protocol: HTTPS
      HealthCheckProtocol: HTTPS  # without this, the probe is plaintext HTTP → never healthy
      # no Name:
  ```

- **Every interface VPC endpoint gets a resource policy** scoped to this
  account/workload — except where GovCloud doesn't support endpoint policies.
  Pre-check before adding one; a `PolicyDocument` on an unsupported endpoint
  fails the stack. The `ecs` endpoint deliberately has none for this reason
  (IAM-side scoping covers it).
  ```bash
  aws ec2 describe-vpc-endpoint-services --region us-gov-west-1 \
    --service-names com.amazonaws.us-gov-west-1.<svc> \
    --query 'ServiceDetails[].VpcEndpointPolicySupported'   # false → omit PolicyDocument
  ```

- **Secrets set out-of-band use a placeholder `SecretString` literal.**
  Changing that literal in the template — or otherwise triggering an update to
  the secret resource — clobbers the live value that a script wrote. Don't
  edit those resources casually; the real value lives only in Secrets Manager.
  ```yaml
  GrafanaOidcClientSecret:
    Properties:
      SecretString: 'REPLACE-ME-run-set-grafana-oidc-secret.sh'  # real value written by the script;
      # editing this line (or the resource's Name/Description) on a later deploy re-applies the
      # placeholder and locks Grafana out. GenerateSecretString is not re-applied on unrelated updates.
  ```

- **Keep `TaskCpu`/`TaskMemory` within the valid Fargate pairings** — the
  `Rules` section asserts them, and an invalid combo otherwise fails deploy
  with an opaque error.

- **AMP KMS and RDS KMS are creation-time only** — enabling them on an existing
  workspace/instance replaces it (and orphans AMP history). Gate such changes
  behind a parameter and document the replacement.
