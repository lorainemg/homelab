# Portainer → Komodo Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `config`, `monitoring`, `immich` and `home-assistant` from Portainer to Komodo, delete the four config-baked images, relocate the Cloudflare tunnel to its own host-managed stack, and retire Portainer.

**Architecture:** Each stack becomes a Komodo Stack resource pinned to its existing compose project name, deploying from this repo on a GitHub webhook. Config that CI used to bake into thin images is bind-mounted out of Komodo's on-host checkout instead, so four Dockerfiles disappear and the build/deploy race disappears with them. `deploy.yml` shrinks to one job that builds the single remaining image (`config-agent`) and then triggers Komodo. `cloudflared` moves out of `portainer/` into a host-managed `tunnel/` stack so no deploy can sever the way in.

**Tech Stack:** Docker Compose, Komodo v2.3.1 (`ghcr.io/moghtech/komodo-core`, `komodo-periphery`, `komodo-cli`), MongoDB 8, Caddy 2.8.4, Cloudflare Tunnel, GitHub Actions, GitHub webhooks.

**Spec:** [docs/superpowers/specs/2026-08-20-portainer-to-komodo-migration-design.md](../specs/2026-08-20-portainer-to-komodo-migration-design.md)

## Global Constraints

- **Branch:** all repo work happens on `komodo-migration`, branched from `main`. The spec and this plan are already committed there.
- **Pushing to `main` deploys.** Until Task 8, `main` still drives Portainer deploys for whatever stacks have not moved. Merge deliberately, never "to save work".
- **Compose project names are pinned and must not change:** `config`, `monitoring`, `immich`, `home-assistant`. The live named volumes carry these as prefixes (`monitoring_grafana_data`, `monitoring_loki_data`, `monitoring_prometheus_data`, `monitoring_tempo_data`, `config_caddy_conf`, `config_caddy_data`, `config_caddy_config`, `immich_model-cache`). A different project name creates empty new volumes and the service starts perfectly healthy with no data.
- **Mount directories, never single files.** A bind mount pins an inode; git replaces files rather than editing them, so a single-file mount from a checkout is frozen at the version present when the container started.
- **Secrets never in git.** Every `.env` is gitignored by the existing `*/.env` rule; only `.env.example` files are committed. Enable the gitleaks hook once per clone: `cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`.
- **`komodo/` and `tunnel/` are host-managed** and must not appear in any paths-filter or deploy matrix in `.github/workflows/deploy.yml`, nor as Komodo Stack resources.
- **Acceptance is a data assertion, never a health check.** A stack backed by an empty volume starts healthy and serves nothing.
- **Commit messages:** single line, casual, no trailers — matching the existing log (`add the komodo stack`, `route komodo.sussman.win to komodo`).
- **Where commands run:** steps marked *(host)* run on the homelab server, reachable as `ssh home` (`/home/lorainemg/homelab` is the server-side clone). Steps marked *(repo)* run in the local clone. Steps marked *(API)* run anywhere and talk to `https://komodo.sussman.win`.

### Komodo API preamble

Every *(API)* step assumes `$JWT` is set. Obtain it once per session:

```bash
JWT=$(curl -s -X POST https://komodo.sussman.win/auth/login \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"type":"LoginLocalUser","params":{"username":"%s","password":"%s"}}' \
        "$(grep '^KOMODO_INIT_ADMIN_USERNAME=' komodo/.env | cut -d= -f2-)" \
        "$(grep '^KOMODO_INIT_ADMIN_PASSWORD=' komodo/.env | cut -d= -f2-)")" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["jwt"])')
```

`POST /read`, `/write` and `/execute` all take `{"type":..., "params":{...}}` with `Authorization: Bearer $JWT`. `/execute` is asynchronous — it returns an Update with `status: "InProgress"`, so confirm results by polling `/read`.

---

### Task 1: Prove a rollback from Komodo's UI

The last unproven verdict criterion, and the procedure you would reach for at the worst possible moment. Done first, on the canary, while nothing that matters is on Komodo.

**Files:** none — this task changes no files.

**Interfaces:**
- Consumes: the running `docker-registry` Stack.
- Produces: a timed, written-down rollback procedure that Tasks 3–6 rely on as their escape hatch.

- [x] **Step 1: Record the currently deployed commit** *(API)*

```bash
curl -s -X POST https://komodo.sussman.win/read -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"type":"ListStacks","params":{}}' \
  | python3 -c 'import sys,json;[print(s["name"], s["info"]["deployed_hash"], s["info"]["latest_hash"]) for s in json.load(sys.stdin)]'
```

Write down **both** hashes. `deployed_hash` is where the Stack is now;
`latest_hash` is `main`'s current tip. They are allowed to differ — the webhook
is file-scoped (`webhook_force_deploy: false`), so commits that do not touch
`registry/` never trigger a redeploy. Step 5 rolls *forward to `latest_hash`*,
which is not necessarily the `deployed_hash` you just recorded.

- [x] **Step 2: Pin the Stack to an older commit and redeploy from the UI**

In Komodo's UI, open the `docker-registry` Stack → Config, leave **branch** as
`main`, and set the **commit** field to an older commit hash that touched
`registry/`. Save, **then** hit Deploy — these are two separate actions, and
saving alone deploys nothing: it rewrites the config and refreshes the git
cache, so `latest_hash` moves to the target while `deployed_hash` stays where
it was. That gap between the two fields is the only sign the Deploy click is
still owed. Start a timer when you click Deploy.

**Use the `commit` field, not the `branch` field.** Komodo interpolates `branch`
straight into `git clone <url> <path> -b <branch>`, and git's `-b` accepts only
branch names and tags, never a bare commit — so a hash there fails the clone
outright (`fatal: Remote branch <hash> not found in upstream origin`). On the
pull path, taken whenever the on-host checkout already exists, it fails *later*
and more quietly: `git checkout -f <hash>` succeeds, then
`git pull --rebase --force origin <hash>` dies with `couldn't find remote ref`,
so the UI shows the commit you asked for and nothing is deployed. The `commit`
field is interpolated into `git reset --hard <commit>` after a successful clone
or pull, which is the mechanism that actually works. Verified against
`lib/git/src/clone.rs` and `lib/git/src/pull.rs` at komodo `v2.3.1`, and
against a real repo — an earlier draft of this plan asserted the opposite.

The criterion from the evaluation spec is *under a minute, with no git revert and no CI run*.

- [x] **Step 3: Confirm the older compose actually deployed** *(API)*

```bash
curl -s -X POST https://komodo.sussman.win/read -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"type":"ListStacks","params":{}}' \
  | python3 -c 'import sys,json;[print(s["name"], s["info"]["deployed_hash"]) for s in json.load(sys.stdin)]'
```

