# DB admin Lambda (container image): bootstraps the least-privilege
# application DB users and rotates their secret (alternating users),
# rolling the gateway service on each rotation.
#
# Built by scripts/build-and-push-dbadmin.sh, which also stages the RDS CA
# bundle (rds-ca-bundle.pem) into this directory so connections verify the
# server certificate - same posture as the gateway image.
#
# Mirror the base image into your internal registry for controlled
# networks and pin it by digest there (a floating tag is mutable upstream).
ARG LAMBDA_BASE_IMAGE=public.ecr.aws/lambda/python:3.12
FROM ${LAMBDA_BASE_IMAGE}

# pg8000 (pure-Python PostgreSQL driver) + its pure-Python deps, installed
# from vendored wheels so the build needs no PyPI access - vendor/ holds
# universal (py3-none-any) wheels committed to the repo. Refresh with:
#   pip download pg8000==1.31.2 -d docker/db-admin/vendor
COPY vendor/ /tmp/vendor/
RUN pip install --no-cache-dir --no-index --find-links /tmp/vendor pg8000==1.31.2 \
 && rm -rf /tmp/vendor

COPY rds-ca-bundle.pem ${LAMBDA_TASK_ROOT}/rds-ca-bundle.pem
COPY app.py ${LAMBDA_TASK_ROOT}/app.py
# Make the handler + CA bundle world-readable so Lambda's non-root runtime
# user can read them regardless of the build host's umask (a umask-077
# checkout yields mode-600 sources that COPY preserves -> permission denied
# /var/task/app.py). A RUN chmod works on the legacy builder; --chmod on COPY
# would require BuildKit. (The vendored wheels are read by root during the
# pip step, so they need no mode change.)
RUN chmod 0644 ${LAMBDA_TASK_ROOT}/app.py ${LAMBDA_TASK_ROOT}/rds-ca-bundle.pem

# Default handler; CloudFormation overrides per function via
# ImageConfig.Command (app.bootstrap_handler / app.rotate_handler).
CMD ["app.rotate_handler"]
