# Claude apps gateway container (offline-friendly build)
#
# Build context expects the pinned native Linux binary already downloaded and
# GPG-verified per the runbook (downloads.claude.ai/claude-code-releases/<ver>/):
#   docker build -t claude-gateway:2.1.207 --build-arg CLAUDE_VERSION=2.1.207 .
#
# Amazon Linux 2023 base: glibc (matches the linux-x64 claude binary), and its
# packages come from Amazon's in-region repos - reachable from an AWS build
# host that can't reach deb.debian.org. Mirror the base image into your
# internal registry for controlled networks and pin it by digest there
# (GATEWAY_BASE_IMAGE=<registry>/amazonlinux@sha256:<digest>) so the base
# can't change under you - a floating tag is mutable upstream.
ARG GATEWAY_BASE_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023
FROM ${GATEWAY_BASE_IMAGE}

ARG CLAUDE_VERSION=unknown
LABEL org.opencontainers.image.title="claude-apps-gateway" \
      org.opencontainers.image.version="${CLAUDE_VERSION}"

# openssl: the entrypoint generates a per-task self-signed cert so the
# ALB->task hop is TLS (SC-8) with the private key never leaving the task.
# ca-certificates: public TLS trust (Okta, AWS endpoints). shadow-utils:
# provides useradd (not in the AL2023 minimal base).
RUN dnf install -y --setopt=install_weak_deps=False \
      ca-certificates openssl shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf

# If your enterprise CA also signs internal endpoints the gateway must call
# (e.g. an internal Okta cell or OTLP collector), trust it in the image
# (RHEL-family paths):
# COPY enterprise-root-ca.pem /etc/pki/ca-trust/source/anchors/enterprise-root-ca.pem
# RUN update-ca-trust

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
