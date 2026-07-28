"""gunicorn config for the download portal.

TLS terminates on the task (self-signed leaf baked into the image; the ALB
re-encrypts and does not validate it) - same SC-8 posture as the gateway and
Grafana tasks. HTTP/1.1 + streamed (no Content-Length) responses => chunked
transfer-encoding, preserving the ZIP truncation-detectability property.
forwarded-allow-ips is deliberately left at its default: the app reads only
the LAST X-Forwarded-For entry (the one the ALB appended) for audit.
"""

import os

bind = "0.0.0.0:" + os.environ.get("PORTAL_PORT", "8080")
worker_class = "gthread"
workers = 2
threads = 8
# The certfile/keyfile defaults match portal/config.py (PORTAL_TLS_CERT/KEY).
certfile = os.environ.get("PORTAL_TLS_CERT", "/etc/portal/tls/server.crt")
keyfile = os.environ.get("PORTAL_TLS_KEY", "/etc/portal/tls/server.key")
# Long installer downloads over slow links: the gthread main loop keeps the
# heartbeat alive while threads stream, but leave headroom anyway.
timeout = 120
graceful_timeout = 30
# Logs to stdout/stderr for the awslogs driver.
accesslog = "-"
errorlog = "-"
