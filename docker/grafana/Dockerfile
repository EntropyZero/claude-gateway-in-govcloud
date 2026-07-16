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
COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/ /var/lib/grafana/dashboards/

# TLS entrypoint: generates a per-task self-signed cert and serves HTTPS
# (the ALB re-encrypts to the target). openssl via apk - grafana-oss
# images are Alpine-based; adjust if your mirrored base differs.
USER root
RUN apk add --no-cache openssl
COPY entrypoint-tls.sh /entrypoint-tls.sh
RUN chmod 0755 /entrypoint-tls.sh
USER 472
ENTRYPOINT ["/entrypoint-tls.sh"]
