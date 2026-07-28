"""Audit logging to the dedicated CMK-encrypted CloudWatch log group.

One JSON line per download / denial / admin action. Audit failure NEVER
aborts a request path (log-and-continue) - but the write is attempted BEFORE
a download stream starts, so a mid-stream client disconnect cannot lose the
record of an authorized download request.
"""

import json
import logging
import os
import socket
import time

log = logging.getLogger("portal")


def build_audit_record(outcome, user_email, user_groups, team, cost_center,
                       version, sha256, source_ip, user_agent, reason=None,
                       event="portal_download", gateway_actor=None,
                       platform=None):
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "outcome": outcome,
        "user_email": user_email,
        "user_groups": user_groups,
        "team": team,
        "cost_center": cost_center,
        # None on non-download events (and on denials where the request never
        # named one); the raw request value on invalid-selection denials.
        "platform": platform,
        "version": version,
        "exe_sha256": sha256,
        "source_ip": source_ip,
        "user_agent": user_agent,
    }
    if reason:
        rec["reason"] = reason
    if gateway_actor:
        # portal_admin events with a connected gateway session: the exact
        # actor string (`oidc:<sub>`) the gateway writes to admin_audit, so
        # the two audit trails join on it.
        rec["gateway_actor"] = gateway_actor
    return rec


class AuditLogger:
    """Writes one JSON line per event to the dedicated CloudWatch log group.
    PutLogEvents no longer requires a sequence token (accepted, ignored).

    The stream name includes the PID: gunicorn runs multiple workers that
    share hostname AND boot second, and two writers on one stream would
    clobber each other. One stream per worker process is the fix."""

    def __init__(self, logs_client, log_group):
        self.logs = logs_client
        self.log_group = log_group
        self.stream = "portal-%s-%d-%d" % (
            socket.gethostname(), os.getpid(), int(time.time()))
        self._ensure_stream()

    def _ensure_stream(self):
        try:
            self.logs.create_log_stream(
                logGroupName=self.log_group, logStreamName=self.stream
            )
        except Exception as exc:  # ResourceAlreadyExists or transient
            log.debug("create_log_stream: %s", exc)

    def write(self, record):
        try:
            self.logs.put_log_events(
                logGroupName=self.log_group,
                logStreamName=self.stream,
                logEvents=[{
                    "timestamp": int(time.time() * 1000),
                    "message": json.dumps(record, separators=(",", ":")),
                }],
            )
        except Exception as exc:  # never let audit failure abort a request path
            log.error("audit write failed: %s", exc)