Expected: `deployed_hash` is the older commit, not the one from Step 1.

- [x] **Step 4: Confirm the registry still has its data**

```bash
curl -s https://registry.sussman.win/v2/_catalog
```

Expected: a non-empty `repositories` list. A rollback that empties the volume is a failed rollback, not a successful one.

- [x] **Step 5: Roll forward again**

Clear the **commit** field in the UI, leaving **branch** as `main`, Deploy, and
confirm via the command in Step 3 that `deployed_hash` is now `main`'s tip —
the `latest_hash` recorded in Step 1, which may be *newer* than the
`deployed_hash` recorded there.

**Do not treat this as tidy-up.** A pin left in the `commit` field is invisible
at a glance: the Stack still reports `branch: main`, webhooks still fire, and
every deploy still reports success — while the stack stays frozen on the pinned
commit indefinitely. Unpinning is the second half of the rollback procedure.

- [x] **Step 6: Write the result into the evaluation plan** *(repo)*

In `docs/superpowers/plans/2026-08-06-komodo-evaluation.md`, under `## Verdict checkpoint — 2026-08-20`, replace the `Rollback is fast from the UI | Not yet exercised` row's note with the measured time and the exact UI path used.

```bash
git add docs/superpowers/plans/2026-08-06-komodo-evaluation.md
git commit -m "time a komodo ui rollback and write down how"
```

---

### Task 2: Move the tunnel into its own host-managed stack

Done before any stack migrates, so every later task has an intact way back in. The tunnel is the only route into this network from outside, and Komodo's own UI is behind it.

**Files:**
- Create: `tunnel/docker-compose.yml`
- Create: `tunnel/.env.example`
- Modify: `portainer/docker-compose.yml` (remove the `cloudflared` service)
- Modify: `scripts/bootstrap.sh:13` (STACKS list)
- Modify: `README.md` (stack table + repo layout + rebuild steps)

**Interfaces:**
- Consumes: `CLOUDFLARE_TUNNEL_TOKEN`, currently in `portainer/.env`.
- Produces: a `tunnel` compose project running one container named `cloudflared`, on the default bridge (it needs no shared network — it dials out and reaches Caddy by hostname over `internal`).

- [x] **Step 1: Create `tunnel/docker-compose.yml`** *(repo)*

The service is copied verbatim from `portainer/docker-compose.yml`, including its network membership, so nothing about how it reaches Caddy changes.

```yaml
# The Cloudflare tunnel — the only route into this network from outside.
#
# Host-managed like komodo/: brought up by hand with
#   docker compose --project-directory tunnel up -d
# and deliberately absent from .github/workflows/deploy.yml and from Komodo's
# Stacks. Nothing that deploys automatically can reach it, because a bad
# deploy of the stack holding the tunnel removes the path you would use to
# repair it — including Komodo's own UI, which is published through it.
services:
  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: cloudflared
    restart: unless-stopped
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
    command: tunnel run

networks:
  default:
    name: internal
    external: true # shared with the other stacks; created by scripts/bootstrap.sh
```

- [x] **Step 2: Create `tunnel/.env.example`** *(repo)*

```bash
# Cloudflare Tunnel configuration.
# Copy to tunnel/.env on the host and fill in. Never commit the filled copy.

# Zero Trust -> Networks -> Tunnels -> your tunnel -> Install and run a connector.
CLOUDFLARE_TUNNEL_TOKEN=changeme
```

- [x] **Step 3: Put the token on the host and start the new stack** *(host)*

The old `cloudflared` is still running at this point. Compose refuses to start a second container named `cloudflared`, so this step brings the new project up only after stopping the old container — a brief tunnel outage, which is why it happens before anything else has moved.

**Steps 1–2 must be committed and pushed before this runs** — the host checks
out `komodo-migration`, so `tunnel/` has to exist on the remote branch first.
Step 8's commit therefore covers only the repo edits in Steps 5–7.

```bash
ssh home
cd /home/lorainemg/homelab
git fetch origin && git checkout komodo-migration && git pull --ff-only
grep '^CLOUDFLARE_TUNNEL_TOKEN=' portainer/.env > tunnel/.env
docker stop cloudflared && docker rm cloudflared
docker compose --project-directory tunnel up -d
docker ps --filter name=cloudflared --format '{{.Names}}\t{{.Status}}'
```

Expected: one `cloudflared`, `Up`.

- [x] **Step 4: Verify every published hostname still answers**

```bash
for h in portainer komodo grafana immich registry; do
  printf '%s: ' "$h"
  curl -s -o /dev/null -w '%{http_code}\n' "https://$h.sussman.win"
done
```

Expected: no `530` and no `000`. A `530` is Cloudflare saying the tunnel has no connector — that is the failure this step exists to catch. Redirects (`301`/`302`) and auth challenges (`401`/`403`) are fine; they prove the request reached Caddy.

**If this fails:** `docker compose --project-directory tunnel down` and restore `cloudflared` by running `docker compose --project-directory portainer up -d` from the pre-change checkout. The token is unchanged, so the old container reconnects.

- [x] **Step 5: Remove `cloudflared` from the Portainer stack** *(repo)*

Delete the `cloudflared` service block and its leading comment from `portainer/docker-compose.yml`, leaving the `portainer` service, the `portainer_data` volume and the `networks:` block untouched. Delete the `CLOUDFLARE_TUNNEL_TOKEN` line from `portainer/.env.example`.

- [x] **Step 6: Swap `portainer` for `tunnel` in the bootstrap order** *(repo)*

In `scripts/bootstrap.sh`, change the STACKS line and its comment:

```bash
# tunnel first: it's the only route in from outside, and everything else is
# published through it.
STACKS=(tunnel portainer registry config immich home-assistant monitoring)
```

`portainer` stays in the list until Task 8 retires it.

- [x] **Step 7: Update the README** *(repo)*

In the stack table, add a `tunnel/` row and amend the `portainer/` row so it no longer claims to hold the tunnel:

```markdown
| [tunnel/](tunnel/) | cloudflared | The Cloudflare Tunnel every published service is reached through. Host-managed, never deployed — a bad deploy of the stack holding the tunnel would remove the path used to repair it |
| [portainer/](portainer/) | Portainer CE | Outgoing control plane, retained as the rollback target until the migration completes. Host-managed, never CI-deployed |
```

Add `├── tunnel/          docker-compose.yml (cloudflared), .env.example` to the repo layout block, and change rebuild step 3 to bring up `tunnel` before `portainer`.

- [x] **Step 8: Commit** *(repo)*

