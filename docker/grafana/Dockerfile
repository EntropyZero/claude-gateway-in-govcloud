# Grafana for the Claude apps gateway usage dashboard.
#
# Bakes the AMP datasource and the Claude Code usage dashboard so the
# container is fully provisioned at boot (no clicking, no persistent volume
# needed for the provisioned content). Mirror the base image into your
# internal registry for controlled networks, and pin by digest there
# (GRAFANA_BASE_IMAGE=<registry>/grafana-oss@sha256:<digest> - a floating
# tag is mutable upstream):
#   docker pull grafana/grafana-oss:11.5.1
#   docker build -t claude-gw-grafana:11.5.1 docker/grafana/
ARG GRAFANA_BASE_IMAGE=grafana/grafana-oss:11.5.1
FROM ${GRAFANA_BASE_IMAGE}

# Datasource + dashboard provisioning. The datasource reads AMP_QUERY_URL /
# AMP_REGION from the environment (set by the ECS task definition).
# --chown to the grafana user (472) so it owns these and can read/traverse
# them regardless of the build host's umask (a umask-077 checkout yields
# mode-600 files / 700 dirs, both of which the OWNER can still read/traverse).
# --chown works on the legacy builder; --chmod would require BuildKit.
COPY --chown=472:472 provisioning/ /etc/grafana/provisioning/
COPY --chown=472:472 dashboards/ /var/lib/grafana/dashboards/

# Serve HTTPS on the ALB->Grafana hop (SC-8). The self-signed leaf in tls/ is
# generated on the BUILD HOST by build-and-push-grafana.sh - so this Alpine
# image needs no openssl and the build has no external package dependency
# (works on an AWS-only build host). The ALB re-encrypts and does not
# validate the target cert. Regenerated each build; not committed.
COPY --chown=472:472 tls/ /etc/grafana/tls/
ENV GF_SERVER_PROTOCOL=https \
    GF_SERVER_CERT_FILE=/etc/grafana/tls/server.crt \
    GF_SERVER_CERT_KEY=/etc/grafana/tls/server.key

# Optional enterprise/TLS-inspection root CA (e.g. Zscaler), staged by
# build-and-push-grafana.sh from EXTRA_CA_CERT_PATH (empty file when unset).
# Grafana's only external call is the Okta OAuth exchange; behind an
# inspecting proxy that hop presents the inspector's derived cert, so the
# inspector's root must be in the system bundle Go reads. Root only for the
# append; back to the grafana user immediately after.
COPY extra-ca.pem /tmp/extra-ca.pem
USER root
RUN if [ -s /tmp/extra-ca.pem ]; then \
      cat /tmp/extra-ca.pem >> /etc/ssl/certs/ca-certificates.crt; \
    fi \
 && rm -f /tmp/extra-ca.pem
USER grafana
