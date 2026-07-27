# Rules — offline build & deploy hosts

- **The build/deploy machine has NO internet access — only AWS service
  endpoints.** It can reach the AWS APIs it deploys to (CloudFormation, ECR,
  S3, Secrets Manager, ECS, …) and nothing else: not grafana.com, not Docker
  Hub, not `downloads.claude.ai`, and not AWS-hosted public *download*
  endpoints either (the RDS truststore, `public.ecr.aws`) — those are CDN
  endpoints, not VPC-endpoint-reachable services. Any script that runs on it
  (`build-and-push-*.sh`, `deploy-*.sh`) must never fetch from the network
  beyond those AWS APIs; a fetch that "usually works" is a bug that surfaces
  only on the real hardened host.

- **All external artifacts flow through the mirror layer, on a separate
  egress host.** `scripts/mirror/*.sh` are the only scripts allowed to
  download; they verify (sha256/GPG) and stage into `mirror/` (gitignored),
  which is then **copied to the build machine**. Build scripts *consume*
  `mirror/` and must **fail closed with instructions** — naming the mirror
  script to run on the egress host — when an artifact is missing; they must
  never invoke a mirror script, which would silently reintroduce the
  network dependency the mirror exists to remove.
  ```bash
  require_mirrored_file "$MIRROR_DIR/rds-ca-bundle.pem" "scripts/mirror/mirror-rds-ca-bundle.sh"  # good — consumes the transferred mirror, fails with instructions
  "${SCRIPT_DIR}/mirror/mirror-grafana-plugin.sh"       # bad — build host can't reach grafana.com; fails only on the real host
  ```

- **Base images are mirrored into a reachable registry, never pulled from
  upstream at build time.** In the target profile every base-image default
  that points at Docker Hub or `public.ecr.aws` must be overridden
  (`GATEWAY_BASE_IMAGE`, `LAMBDA_BASE_IMAGE`, `GRAFANA_BASE_IMAGE`,
  `PORTAL_BASE_IMAGE`) to a digest-pinned copy in the deployment's own ECR —
  `scripts/mirror/mirror-base-images.sh` does that mirroring (all four, and
  persists the vars into deploy.env). Like `mirror-collector.sh` (docker
  pull from `public.ecr.aws` + push to ECR), it runs from a host that
  reaches both the upstream registries and AWS. Upstream defaults exist for
  dev convenience only — document the override next to any new base image.
