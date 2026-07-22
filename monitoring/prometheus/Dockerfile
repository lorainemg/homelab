FROM prom/prometheus:latest
COPY prometheus.yml /etc/prometheus/prometheus.yml
# The HA scrape token can't be baked in (this image is public); the
# entrypoint materializes it from the HA_TOKEN env var at startup.
COPY --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh
USER root
RUN mkdir -p /etc/prometheus/secrets && chown -R nobody:nobody /etc/prometheus
USER nobody
ENTRYPOINT ["/docker-entrypoint.sh"]
