# syntax=docker/dockerfile:1

# Standard library only, so the image is the interpreter plus this package. The
# tree is copied rather than `pip install`ed: `pip install .` would need
# setuptools (a network fetch inside the build) to gain nothing over PYTHONPATH.
FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Non-root, fixed uid/gid: the pod needs read access to the hath state file
# (world-readable, or the pod's fsGroup) and nothing else. No shell user, no home
# directory, no write access anywhere.
RUN addgroup -S -g 65532 egress \
 && adduser -S -D -H -u 65532 -G egress -h /app egress

WORKDIR /app
COPY egress_sync /app/egress_sync

USER 65532:65532

# /metrics and /healthz are served on LISTEN_ADDR (default 0.0.0.0:8080).
EXPOSE 8080

ENTRYPOINT ["python", "-m", "egress_sync"]
