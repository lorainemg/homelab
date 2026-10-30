FROM node:22-bookworm-slim AS node
FROM debian:bookworm-slim
COPY --from=node /usr/local/ /usr/local/
ARG TARGETARCH
ARG T3_VERSION=0.1.5
# Standalone upstream runtime: no Electron, Node installer, or host systemd.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git openssh-client ripgrep libatomic1 libstdc++6 python3 bubblewrap \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) arch=x64; checksum=21d50dcc9c634221b11b58d6a94350d2c6ee36905737b054b11fa4eba295a98b ;; \
      arm64) arch=arm64; checksum=389362e2a349ea62b38d9bc708f4fca2381936cccb2d326b5fc5d074b072aef1 ;; \
      *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    test "$T3_VERSION" = 0.1.5; \
    curl -fSL "https://github.com/pingdotgg/t3code/releases/download/v${T3_VERSION}/t3-${T3_VERSION}-linux-${arch}.tar.gz" -o /tmp/t3.tar.gz; \
    echo "$checksum  /tmp/t3.tar.gz" | sha256sum -c -; \
    mkdir /opt/t3; tar xzf /tmp/t3.tar.gz -C /opt/t3 --strip-components=1; \
    ln -s /opt/t3/t3 /usr/local/bin/t3; rm /tmp/t3.tar.gz
ARG CODEX_VERSION=0.166.0
# Keep aligned with ../catalog/Dockerfile.
RUN npm install -g @openai/codex@${CODEX_VERSION}
COPY configure.py /opt/t3/configure.py
RUN groupadd -g 1000 t3 && useradd -u 1000 -g t3 -m t3 \
    && mkdir /workspace && chown t3:t3 /workspace
ENV HOME=/home/t3 T3CODE_HOME=/home/t3/.t3
USER t3
WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/t3/configure.py"]
CMD ["t3", "serve", "--host", "0.0.0.0", "--port", "3773", "/workspace"]
