# Grafana for the Claude apps gateway usage dashboard.
#
# Bakes the AMP datasource and the Claude Code usage dashboard so the
# container is fully provisioned at boot (no clicking, no persistent volume
# needed for the provisioned content). Mirror the base image into your
# internal registry for controlled networks:
#   docker pull grafana/grafana-oss:11.5.1
#   docker build -t claude-gw-grafana:11.5.1 docker/grafana/
ARG GRAFANA_BASE_IMAGE=grafana/grafana-oss:11.5.1
FROM ${GRAFANA_BASE_IMAGE}

# Datasource + dashboard provisioning. The datasource reads AMP_QUERY_URL /
# AMP_REGION from the environment (set by the ECS task definition).
COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/ /var/lib/grafana/dashboards/
