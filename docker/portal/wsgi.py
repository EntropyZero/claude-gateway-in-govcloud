"""gunicorn entrypoint: `gunicorn wsgi:application`.

Config errors (missing env, malformed PORTAL_COST_CENTER_TEAMS) raise here at
worker boot, so a misconfigured task exits loudly instead of serving a broken
page. Each worker builds its own app (no preload): boto3 clients are not
fork-shared, and the audit stream name embeds the worker PID.
"""

import logging
import os
import sys

logging.basicConfig(level=os.environ.get("PORTAL_LOG_LEVEL", "INFO"),
                    stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(message)s")

from portal import create_app  # noqa: E402

application = create_app()
