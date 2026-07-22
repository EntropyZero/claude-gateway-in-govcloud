# Claude Code installer download portal (ECS Fargate service).
#
# A small stdlib+boto3 HTTP service (docker/portal/app.py) that runs the OIDC
# authorization-code flow, checks Okta group membership, and streams a
# per-download ZIP. NOT Lambda: ALB->Lambda responses cap at 1 MB and the
# claude.exe binary is 100+ MB, so this must be a long-running service that
# streams the body.
#
# Mirror the base image into your internal registry for controlled networks
# and pin it by digest there (a floating tag is mutable upstream):
#   docker build -t claude-gw-portal:1.0.0 docker/portal/
ARG PORTAL_BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${PORTAL_BASE_IMAGE}

# boto3 (S3 GetObject for the artifacts + CloudWatch Logs for the audit trail)
# is the only non-stdlib dependency, installed from vendored wheels so the
# build needs NO PyPI access - same offline posture as docker/db-admin. vendor/
# holds boto3 + its transitive deps as universal (py3-none-any) wheels committed
# to the repo. Refresh with:
#   pip download boto3==1.34.162 -d docker/portal/vendor
COPY vendor/ /tmp/vendor/
RUN pip install --no-cache-dir --no-index --find-links /tmp/vendor boto3==1.34.162 \
 && rm -rf /tmp/vendor

# Optional enterprise/TLS-inspection root CA (e.g. Zscaler), staged by
# build-and-push-portal.sh from EXTRA_CA_CERT_PATH (empty file when unset).
# The portal's outbound Okta OIDC calls (discovery/token/jwks/userinfo) present
# the inspector's derived cert behind inspected egress, so its root must be in
# the system trust store Python's ssl reads. Root only for the install; back to
# the unprivileged user immediately after.
COPY extra-ca.pem /tmp/extra-ca.pem
RUN if [ -s /tmp/extra-ca.pem ]; then \
      cp /tmp/extra-ca.pem /usr/local/share/ca-certificates/extra-ca.crt \
      && update-ca-certificates; \
    fi \
 && rm -f /tmp/extra-ca.pem

COPY app.py /opt/portal/app.py
# Listener TLS cert (self-signed leaf generated on the build host by
# build-and-push-portal.sh so the image needs no openssl). The ALB re-encrypts
# to the target and does not validate this cert; it only encrypts the ALB->task
# hop (SC-8). --chown so the unprivileged user owns the mode-600 key.
COPY --chown=10001:10001 tls/ /etc/portal/tls/

# Fix modes regardless of the build host's umask (a umask-077 checkout yields
# mode-600 sources that COPY preserves, breaking reads as the portal user), and
# create the unprivileged runtime user without shadow-utils.
RUN chmod 0644 /opt/portal/app.py \
 && printf 'portal:x:10001:10001::/home/portal:/sbin/nologin\n' >> /etc/passwd \
 && printf 'portal:x:10001:\n' >> /etc/group \
 && mkdir -p /home/portal \
 && chown 10001:10001 /home/portal

USER portal
EXPOSE 8080
ENTRYPOINT ["python3", "/opt/portal/app.py"]
