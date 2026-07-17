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
# Enterprise/TLS-inspection root CA (e.g. Zscaler): set EXTRA_CA_CERT_PATH in
# deploy.env and build-and-push-image.sh stages it as extra-ca.pem (empty file
# when unset). Without it, egress through an inspecting proxy fails TLS on
# the Okta hops (the inspector's derived cert isn't in any default store).

# The verified native binary from the release bucket (glibc build, linux-x64).
COPY claude /usr/local/bin/claude
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# RDS CA trust bundle (fetched by build-and-push-image.sh) - lets the
# entrypoint use sslmode=verify-full instead of trust-blind 'require'.
COPY rds-ca-bundle.pem /usr/local/share/rds-ca-bundle.pem
# Optional extra root CA (may be an empty file - see above).
COPY extra-ca.pem /usr/local/share/extra-ca.pem
# Listener TLS cert, generated on the build host by build-and-push-image.sh.
# --chown (works on the legacy builder) so the gateway user owns the cert and
# reads it even at 0600 - keeps the private key owner-only.
COPY --chown=10001:10001 tls/ /etc/claude/tls/
# The RUN below fixes modes regardless of the build host's umask (a umask-077
# checkout yields mode-600 sources that COPY preserves, breaking reads/exec as
# the unprivileged gateway user). RUN chmod works on the legacy builder;
# --chmod on COPY would require BuildKit.
# It also installs the RDS CA bundle into the OS trust store: the gateway's
# Postgres client does NOT honor the libpq sslrootcert= URL param - it
# verifies against the runtime's default trust (the native binary reads the
# OS store), and Amazon's RDS CAs are private, so without this every DB
# connection fails with "self signed certificate in certificate chain".
RUN chmod 0755 /usr/local/bin/claude /usr/local/bin/entrypoint.sh \
 && chmod 0644 /usr/local/share/rds-ca-bundle.pem /usr/local/share/extra-ca.pem \
 && cp /usr/local/share/rds-ca-bundle.pem /etc/pki/ca-trust/source/anchors/rds-ca-bundle.pem \
 && if [ -s /usr/local/share/extra-ca.pem ]; then \
      cp /usr/local/share/extra-ca.pem /etc/pki/ca-trust/source/anchors/extra-ca.pem; \
    fi \
 && update-ca-trust \
 && cat /usr/local/share/rds-ca-bundle.pem /usr/local/share/extra-ca.pem \
      > /usr/local/share/node-extra-cas.pem \
 && chmod 0644 /usr/local/share/node-extra-cas.pem \
 && printf 'gateway:x:10001:10001::/home/gateway:/sbin/nologin\n' >> /etc/passwd \
 && printf 'gateway:x:10001:\n' >> /etc/group \
 && mkdir -p /home/gateway \
 && chown 10001:10001 /home/gateway /etc/claude

USER gateway
ENV CLAUDE_CONFIG_DIR=/tmp/.claude
# Belt-and-suspenders with the OS-store install above: NODE_EXTRA_CA_CERTS is
# the documented CA-extension mechanism for the claude runtime (it EXTENDS the
# default roots, so unrelated TLS is unaffected). Points at the combined
# RDS + optional-extra bundle built in the RUN above.
ENV NODE_EXTRA_CA_CERTS=/usr/local/share/node-extra-cas.pem
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