```bash
git add tunnel/ portainer/ scripts/bootstrap.sh README.md
git commit -m "give the tunnel its own stack so no deploy can cut the way in"
```

---

### Task 3: Convert `monitoring` to checkout-mounted config

Repo-only. Nothing deploys here — this task produces a compose file that resolves correctly and four deleted Dockerfiles. `monitoring` is still running under Portainer from its old image at the end of it.

**Files:**
- Modify: `monitoring/docker-compose.yml`
- Delete: `monitoring/promtail/Dockerfile`, `monitoring/tempo/Dockerfile`, `monitoring/otelcol/Dockerfile`, `monitoring/prometheus/Dockerfile`
- Create: `monitoring/prometheus/secrets/.gitkeep`
- Create: `monitoring/.env.example`

**Interfaces:**
- Consumes: Komodo's on-host checkout at `/etc/komodo/stacks/monitoring/`, created in Task 4.
- Produces: a `monitoring` compose project whose four config-carrying services run upstream images with `./<service>` bind-mounted, and which reads `GRAFANA_ADMIN_PASSWORD` and `HA_TOKEN` from `monitoring/.env`.

- [x] **Step 1: Rewrite the four services to mount their config** *(repo)*

In `monitoring/docker-compose.yml`, replace the `prometheus`, `promtail`, `tempo` and `otel-collector` service definitions with these. Everything else in the file — `node-exporter`, `grafana`, `loki`, `cadvisor`, the `networks:` block, the `volumes:` block — is unchanged except for one addition in Step 2.

```yaml
  prometheus:
    # Upstream image; config comes from ./prometheus in the checkout beside
    # this file, mounted read-only.
    image: prom/prometheus:latest
    container_name: prometheus
    restart: unless-stopped
    entrypoint: ["/bin/sh", "/etc/prometheus/docker-entrypoint.sh"]
    ports:
      - "9090:9090"
    environment:
      - HA_TOKEN=${HA_TOKEN:-}
    volumes:
      - ./prometheus:/etc/prometheus:ro
      - prometheus_data:/prometheus
    tmpfs:
      # The entrypoint writes the HA token here at startup. Deliberately
      # OUTSIDE the read-only ./prometheus mount: docker cannot create a
      # mountpoint inside a read-only bind mount, and a placeholder directory
      # could not be committed anyway because .gitignore has **/secrets/.
      # tmpfs also suits the data — the token is rewritten from HA_TOKEN on
      # every start, so it never persists and never touches disk.
      - /run/prometheus:uid=65534,gid=65534,mode=0700
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--storage.tsdb.retention.time=30d'
    networks:
      - internal
```

```yaml
  promtail:
    image: grafana/promtail:latest
    container_name: promtail
    restart: unless-stopped
    volumes:
      - ./promtail:/etc/promtail:ro
      - /var/log:/var/log:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /var/run/docker.sock:/var/run/docker.sock
    command: -config.file=/etc/promtail/promtail.yml
    networks:
      - internal
```

```yaml
  tempo:
    image: grafana/tempo:2.4.2
    container_name: tempo
    restart: unless-stopped
    command: -config.file=/etc/tempo/tempo.yml
    volumes:
      - ./tempo:/etc/tempo:ro
      - tempo_data:/tmp/tempo
    networks:
      - internal
```

```yaml
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    container_name: otel_collector
    restart: unless-stopped
    command: ["--config=/etc/otelcol/otel-collector.yml"]
    volumes:
      - ./otelcol:/etc/otelcol:ro
    networks:
      - internal
      # Joins the trakt bot's network so the collector can forward telemetry
      # to its Aspire dashboard.
      - trakt-tg-bot_aspire
```

- [x] **Step 2: Leave the `volumes:` block alone** *(repo)*

In the `volumes:` block at the bottom of `monitoring/docker-compose.yml`:

```yaml
volumes:
  prometheus_data:
  grafana_data:
  loki_data:
  tempo_data:
```

Unchanged from before, in other words — an earlier draft of this step added a
`prometheus_secrets` volume, which does not work. See Step 1.

- [x] **Step 3: Point the token path outside the config mount** *(repo)*

The token cannot live under `/etc/prometheus`. Docker cannot create a
mountpoint inside a read-only bind mount, and the placeholder directory that
would work around that cannot be committed — `.gitignore` carries a blanket
`**/secrets/`. So the tmpfs goes at `/run/prometheus` instead, and the two
files that name the path follow it.

In `monitoring/prometheus/docker-entrypoint.sh`:

```sh
printf '%s' "${HA_TOKEN:-}" > /run/prometheus/ha_token
```

In `monitoring/prometheus/prometheus.yml`, under the `home-assistant` job:

```yaml
    authorization:
      credentials_file: /run/prometheus/ha_token
```

- [x] **Step 4: Delete the four Dockerfiles** *(repo)*

```bash
git rm monitoring/prometheus/Dockerfile monitoring/promtail/Dockerfile \
       monitoring/tempo/Dockerfile monitoring/otelcol/Dockerfile
```

- [x] **Step 5: Create `monitoring/.env.example`** *(repo)*

```bash
# Monitoring stack configuration.
# Copy to monitoring/.env on the host and fill in. Never commit the filled copy.
# Compose auto-loads .env from this directory, so nothing references it.

# Grafana's initial admin password.
GRAFANA_ADMIN_PASSWORD=changeme
# Long-lived Home Assistant access token; prometheus scrapes HA with it.
# HA -> your profile -> Security -> Long-lived access tokens.
HA_TOKEN=changeme
```

- [x] **Step 6: Verify the compose file resolves both ways** *(repo)*

The two invocations are Komodo's and `bootstrap.sh`'s. They must agree.

```bash
printf 'GRAFANA_ADMIN_PASSWORD=x\nHA_TOKEN=y\n' > monitoring/.env
docker compose -f monitoring/docker-compose.yml config | grep -A2 'source:.*monitoring'
docker compose --project-directory monitoring config | grep -A2 'source:.*monitoring'
```

Expected: both print the same absolute paths, ending `/monitoring/prometheus`, `/monitoring/promtail`, `/monitoring/tempo`, `/monitoring/otelcol`. Any `${...}` left unresolved, or a path under `/tmp`, means the file is wrong.

- [x] **Step 7: Verify no image reference survives** *(repo)*

```bash
grep -n 'ghcr.io/lorainemg/homelab' monitoring/docker-compose.yml
```

Expected: no output. Any hit means a service still points at a deleted build.

- [x] **Step 8: Commit** *(repo)*

```bash
git add monitoring/ && git rm --cached monitoring/.env 2>/dev/null; \
git commit -m "read the monitoring configs from the checkout instead of baking them"
```

