FROM alpine:3.22

RUN apk add --no-cache git && mkdir -p /work && chmod 777 /work

COPY backup.sh /backup.sh

ENTRYPOINT ["/bin/sh", "/backup.sh"]
