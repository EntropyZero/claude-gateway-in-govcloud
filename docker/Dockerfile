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

# No package install: this image needs NO access to the Amazon Linux repos.
# - public TLS trust: the base already ships /etc/pki/tls/certs/ca-bundle.crt.
# - openssl: not needed at runtime - the listener cert is generated on the
#   build host and baked in (see the tls/ COPY below).
# - the unprivileged user is created by writing /etc/passwd directly, so no
#   shadow-utils/useradd is required.
# The claude binary is self-contained (bundles its own TLS), so it needs no
# system openssl.
#
# If your enterprise CA also signs internal endpoints the gateway must call,
# trust it in the image (RHEL-family paths - update-ca-trust is in the base):
# COPY enterprise-root-ca.pem /etc/pki/ca-trust/source/anchors/enterprise-root-ca.pem
# RUN update-ca-trust

# The verified native binary from the release bucket (glibc build, linux-x64).
COPY claude /usr/local/bin/claude
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# RDS CA trust bundle (fetched by build-and-push-image.sh) - lets the
# entrypoint use sslmode=verify-full instead of trust-blind 'require'.
COPY rds-ca-bundle.pem /usr/local/share/rds-ca-bundle.pem
# Listener TLS cert, generated on the build host by build-and-push-image.sh.
# --chown (works on the legacy builder) so the gateway user owns the cert and
# reads it even at 0600 - keeps the private key owner-only.
COPY --chown=10001:10001 tls/ /etc/claude/tls/
# The RUN below fixes modes regardless of the build host's umask (a umask-077
# checkout yields mode-600 sources that COPY preserves, breaking reads/exec as
# the unprivileged gateway user). RUN chmod works on the legacy builder;
# --chmod on COPY would require BuildKit.
RUN chmod 0755 /usr/local/bin/claude /usr/local/bin/entrypoint.sh \
 && chmod 0644 /usr/local/share/rds-ca-bundle.pem \
 && printf 'gateway:x:10001:10001::/home/gateway:/sbin/nologin\n' >> /etc/passwd \
 && printf 'gateway:x:10001:\n' >> /etc/group \
 && mkdir -p /home/gateway \
 && chown 10001:10001 /home/gateway /etc/claude

USER gateway
ENV CLAUDE_CONFIG_DIR=/tmp/.claude
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
