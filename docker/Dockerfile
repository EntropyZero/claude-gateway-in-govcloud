# Claude apps gateway container (offline-friendly build)
#
# Build context expects the pinned native Linux binary already downloaded and
# GPG-verified per the runbook (downloads.claude.ai/claude-code-releases/<ver>/):
#   docker build -t claude-gateway:2.1.207 --build-arg CLAUDE_VERSION=2.1.207 .
#
# Mirror the base image into your internal registry for controlled networks,
# and pin it by digest there (FROM <registry>/debian@sha256:<digest>) so the
# base can't change under you - a floating tag is mutable upstream.
FROM debian:bookworm-slim

ARG CLAUDE_VERSION=unknown
LABEL org.opencontainers.image.title="claude-apps-gateway" \
      org.opencontainers.image.version="${CLAUDE_VERSION}"

# openssl: the entrypoint generates a per-task self-signed cert so the
# ALB->task hop is TLS (SC-8) with the private key never leaving the task.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates openssl \
 && rm -rf /var/lib/apt/lists/*

# If your enterprise CA also signs internal endpoints the gateway must call
# (e.g. an internal Okta cell or OTLP collector), trust it in the image:
# COPY enterprise-root-ca.pem /usr/local/share/ca-certificates/enterprise-root-ca.crt
# RUN update-ca-certificates

# The verified native binary from the release bucket (glibc build, linux-x64).
COPY claude /usr/local/bin/claude
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# RDS CA trust bundle (fetched by build-and-push-image.sh) - lets the
# entrypoint use sslmode=verify-full instead of trust-blind 'require'.
COPY rds-ca-bundle.pem /usr/local/share/rds-ca-bundle.pem
RUN chmod 0755 /usr/local/bin/claude /usr/local/bin/entrypoint.sh \
 && useradd --system --create-home --uid 10001 gateway \
 && mkdir -p /etc/claude && chown gateway:gateway /etc/claude

USER gateway
ENV CLAUDE_CONFIG_DIR=/tmp/.claude
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
