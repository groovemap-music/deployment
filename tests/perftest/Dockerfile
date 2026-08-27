# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# GrooveMap API Performance Test Container
#
# Lightweight container that sequentially times all API query endpoints
# and produces a performance report. Not included in docker-compose —
# built and run manually.
#
# See README.md for usage instructions.

FROM python:3.13-slim@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f

LABEL org.opencontainers.image.title="GrooveMap Performance Test" \
      org.opencontainers.image.description="Sequential performance test runner for API query endpoints" \
      com.groovemap.service="perftest"

COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Create non-root user and directories
RUN groupadd -r perftest && \
    useradd -r -l -g perftest -m -s /bin/sh perftest && \
    mkdir -p /app /results && \
    chown -R perftest:perftest /app /results

WORKDIR /app

COPY --chown=perftest:perftest run_perftest.py entrypoint.sh ./
COPY dependency-licenses.json /licenses/dependency-licenses.json
RUN chmod +x entrypoint.sh

USER perftest

VOLUME ["/results", "/config"]

ENTRYPOINT ["/app/entrypoint.sh"]
