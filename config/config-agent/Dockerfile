FROM alpine:3.22

RUN apk add --no-cache git curl

# Config is mounted from Komodo's checkout at runtime, not baked in — only the
# agent itself ships in this image, which is why it almost never rebuilds.
COPY config/config-agent/agent.sh /agent.sh

ENTRYPOINT ["/bin/sh", "/agent.sh"]
