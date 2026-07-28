# Claude Code installer download portal (ECS Fargate service).
#
# A Flask app (docker/portal/portal/) served by gunicorn that runs the OIDC
# authorization-code flow, checks Okta group membership, and streams a
# per-download ZIP. NOT Lambda: ALB->Lambda responses cap at 1 MB and the
# claude.exe binary is 100+ MB, so this must be a long-running service that
# streams the body.
#
# Mirror the base image into your internal registry for controlled networks
# and pin it by digest there (a floating tag is mutable upstream):
#   docker build -t claude-gw-portal:2.0.0 docker/portal/
ARG PORTAL_BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${PORTAL_BASE_IMAGE}

# flask + gunicorn + boto3 (S3 GetObject for the artifacts + CloudWatch Logs
# for the audit trail), installed from vendored wheels so the build needs NO
# PyPI access - same offline posture as docker/db-admin. vendor/ holds the
# full transitive closure as wheels committed to the repo; the pins live in
# requirements.txt. Refresh with:
#   scripts/mirror/mirror-python-deps.sh portal
COPY requirements.txt /tmp/requirements.txt
COPY vendor/ /tmp/vendor/
RUN pip install --no-cache-dir --no-index --find-links /tmp/vendor -r /tmp/requirements.txt \
 && rm -rf /tmp/vendor /tmp/requirements.txt

# Optional extra trust anchors (enterprise/TLS-inspection root, gateway
# internal-PKI chain), staged by build-and-push-portal.sh as one concatenated
# extra-ca.pem (empty file when unset). The portal's outbound Okta OIDC calls
# (discovery/token/jwks/userinfo) present the inspector's derived cert behind
# inspected egress, so these roots must be in the system trust store Python's
# ssl reads. Root only for the install; back to the unprivileged user
# immediately after.
#
# The bundle is split one-cert-per-file: openssl rehash refuses multi-cert
# files under /usr/local/share/ca-certificates ("skipping ..., it does not
# contain exactly one certificate or crl"), leaving those CAs without hash
# symlinks in /etc/ssl/certs. The same rehash warning for ca-certificates.crt
# itself is unavoidable Debian-trixie noise (it scans the master bundle in
# /etc/ssl/certs) and harmless — update-ca-certificates still exits 0.
COPY extra-ca.pem /tmp/extra-ca.pem
RUN if [ -s /tmp/extra-ca.pem ]; then \
      awk '/-----BEGIN /{n++; p=1} p{print > ("/usr/local/share/ca-certificates/extra-ca-" n ".crt")} /-----END /{p=0}' /tmp/extra-ca.pem \
      && update-ca-certificates; \
    fi \
 && rm -f /tmp/extra-ca.pem

# The application package + entrypoints.
COPY portal/ /opt/portal/portal/
COPY wsgi.py gunicorn.conf.py /opt/portal/
# Listener TLS cert (self-signed leaf generated on the build host by
# build-and-push-portal.sh so the image needs no openssl). The ALB re-encrypts
# to the target and does not validate this cert; it only encrypts the ALB->task
# hop (SC-8). --chown so the unprivileged user owns the mode-600 key.
COPY --chown=10001:10001 tls/ /etc/portal/tls/

# Fix modes regardless of the build host's umask (a umask-077 checkout yields
# mode-600 sources that COPY preserves, breaking reads as the portal user), and
# create the unprivileged runtime user without shadow-utils.
RUN find /opt/portal -type d -exec chmod 0755 {} + \
 && find /opt/portal -type f -exec chmod 0644 {} + \
 && printf 'portal:x:10001:10001::/home/portal:/sbin/nologin\n' >> /etc/passwd \
 && printf 'portal:x:10001:\n' >> /etc/group \
 && mkdir -p /home/portal \
 && chown 10001:10001 /home/portal

USER portal
WORKDIR /opt/portal
EXPOSE 8080
ENTRYPOINT ["gunicorn", "--config", "/opt/portal/gunicorn.conf.py", "wsgi:application"]
