#!/bin/sh
# Write the scrape tokens where prometheus.yml expects them, then hand off to
# Prometheus with whatever flags compose passed. /run/prometheus is a tmpfs,
# so the tokens are rewritten from the environment on every start and never
# touch disk.
printf '%s' "${HA_TOKEN:-}" > /run/prometheus/ha_token
printf '%s' "${LIBRECHAT_METRICS_SECRET:-}" > /run/prometheus/librechat_metrics_token
exec /bin/prometheus "$@"
