FROM alpine:3.22

COPY ha-config /src/ha-config
COPY mosquitto /src/mosquitto
COPY config-sync/sync.sh /sync.sh

ENTRYPOINT ["/bin/sh", "/sync.sh"]
