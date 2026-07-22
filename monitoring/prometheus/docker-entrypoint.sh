#!/bin/sh
# Write the Home Assistant scrape token where prometheus.yml expects it,
# then hand off to Prometheus with whatever flags compose passed.
printf '%s' "${HA_TOKEN:-}" > /etc/prometheus/secrets/ha_token
exec /bin/prometheus "$@"
