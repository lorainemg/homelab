# Extract native metadata from the same pinned Codex used by the T3 image.
# Keep CODEX_VERSION aligned with ../server/Dockerfile.
FROM node:22-bookworm-slim AS metadata
ARG CODEX_VERSION=0.166.0
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@${CODEX_VERSION} \
    && codex debug models --bundled > /bundled-models.json

FROM python:3.13-slim
WORKDIR /opt/catalog
COPY catalog.py ./
COPY --from=metadata /bundled-models.json ./bundled-models.json
USER 1000:1000
ENTRYPOINT ["python3", "catalog.py"]