Confirm `monitoring/.env` is not in the commit: `git show --stat HEAD | grep -c '\.env$'` must print `0`.

---

### Task 4: Cut `monitoring` over to Komodo

The first real cutover, and the one that exercises every new mechanism at once. It is also the stack with the most named-volume exposure — all four of its data volumes are project-prefixed — so this is where the pinned project name earns its keep.

**Files:**
- Modify: `.github/workflows/deploy.yml` (remove `monitoring` from the deploy matrix and its paths-filter entry)

**Interfaces:**
- Consumes: the compose file from Task 3.
- Produces: a Komodo Stack named `monitoring` with `project_name: monitoring`, deploying `monitoring/docker-compose.yml` from `main`.

- [x] **Step 1: Capture the baseline that Step 9 checks against**

Before anything is deleted. This is the data assertion — a container health check would pass against an empty volume.

```bash
curl -s 'https://grafana.sussman.win/api/search?type=dash-db' | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d),"dashboards");[print(" -",x["title"]) for x in d]'
ssh home "curl -sG 'http://localhost:9090/api/v1/query' --data-urlencode 'query=count(up)'" | python3 -c 'import sys,json;print("series:",json.load(sys.stdin)["data"]["result"])'
```

Write both down. If Grafana's API requires auth, use `-u admin:$GRAFANA_ADMIN_PASSWORD`
(the live value is readable from the running container's env).

Measured 2026-08-23, before the cutover:

- **5 dashboards** — Apps Logs, Container Dashboard, Home Assistant, Host Data,
  Immich Overview
- **`count(up)` = 5** — five scrape targets answering
- volumes: `monitoring_grafana_data`, `monitoring_loki_data`,
  `monitoring_prometheus_data`, `monitoring_tempo_data`

- [x] **Step 2: List the volumes that must survive** *(host)*

```bash
ssh home 'docker volume ls --format "{{.Name}}" | grep "^monitoring_"'
```

Expected exactly: `monitoring_grafana_data`, `monitoring_loki_data`, `monitoring_prometheus_data`, `monitoring_tempo_data`.

- [x] **Step 3: Take `monitoring` out of CI** *(repo)*

Done *before* the stack moves, so a later push cannot recreate it in Portainer alongside Komodo's copy — two control planes fighting over one compose project.

In `.github/workflows/deploy.yml`:
- remove `"monitoring"` from the `workflow_dispatch` matrix list on line 58, leaving `fromJSON('["config","immich","home-assistant"]')`
- delete the `monitoring` entry from the `changes` job's paths-filter
- add `monitoring` to the filter-output exclusion already used for `registry`, so the dynamic `needs.changes.outputs.stacks` list cannot contain it either

```bash
git add .github/workflows/deploy.yml
git commit -m "stop deploying monitoring from CI"
```

- [ ] **Step 4: Merge the branch so far to `main`** *(repo)*

Komodo deploys from `main`, so the new compose file has to be there before the Stack is created.

```bash
git checkout main && git merge --no-ff komodo-migration -m "move the tunnel out and take monitoring off CI"
git push origin main
git checkout komodo-migration
```

This push deploys `config`, `immich` and `home-assistant` through Portainer as usual, and does nothing to `monitoring`.

- [ ] **Step 5: Delete the Portainer stack, keeping the volumes**

In Portainer, Stacks → `monitoring` → Delete. Portainer removes containers and leaves named volumes.

- [ ] **Step 6: Verify the volumes survived — hard gate** *(host)*

```bash
ssh home 'docker volume ls --format "{{.Name}}" | grep "^monitoring_"'
```

Expected: the same four names from Step 2. **If any are missing, stop.** Restore them before continuing; everything after this assumes they exist.

- [ ] **Step 7: Create the Komodo Stack** *(API)*

`project_name` is set explicitly even though the name alone would produce it. It is the one field whose omission destroys data.

```bash
curl -s -X POST https://komodo.sussman.win/write -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{
  "type": "CreateStack",
  "params": {
    "name": "monitoring",
    "config": {
      "server_id": "Local",
      "project_name": "monitoring",
      "git_provider": "github.com",
      "repo": "lorainemg/homelab",
      "branch": "main",
      "file_paths": ["monitoring/docker-compose.yml"],
      "webhook_enabled": true,
      "auto_pull": true,
      "auto_update": false,
      "poll_for_updates": false
    }
  }
}' | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("name"), d.get("_id",{}).get("$oid",""))'
```

- [ ] **Step 8: Place the secrets in Komodo's checkout, then deploy** *(host)*

The checkout only exists after Komodo has pulled once, so deploy, place the `.env`, then deploy again. The first deploy brings Grafana up with a default admin password for a few seconds; that is why this stack goes first and Immich does not.

```bash
ssh home
JWT=... # per the preamble
curl -s -X POST https://komodo.sussman.win/execute -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"type":"DeployStack","params":{"stack":"monitoring"}}' >/dev/null
sleep 30
cp /home/lorainemg/homelab/monitoring/.env /etc/komodo/stacks/monitoring/monitoring/.env
curl -s -X POST https://komodo.sussman.win/execute -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"type":"DeployStack","params":{"stack":"monitoring"}}' >/dev/null
```

If `/home/lorainemg/homelab/monitoring/.env` does not exist on the server, create it from `monitoring/.env.example` with the real values first — they are the same values currently held as the `GRAFANA_ADMIN_PASSWORD` and `HA_TOKEN` GitHub Actions secrets.

- [ ] **Step 9: Verify the data, not the containers**

```bash
curl -s 'https://grafana.sussman.win/api/search?type=dash-db' | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d),"dashboards")'
ssh home "curl -sG 'http://localhost:9090/api/v1/query' --data-urlencode 'query=count(up)'" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["result"])'
```

Expected: the same dashboard count and title list from Step 1, and a non-empty query result. An empty dashboard list means Grafana came up on a fresh volume — go to Rollback.

- [ ] **Step 10: Verify Prometheus is reading the mounted config and its token**

```bash
ssh home 'docker exec prometheus cat /run/prometheus/ha_token | wc -c; docker exec prometheus ls -l /etc/prometheus/prometheus.yml'
ssh home "curl -sG 'http://localhost:9090/api/v1/query' --data-urlencode 'query=up{job=~\".*home.*\"}'" | head -c 300
```

Expected: a non-zero byte count for the token, `prometheus.yml` present, and the Home Assistant scrape target reporting. A zero-byte token means `HA_TOKEN` is missing from `monitoring/.env` on the host; a *missing* file means the `/run/prometheus` tmpfs is missing from the compose file.

- [ ] **Step 11: Verify a config edit reaches the container without a build**

The point of the whole exercise. Change something harmless and observable in `monitoring/prometheus/prometheus.yml` — e.g. `scrape_interval` — commit, push to `main`, and wait for the webhook.

```bash
ssh home 'docker exec prometheus grep scrape_interval /etc/prometheus/prometheus.yml'
```

Expected: the new value, with no GitHub Actions run having occurred. Revert the change and push again once confirmed.

- [ ] **Step 12: Add the GitHub webhook**

Repo Settings → Webhooks → Add webhook.

| Field | Value |
|---|---|
| Payload URL | `https://komodo.sussman.win/listener/github/stack/monitoring/deploy` |
| Content type | `application/json` |
| Secret | the value of `KOMODO_WEBHOOK_SECRET` in `komodo/.env` |
| Events | push only |

If Step 11 already deployed without this, the listener was reached some other way — check before assuming it works.

- [ ] **Step 13: Commit the README change** *(repo)*

Amend the `monitoring/` row's "Why" in the README stack table to note it is deployed by Komodo from this repo on a webhook, not by CI.

```bash
git add README.md && git commit -m "note that komodo deploys monitoring now"
```

**Rollback for this task:** delete the Komodo Stack (`{"type":"DeleteStack","params":{"stack":"monitoring"}}` on `/write`), restore `monitoring` to the `deploy.yml` matrix and paths-filter, revert Task 3's commit, push, and run the Deploy workflow with `workflow_dispatch`. The volumes reattach because the project name is `monitoring` on both sides.

---

### Task 5: Cut `immich` over to Komodo

Less exposed to the project-name hazard than it looks: its only named volume is `immich_model-cache`, a rebuildable ML cache. The library and Postgres data are bind mounts (`/data/immich/library`, `/data/immich/postgres`), and bind paths carry no project prefix, so they reattach regardless. `immich` has no config to un-bake — all four of its images are upstream — so this task is a pure control-plane move.

**Files:**
- Create: `immich/.env.example` additions (fold `immich/deploy.env` in)
- Delete: `immich/deploy.env`
- Modify: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: the Komodo Stack pattern from Task 4.
- Produces: a Komodo Stack named `immich` with `project_name: immich`.

- [ ] **Step 1: Capture the baseline**

```bash
curl -s https://immich.sussman.win/api/server/statistics -H "x-api-key: $IMMICH_API_KEY" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("photos",d.get("photos"),"videos",d.get("videos"),"usage",d.get("usage"))'
```

Write down the counts. If you have no API key, use the web UI and note the asset count from the library page. This number is the acceptance test in Step 8.

- [ ] **Step 2: Fold `deploy.env` into `.env.example`** *(repo)*

Compose auto-loads `.env` from the stack directory; the split between committed non-secret values and injected secrets only existed because CI assembled them. Append the contents of `immich/deploy.env` to `immich/.env.example`, add `DB_PASSWORD=changeme` with a comment, then:

```bash
git rm immich/deploy.env
git add immich/.env.example
git commit -m "fold immich's deploy.env into its env example"
```

- [ ] **Step 3: Take `immich` out of CI, merge, and push** *(repo)*

Same two edits as Task 4 Step 3 — the `workflow_dispatch` list becomes `fromJSON('["config","home-assistant"]')`, and `immich` joins the exclusion applied to the dynamic list. Then merge to `main` and push.

```bash
git add .github/workflows/deploy.yml && git commit -m "stop deploying immich from CI"
git checkout main && git merge --no-ff komodo-migration -m "take immich off CI" && git push origin main
git checkout komodo-migration
```

- [ ] **Step 4: Record the bind-mount paths before deleting anything** *(host)*

```bash
ssh home 'docker inspect immich_server --format "{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}"'
```

Expected to include `/data/immich/library` and, on the postgres container, `/data/immich/postgres`. These are what actually hold the photos.

- [ ] **Step 5: Delete the Portainer stack and verify the data directories are untouched**

Portainer → Stacks → `immich` → Delete, then:

```bash
ssh home 'du -sh /data/immich/library /data/immich/postgres; docker volume ls --format "{{.Name}}" | grep immich'
```

Expected: both directories still hold their previous size, and `immich_model-cache` still listed. **If the library directory is empty or gone, stop.**

- [ ] **Step 6: Create the Komodo Stack** *(API)*

```bash
curl -s -X POST https://komodo.sussman.win/write -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{
  "type": "CreateStack",
  "params": {
    "name": "immich",
    "config": {
      "server_id": "Local",
      "project_name": "immich",
      "git_provider": "github.com",
      "repo": "lorainemg/homelab",
      "branch": "main",
      "file_paths": ["immich/docker-compose.yml"],
      "webhook_enabled": true,
      "auto_pull": true,
      "auto_update": false,
      "poll_for_updates": false
    }
  }
}' | python3 -c 'import sys,json;print(json.load(sys.stdin).get("name"))'
```

- [ ] **Step 7: Place `.env` and deploy** *(host)*

Unlike `monitoring`, do **not** deploy before the `.env` exists — Immich's Postgres would initialise with an empty password and the server would then fail to connect with the real one.

Komodo creates the checkout on its first deploy, which is too late here. Create
it by hand first — Komodo's own `git pull` reconciles it on the next deploy, and
the untracked `.env` survives that pull:

```bash
ssh home
git clone --branch main --depth 1 https://github.com/lorainemg/homelab /etc/komodo/stacks/immich
cp /home/lorainemg/homelab/immich/.env /etc/komodo/stacks/immich/immich/.env
ls -l /etc/komodo/stacks/immich/immich/.env
```

Expected: the file exists. Then deploy *(API)*:

```bash
curl -s -X POST https://komodo.sussman.win/execute -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"type":"DeployStack","params":{"stack":"immich"}}' >/dev/null
```

- [ ] **Step 8: Verify the library, not the containers**

```bash
curl -s https://immich.sussman.win/api/server/statistics -H "x-api-key: $IMMICH_API_KEY" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("photos",d.get("photos"),"videos",d.get("videos"))'
```

Expected: the counts from Step 1. Immich backed by an empty database starts perfectly healthy and shows zero assets — that is the failure this asserts against.

- [ ] **Step 9: Add the GitHub webhook**

Payload URL `https://komodo.sussman.win/listener/github/stack/immich/deploy`, same secret and settings as Task 4 Step 12.

- [ ] **Step 10: Commit the README change** *(repo)*

```bash
git add README.md && git commit -m "note that komodo deploys immich now"
```

**Rollback for this task:** delete the Komodo Stack, restore `immich` to `deploy.yml`, restore `immich/deploy.env` from git history, push, and run the Deploy workflow. The library and database are bind mounts and are never at risk from the project name.

---

### Task 6: Cut `home-assistant` over to Komodo

The stack with no named volumes at all — every piece of state is bind-mounted from the data root, so the project-name hazard does not apply. It carries no secrets either. The care here is about the config-agent seam: `config` (still on Portainer at this point) syncs HA's yaml into the live directory, and that keeps working throughout because the two stacks only meet on disk.

**Files:**
- Modify: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: the Komodo Stack pattern from Task 4.
- Produces: a Komodo Stack named `home-assistant` with `project_name: home-assistant`.

- [ ] **Step 1: Capture the baseline**

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" https://home-assistant.sussman.win/api/states \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d),"entities")'
ssh home 'docker ps --filter label=com.docker.compose.project=home-assistant --format "{{.Names}}"'
```

Write down the entity count and the container list (expect six: home assistant, mosquitto, whisper, piper, ollama, ollama-pull).

- [ ] **Step 2: Take `home-assistant` out of CI, merge, and push** *(repo)*

The `workflow_dispatch` list becomes `fromJSON('["config"]')`; `home-assistant` joins the dynamic-list exclusion. Note the paths-filter entry for this stack has exclusions (`!home-assistant/ha-config/**`, `!home-assistant/mosquitto/**`) because those paths belong to the `config` stack's image — leave the `config` filter's use of them alone.

```bash
git add .github/workflows/deploy.yml && git commit -m "stop deploying home-assistant from CI"
git checkout main && git merge --no-ff komodo-migration -m "take home-assistant off CI" && git push origin main
git checkout komodo-migration
```

- [ ] **Step 3: Record the bind mounts** *(host)*

```bash
ssh home 'for c in homeassistant mosquitto ollama; do echo "== $c"; docker inspect $c --format "{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}"; done'
```

- [ ] **Step 4: Delete the Portainer stack and verify the data root** *(host)*

```bash
ssh home 'du -sh /data/home-assistant/ha-config /data/mosquitto/config; ls /data/home-assistant/ha-config/.storage | head -3'
```

Expected: unchanged sizes and a populated `.storage`. **If `.storage` is empty, stop** — that directory holds every entity registration and every credential HA has.

- [ ] **Step 5: Create the Komodo Stack** *(API)*

```bash
curl -s -X POST https://komodo.sussman.win/write -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{
  "type": "CreateStack",
  "params": {
    "name": "home-assistant",
    "config": {
      "server_id": "Local",
      "project_name": "home-assistant",
      "git_provider": "github.com",
      "repo": "lorainemg/homelab",
      "branch": "main",
      "file_paths": ["home-assistant/docker-compose.yml"],
      "webhook_enabled": true,
      "auto_pull": true,
      "auto_update": false,
      "poll_for_updates": false
    }
  }
}' | python3 -c 'import sys,json;print(json.load(sys.stdin).get("name"))'
```

- [ ] **Step 6: Place `.env` and deploy** *(host)*

`home-assistant/.env` holds only non-secret values (`TZ`, `DATA_ROOT` and friends — check `home-assistant/.env.example`), but compose still needs it for the bind-mount paths to resolve.

```bash
ssh home
git clone --branch main --depth 1 https://github.com/lorainemg/homelab /etc/komodo/stacks/home-assistant
cp /home/lorainemg/homelab/home-assistant/.env /etc/komodo/stacks/home-assistant/home-assistant/.env
JWT=... # per the preamble
curl -s -X POST https://komodo.sussman.win/execute -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"type":"DeployStack","params":{"stack":"home-assistant"}}' >/dev/null
```

- [ ] **Step 7: Verify the entities and the voice pipeline**

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" https://home-assistant.sussman.win/api/states \
  | python3 -c 'import sys,json;print(len(json.load(sys.stdin)),"entities")'
ssh home 'docker ps --filter label=com.docker.compose.project=home-assistant --format "{{.Names}}\t{{.Status}}"'
```

Expected: the entity count from Step 1, and the same containers. A fresh HA volume produces a working server with an onboarding screen and zero entities — that is the failure mode this asserts against. Then speak one command through Assist to confirm Whisper → Ollama → Piper still answers.

- [ ] **Step 8: Confirm config-agent still syncs into the moved stack** *(host)*

`config` is still on Portainer; the seam between them is the `/data` directory, which neither control plane moved.

```bash
ssh home 'docker logs --tail 20 config-agent'
```

Expected: its usual sync output with no errors. Then edit a watched file (e.g. `home-assistant/ha-config/scenes.yaml`), push to `main`, and confirm the change reaches `/data/home-assistant/ha-config/scenes.yaml`.

- [ ] **Step 9: Add the GitHub webhook**

Payload URL `https://komodo.sussman.win/listener/github/stack/home-assistant/deploy`.

- [ ] **Step 10: Commit the README change** *(repo)*

```bash
git add README.md && git commit -m "note that komodo deploys home assistant now"
```

**Rollback for this task:** delete the Komodo Stack, restore `home-assistant` to `deploy.yml`, push, run the Deploy workflow. No named volumes are involved, so the risk here is downtime, not data.

---

### Task 7: Convert and cut over `config`, and shrink CI to a build

Last, because `config` owns Caddy and every previous cutover was verified through it. This is also the only stack where the build/deploy race still exists, so it is the only one CI still triggers.

**Files:**
- Create: `config/caddy/Caddyfile` (moved from `config/Caddyfile`)
- Modify: `config/docker-compose.yml`
- Modify: `config/config-agent/Dockerfile`
- Modify: `config/config-agent/agent.sh:52` (drop the Caddyfile sync)
- Modify: `config/.env.example` (fold in `config/deploy.env`)
- Delete: `config/deploy.env`
- Modify: `.github/workflows/deploy.yml` (strip to one build job plus a Komodo trigger)

**Interfaces:**
- Consumes: Komodo's checkout at `/etc/komodo/stacks/config/`.
- Produces: a Komodo Stack named `config` with `project_name: config`; a `deploy.yml` whose only job builds `ghcr.io/lorainemg/homelab/config-agent` and then calls Komodo's `config` listener.

- [ ] **Step 1: Move the Caddyfile into a directory of its own** *(repo)*

The directory rule: a single-file bind mount from a git checkout is frozen at the inode present when the container started, so Caddy must mount a directory.

```bash
mkdir -p config/caddy
git mv config/Caddyfile config/caddy/Caddyfile
```

- [ ] **Step 2: Mount it into Caddy and stop the agent syncing it** *(repo)*

In `config/docker-compose.yml`, replace the caddy service's `caddy_conf:/etc/caddy` volume with the checkout mount, and drop `caddy_conf` from config-agent's volumes:

```yaml
    volumes:
      # Mounted from the checkout, not written by config-agent: a pull updates
      # the file on disk and --watch reloads it in place, so no image build and
      # no container recreation sit between an edit and Caddy running it.
      # Directory, not file: git replaces files, and a single-file bind mount
      # would stay pinned to the old inode forever.
      - ./caddy:/etc/caddy:ro
      - caddy_data:/data
      - caddy_config:/config
```

Then point config-agent's `/src` at the checkout instead of the image:

```yaml
    volumes:
      - ../home-assistant/ha-config:/src/ha-config:ro
      - ../home-assistant/mosquitto:/src/mosquitto:ro
      - ${DATA_ROOT:-/data}/home-assistant/ha-config:/live/ha-config
      - ${DATA_ROOT:-/data}/mosquitto/config:/live/mosquitto
```

Remove `caddy_conf:` from the `volumes:` block at the bottom of the file.

- [ ] **Step 3: Drop the Caddyfile from the agent** *(repo)*

In `config/config-agent/agent.sh`, delete the line `sync_dir /src/caddy /live/caddy` and amend the header comment so it no longer claims to own the Caddyfile. In `config/config-agent/Dockerfile`, delete the three `COPY` lines that bake config, leaving only the agent script:

```dockerfile
FROM alpine:3.22

RUN apk add --no-cache git curl

# Config is mounted from Komodo's checkout at runtime, not baked in — only the
# agent itself ships in this image, which is why it almost never rebuilds.
COPY config/config-agent/agent.sh /agent.sh

ENTRYPOINT ["/bin/sh", "/agent.sh"]
```

- [ ] **Step 4: Fold `deploy.env` into `.env.example`** *(repo)*

Append `TZ=America/New_York` and `DOMAIN_BASE=sussman.win` to `config/.env.example`, plus `HOMELAB_PUSH_TOKEN=changeme` and `HA_TOKEN=changeme` with comments.

```bash
git rm config/deploy.env
```

- [ ] **Step 5: Verify the compose file resolves both ways** *(repo)*

```bash
docker compose -f config/docker-compose.yml config | grep -A2 'source:'
docker compose --project-directory config config | grep -A2 'source:'
```

Expected: both print identical absolute paths, and the `../home-assistant/...` mounts resolve to the repo's `home-assistant/` directory, not to a path outside the repo.

- [ ] **Step 6: Strip `deploy.yml` to one job** *(repo)*

Everything except the `config-agent` build goes: the four monitoring image builds, the `Assemble stack env` step, the `cssnr/portainer-stack-deploy-action` step, the stack matrix and the `changes` job's now-unused outputs. What remains:

```yaml
name: Deploy
on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  build-config-agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: images
        with:
          filters: |
            config_agent:
              - 'config/config-agent/**'
      - uses: docker/login-action@v3
        if: github.event_name == 'workflow_dispatch' || steps.images.outputs.config_agent == 'true'
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        if: github.event_name == 'workflow_dispatch' || steps.images.outputs.config_agent == 'true'
        with:
          context: .
          file: config/config-agent/Dockerfile
          push: true
          tags: |
            ghcr.io/${{ github.repository }}/config-agent:latest
            ghcr.io/${{ github.repository }}/config-agent:${{ github.sha }}
      # The image must exist in GHCR before Komodo pulls it. This step is what
      # closes the build/deploy race: same job, strictly after the build.
      - name: Tell Komodo to deploy config
        if: github.event_name == 'workflow_dispatch' || steps.images.outputs.config_agent == 'true'
        run: |
          curl -sf -X POST "${{ secrets.KOMODO_URL }}/listener/github/stack/config/deploy" \
            -H 'Content-Type: application/json' \
            -H "X-Hub-Signature-256: sha256=$(printf '%s' '{}' | openssl dgst -sha256 -hmac '${{ secrets.KOMODO_WEBHOOK_SECRET }}' | cut -d' ' -f2)" \
            -H 'X-GitHub-Event: push' \
            -d '{}'
```

**Verify this trigger before relying on it.** Komodo's GitHub listener may
require a parseable push payload (it filters on `ref` against the Stack's
branch), in which case an empty `{}` body is rejected and nothing deploys —
silently, because the `curl` still gets a 2xx. Test it by hand once, against
the `docker-registry` listener, and check for a new Update in Komodo. If it is
rejected, use the authenticated API instead of the listener, which needs a
Komodo API key rather than the webhook secret:

```yaml
      - name: Tell Komodo to deploy config
        if: github.event_name == 'workflow_dispatch' || steps.images.outputs.config_agent == 'true'
        run: |
          curl -sf -X POST "${{ secrets.KOMODO_URL }}/execute" \
            -H "Authorization: Bearer ${{ secrets.KOMODO_API_KEY }}" \
            -H 'Content-Type: application/json' \
            -d '{"type":"DeployStack","params":{"stack":"config"}}'
```

Add `KOMODO_URL` and `KOMODO_WEBHOOK_SECRET` (or `KOMODO_API_KEY`) as Actions secrets. Delete `PORTAINER_URL` and `PORTAINER_API_TOKEN`, plus `GRAFANA_ADMIN_PASSWORD`, `HA_TOKEN` and `DB_PASSWORD` — those now live in host `.env` files. Keep `HOMELAB_PUSH_TOKEN`: it is baked into nothing but is still passed to the agent through `config/.env` on the host, so delete it from Actions only after confirming that.

- [ ] **Step 7: Commit and merge to `main`** *(repo)*

```bash
git add config/ .github/workflows/deploy.yml
git commit -m "read caddy's config from the checkout and shrink ci to one build"
git checkout main && git merge --no-ff komodo-migration -m "move config onto komodo" && git push origin main
git checkout komodo-migration
```

This push still deploys `config` through Portainer one last time, from the *old* compose in Portainer's copy — harmless, because the next step replaces it.

- [ ] **Step 8: Delete the Portainer stack and verify the volumes** *(host)*

```bash
ssh home 'docker volume ls --format "{{.Name}}" | grep "^config_"'
```

Expected: `config_caddy_data` and `config_caddy_config` still present. `config_caddy_conf` is now unused and can be removed later; leave it for now as a fallback copy of the Caddyfile.

- [ ] **Step 9: Create the Komodo Stack, place `.env`, deploy** *(API + host)*

```bash
curl -s -X POST https://komodo.sussman.win/write -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{
  "type": "CreateStack",
  "params": {
    "name": "config",
    "config": {
      "server_id": "Local",
      "project_name": "config",
      "git_provider": "github.com",
      "repo": "lorainemg/homelab",
      "branch": "main",
      "file_paths": ["config/docker-compose.yml"],
      "webhook_enabled": true,
      "auto_pull": true,
      "auto_update": false,
      "poll_for_updates": false
    }
  }
}' | python3 -c 'import sys,json;print(json.load(sys.stdin).get("name"))'
```

```bash
ssh home
git clone --branch main --depth 1 https://github.com/lorainemg/homelab /etc/komodo/stacks/config
cp /home/lorainemg/homelab/config/.env /etc/komodo/stacks/config/config/.env
JWT=... # per the preamble
curl -s -X POST https://komodo.sussman.win/execute -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"type":"DeployStack","params":{"stack":"config"}}' >/dev/null
```

**This is the deploy most likely to lock you out**, because it recreates Caddy. If hostnames stop answering, the tunnel is still up (Task 2) — reach the box over SSH and run `docker compose --project-directory /etc/komodo/stacks/config/config up -d` by hand.

- [ ] **Step 10: Verify every hostname still routes**

```bash
for h in komodo grafana immich home-assistant registry portainer; do
  printf '%s: ' "$h"; curl -s -o /dev/null -w '%{http_code}\n' "https://$h.sussman.win"
done
```

Expected: no `502` and no `530`.

- [ ] **Step 11: Verify a Caddyfile edit hot-reloads with no build and no recreation**

The single clearest proof the design works. Note Caddy's container start time, edit `config/caddy/Caddyfile` (add a comment), push to `main`, then:

```bash
ssh home 'docker exec config-caddy grep -c . /etc/caddy/Caddyfile; docker inspect config-caddy --format "{{.State.StartedAt}}"'
```

Expected: the new line count, and a `StartedAt` **unchanged** from before the push. A changed start time means the container was recreated — the failure the volume-and-`--watch` design exists to prevent.

- [ ] **Step 12: Verify an `agent.sh` edit reaches the agent through CI**

The other half: this one *must* involve a build. Add a harmless `echo` to `config/config-agent/agent.sh`, push, watch the Actions run build and push the image, then confirm Komodo redeployed *after* it:

```bash
ssh home 'docker logs --tail 5 config-agent; docker inspect config-agent --format "{{.Config.Image}} {{.State.StartedAt}}"'
```

Expected: the new echo present, and a start time later than the Actions run's completion. If the agent is running the old script, the trigger fired before the push completed — the race, and the reason this step exists.

- [ ] **Step 13: Add the GitHub webhook and commit the README** *(repo)*

Add the `config` webhook (`.../stack/config/deploy`) for completeness, so a compose-only change deploys without waiting on CI. Update the README's CI/CD section to describe the one-build pipeline and the Komodo trigger.

```bash
git add README.md && git commit -m "describe the komodo deploy pipeline"
```

---

### Task 8: Retire Portainer

**Files:**
- Delete: `portainer/docker-compose.yml`, `portainer/.env.example`
- Modify: `scripts/bootstrap.sh` (drop `portainer` from STACKS)
- Modify: `README.md`
- Modify: `LEARNING.md`

**Interfaces:**
- Consumes: nothing. Every stack is on Komodo by this point.
- Produces: a repo with one control plane.

- [ ] **Step 1: Confirm nothing is left on Portainer** *(host)*

```bash
ssh home "docker ps --format '{{index .Labels \"com.docker.compose.project\"}}' | sort -u"
```

Expected projects: `config`, `docker-registry`, `home-assistant`, `immich`, `komodo`, `monitoring`, `trakt-tg-bot`, `tunnel`. **If any container is still labelled by a Portainer-managed project you have not migrated, stop.**

- [ ] **Step 2: Stop Portainer but keep its data** *(host)*

```bash
ssh home 'cd /home/lorainemg/homelab && docker compose --project-directory portainer down'
docker volume ls --format '{{.Name}}' | grep portainer_data
```

Expected: the container gone, `portainer_data` still listed. Keep it for one month — it is the only copy of Portainer's stack definitions if a rollback is ever needed.

- [ ] **Step 3: Remove the hostname from the edge**

Delete the `portainer.sussman.win` public hostname from the Cloudflare tunnel, and its block from `config/caddy/Caddyfile`. Push; Caddy hot-reloads it away with no recreation.

- [ ] **Step 4: Delete the stack from the repo** *(repo)*

```bash
git rm -r portainer/
```

In `scripts/bootstrap.sh`, drop `portainer` from STACKS:

```bash
STACKS=(tunnel komodo registry config immich home-assistant monitoring)
```

Note `komodo` is now second: it is host-managed like `tunnel`, and bootstrap should bring the control plane up before the stacks it manages.

- [ ] **Step 5: Update the README** *(repo)*

Remove the `portainer/` row from the stack table and the `portainer/` line from the repo layout. Rewrite the opening paragraph and the CI/CD section: pushing to `main` now builds one image and Komodo deploys from the repo on webhooks. Rewrite the "Rebuilding from scratch" steps — `PORTAINER_URL` / `PORTAINER_API_TOKEN` are gone, replaced by `KOMODO_URL` / `KOMODO_WEBHOOK_SECRET`, and the per-stack `.env` files (now including the folded-in `deploy.env` values) are placed both in the clone and in Komodo's checkouts.

- [ ] **Step 6: Update `LEARNING.md`** *(repo)*

Move the migration entry from *Next* to *Covered* with the date, and add whatever turned out to be shaky during execution.

- [ ] **Step 7: Commit and merge** *(repo)*

```bash
git add -A
git commit -m "retire portainer"
git checkout main && git merge --no-ff komodo-migration -m "finish the komodo migration" && git push origin main
```

- [ ] **Step 8: Delete `portainer_data` after one month** *(host)*

Calendar item, not a step to run now.

```bash
ssh home 'docker volume rm portainer_data'
```

---

## Rollback

Per stack, at any point, without touching the others:

1. Delete the Komodo Stack: `POST /write {"type":"DeleteStack","params":{"stack":"<name>"}}`. Containers go; volumes stay.
2. Recreate it in Portainer under the identical project name from the unchanged compose file, with its env restored from the host `.env`.
3. Restore that stack's entry to the matrix and paths-filter in `.github/workflows/deploy.yml`, push, and run the Deploy workflow with `workflow_dispatch`.

The volume reattaches either way, because the project name is pinned on both sides. If the tunnel move (Task 2) is what broke, `cloudflared` returns to `portainer/docker-compose.yml` and comes back up from the host with an unchanged token.

Nothing here is one-way until Task 8 Step 8 deletes `portainer_data`, which is why that step waits a month.
