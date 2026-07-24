FROM alpine:3.22

RUN apk add --no-cache git curl

# Build context is the repo root: this image carries every config the agent
# syncs (HA yaml, mosquitto.conf, Caddyfile).
COPY home-assistant/ha-config /src/ha-config
COPY home-assistant/mosquitto /src/mosquitto
COPY config/Caddyfile /src/caddy/Caddyfile
COPY config/config-agent/agent.sh /agent.sh

ENTRYPOINT ["/bin/sh", "/agent.sh"]
