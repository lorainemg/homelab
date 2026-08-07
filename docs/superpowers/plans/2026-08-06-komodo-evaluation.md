# Komodo Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up Komodo v2 beside Portainer, move the `docker-registry` stack onto it with its data intact, and drive that stack's deploys straight from a GitHub webhook instead of CI.

**Architecture:** A new host-managed `komodo/` compose (mongo + core + periphery) published at `komodo.sussman.win` through the existing Caddy + Cloudflare Tunnel path. Komodo clones this repo itself and runs `docker compose -p docker-registry -f registry/docker-compose.yml`, so `registry` leaves `.github/workflows/deploy.yml` entirely. Portainer keeps its four remaining stacks and is the rollback target throughout.

**Tech Stack:** Docker Compose, Komodo v2.3.1 (`ghcr.io/moghtech/komodo-core`, `ghcr.io/moghtech/komodo-periphery`), MongoDB 8, Caddy 2, Cloudflare Tunnel, GitHub Actions, GitHub webhooks.

**Spec:** [docs/superpowers/specs/2026-08-06-komodo-evaluation-design.md](../specs/2026-08-06-komodo-evaluation-design.md)

## Global Constraints

- **Branch:** all repo work happens on `komodo-evaluation`, branched from `main`. Never on `vaultwarden`.
- **Compose project name for the registry stack is `docker-registry`** — never `registry`. The live volume is `docker-registry_registry-data`; any other project name silently creates an empty one.
- **`komodo/` is host-managed and never CI-deployed.** It must not appear in any paths-filter or deploy matrix in `.github/workflows/deploy.yml`.
- **Image tags are pinned.** `ghcr.io/moghtech/komodo-core:2.3.1`, `ghcr.io/moghtech/komodo-periphery:2.3.1`, `mongo:8`. Never `:latest`, never the bare `:2` floating tag from Komodo's sample compose.
- **`PERIPHERY_ROOT_DIRECTORY` must be the identical path inside and outside the container** (`/etc/komodo:/etc/komodo`). Docker resolves bind paths on the host; a mismatch breaks stack deploys in ways that are hard to diagnose. See [moghtech/komodo#180](https://github.com/moghtech/komodo/discussions/180).
- **Secrets never in git.** `komodo/.env` is gitignored (covered by the existing `*/.env` rule); only `komodo/.env.example` with placeholder values is committed.
- **Portainer is not modified.** Its compose, its container, and its four other stacks stay exactly as they are.
- **Commit messages:** single line, casual, no trailers — matching the existing log (`add the vaultwarden stack`, `route vault.sussman.win to vaultwarden`).
- **Host commands** run on the homelab server over SSH. Repo commands run in the local clone. Each step says which.

---

### Task 1: Scaffold the `komodo/` stack in the repo

Repo-only. Nothing is deployed here — this task produces reviewable files and a compose file that parses.

**Files:**
- Create: `komodo/docker-compose.yml`
- Create: `komodo/.env.example`
- Verify: `.gitignore` (no change expected — confirm `*/.env` covers `komodo/.env`)

**Interfaces:**
- Consumes: the external `internal` network created by `scripts/bootstrap.sh`.
- Produces: three containers named `komodo-core`, `komodo-periphery` and `komodo-mongo` (compose *services* are `core`, `periphery`, `mongo`; the `container_name:` keys are what Docker DNS resolves on the shared `internal` network, which is why they are set explicitly rather than left to compose's `komodo-core-1` default). Task 3 reverse-proxies to `komodo-core:9120`. Task 2 brings this compose up.

- [ ] **Step 1: Create `komodo/docker-compose.yml`**

Derived from Komodo's official `mongo.compose.yaml`, with four deliberate changes from the upstream sample: pinned tags instead of `:2`, no published host port, a private `komodo` network with only Core additionally joining `internal`, and explicit `container_name`s so nothing collides on the shared network.

```yaml
# Komodo — second control plane, under evaluation alongside Portainer.
#
# Host-managed like portainer/: brought up by hand with
#   docker compose --project-directory komodo up -d
# and deliberately absent from .github/workflows/deploy.yml, so no CI deploy
# can break the thing performing deploys.
services:
  mongo:
    image: mongo:8
    container_name: komodo-mongo
    # Excludes this container from Komodo's own "Stop All Containers" action.
    # Without it, one misclick in the UI stops the database out from under Core.
    labels:
      komodo.skip:
    command: --quiet --wiredTigerCacheSizeGB 0.25
    restart: unless-stopped
    volumes:
      - mongo-data:/data/db
      - mongo-config:/data/configdb
    environment:
      MONGO_INITDB_ROOT_USERNAME: ${KOMODO_DATABASE_USERNAME}
      MONGO_INITDB_ROOT_PASSWORD: ${KOMODO_DATABASE_PASSWORD}
    networks:
      - komodo

  core:
    image: ghcr.io/moghtech/komodo-core:2.3.1
    container_name: komodo-core
    init: true
    restart: unless-stopped
    depends_on:
      - mongo
    # No ports: published. Caddy reaches this over `internal`, so nothing on
    # the LAN can bypass the reverse proxy by hitting a host port.
    env_file: ./.env
    environment:
      KOMODO_DATABASE_ADDRESS: komodo-mongo:27017
    volumes:
      # Core generates a keypair here; periphery reads it from the same volume.
      - keys:/config/keys
      - ${KOMODO_BACKUPS_PATH:-/etc/komodo/backups}:/backups
    networks:
      # `komodo` for its own database and agent; `internal` only so Caddy can
      # resolve komodo-core by name.
      - komodo
      - internal

  periphery:
    image: ghcr.io/moghtech/komodo-periphery:2.3.1
    container_name: komodo-periphery
    init: true
    restart: unless-stopped
    depends_on:
      - core
    env_file: ./.env
    volumes:
      - keys:/config/keys
      - /var/run/docker.sock:/var/run/docker.sock
      - /proc:/proc
      # Identical path inside and outside the container, on purpose. Docker
      # resolves bind paths on the host, so a mismatch here breaks stack
      # deploys confusingly. https://github.com/moghtech/komodo/discussions/180
      - ${PERIPHERY_ROOT_DIRECTORY:-/etc/komodo}:${PERIPHERY_ROOT_DIRECTORY:-/etc/komodo}
    networks:
      - komodo

volumes:
  mongo-data:
  mongo-config:
  keys:

networks:
  komodo:
    name: komodo
  internal:
    external: true # shared with the other stacks; created by scripts/bootstrap.sh
```

- [ ] **Step 2: Create `komodo/.env.example`**

```sh
# Komodo Core + Periphery configuration.
# Copy to komodo/.env on the host and fill in. Never commit the filled copy.

# --- Database ---
KOMODO_DATABASE_USERNAME=komodo
# Generate: openssl rand -base64 24
KOMODO_DATABASE_PASSWORD=changeme

# --- Core ---
# Public URL. Used for OAuth redirects and the webhook URLs Komodo suggests.
KOMODO_HOST=https://komodo.sussman.win
KOMODO_TITLE=Komodo

# Login with username + password.
KOMODO_LOCAL_AUTH=true
# The admin account is created on first launch from these values, so there is
# never an open-signup window to race.
KOMODO_INIT_ADMIN_USERNAME=admin
# Generate: openssl rand -base64 24
KOMODO_INIT_ADMIN_PASSWORD=changeme
# No second user is ever wanted here.
KOMODO_DISABLE_USER_REGISTRATION=true

# Name of the server resource auto-created for this host.
KOMODO_FIRST_SERVER_NAME=Local

# Authenticates incoming GitHub webhooks (HMAC over the request body).
# Must match the Secret field of the GitHub webhook exactly.
# Generate: openssl rand -hex 32
KOMODO_WEBHOOK_SECRET=changeme
# Signs session tokens. Generate: openssl rand -base64 32
KOMODO_JWT_SECRET=changeme

# Dated database backups land here on the host.
KOMODO_BACKUPS_PATH=/etc/komodo/backups

TZ=America/New_York

# --- Periphery ---
PERIPHERY_CORE_ADDRESS=ws://komodo-core:9120
PERIPHERY_CONNECT_AS=Local
PERIPHERY_CORE_PUBLIC_KEYS=file:/config/keys/core.pub
# Must match the bind mount in docker-compose.yml on both sides.
PERIPHERY_ROOT_DIRECTORY=/etc/komodo
```

- [ ] **Step 3: Confirm `komodo/.env` would be ignored**

Run (local repo):

```bash
git check-ignore -v komodo/.env
```

Expected: prints `.gitignore:3:*/.env	komodo/.env`. If it prints nothing, stop — the file would be committable and `.gitignore` needs a `komodo/.env` line before continuing.

- [ ] **Step 4: Verify the compose file parses**

Run (local repo):

```bash
cp komodo/.env.example komodo/.env
docker compose --project-directory komodo config >/dev/null && echo PARSE_OK
rm komodo/.env
```

Expected: `PARSE_OK`. A warning about the external `internal` network not existing locally is fine — the network lives on the server. If it errors on undefined variables, a name in the compose file does not match `.env.example`.

- [ ] **Step 5: Commit**

```bash
git add komodo/docker-compose.yml komodo/.env.example
git commit -m "add the komodo stack"
```

---

### Task 2: Bring Komodo up on the host

Host-only. No repo changes. Komodo runs but is not yet published or wired to anything.

**Files:**
- Create (on host, not in git): `komodo/.env`

**Interfaces:**
- Consumes: `komodo/docker-compose.yml` and `komodo/.env.example` from Task 1.
- Produces: a running `komodo-core` on the `internal` network answering on port 9120, and a Komodo *Server* resource named `Local` in `Connected` state. Task 3 publishes it; Task 4 creates a Stack on that server.

- [ ] **Step 1: Get the repo onto the host and create the env file**

Run (on the homelab host, in the clone of this repo):

```bash
git fetch origin && git checkout komodo-evaluation && git pull
mkdir -p /etc/komodo/backups
cp komodo/.env.example komodo/.env
```

- [ ] **Step 2: Fill in the four generated secrets**

Run (on host) and paste each value into the matching key in `komodo/.env`:

```bash
echo "KOMODO_DATABASE_PASSWORD=$(openssl rand -base64 24)"
echo "KOMODO_INIT_ADMIN_PASSWORD=$(openssl rand -base64 24)"
echo "KOMODO_WEBHOOK_SECRET=$(openssl rand -hex 32)"
echo "KOMODO_JWT_SECRET=$(openssl rand -base64 32)"
```

Record `KOMODO_INIT_ADMIN_PASSWORD` and `KOMODO_WEBHOOK_SECRET` somewhere you can retrieve them — the first is your login, the second must be typed into GitHub in Task 7.

- [ ] **Step 3: Start the stack**

Run (on host):

```bash
docker compose --project-directory komodo up -d
```

- [ ] **Step 4: Verify all three containers are running**

Run (on host):

```bash
docker compose --project-directory komodo ps
```

Expected: `komodo-mongo`, `komodo-core` and `komodo-periphery` all `running`. If `komodo-core` is restarting, read `docker logs komodo-core` — the usual cause is a database credential mismatch between the `mongo` and `core` env values.

- [ ] **Step 5: Verify Core answers on the internal network**

Run (on host) — from a throwaway container on `internal`, which also proves Caddy will be able to reach it in Task 3:

```bash
docker run --rm --network internal curlimages/curl:latest \
  -s -o /dev/null -w '%{http_code}\n' http://komodo-core:9120
```

Expected: `200`.

- [ ] **Step 6: Verify Periphery connected to Core**

Run (on host):

```bash
docker logs komodo-core 2>&1 | grep -i -m5 "periphery\|server.*connect"
```

Expected: a line showing the `Local` server connected or healthy. If instead you see repeated connection errors, check that `PERIPHERY_CORE_ADDRESS` is exactly `ws://komodo-core:9120` — it must match the `container_name`, not the service name.

No commit — this task changes nothing in git.

---

### Task 3: Publish `komodo.sussman.win`

**Files:**
- Modify: `config/Caddyfile` (append one block)

**Interfaces:**
- Consumes: `komodo-core:9120` from Task 2.
- Produces: a working `https://komodo.sussman.win` login. Task 7 points a GitHub webhook at this hostname.

- [ ] **Step 1: Add the Caddy block**

Append to `config/Caddyfile` (local repo), matching the existing style exactly:

```
http://komodo.sussman.win {
    reverse_proxy komodo-core:9120
}
```

- [ ] **Step 2: Add the tunnel hostname**

In the Cloudflare Zero Trust dashboard → Networks → Tunnels → your tunnel → Public Hostnames, add:

| Field | Value |
|---|---|
| Subdomain | `komodo` |
| Domain | `sussman.win` |
| Service | `http://caddy:80` |

Same as every other service — the tunnel always points at Caddy, and Caddy routes by hostname.

- [ ] **Step 3: Commit and push, which deploys the Caddyfile**

```bash
git add config/Caddyfile
git commit -m "route komodo.sussman.win to komodo"
git push -u origin komodo-evaluation
```

Then merge to `main` (or push directly to `main` if that is your habit for this repo) — the deploy only fires on `main`.

- [ ] **Step 4: Verify the config stack redeployed and Caddy hot-reloaded**

Watch the run:

```bash
gh run watch
```

Expected: the `config` stack job succeeds. This edit rebuilds the `config-agent` image and syncs the Caddyfile; Caddy picks it up under `--watch` without the container being recreated.

- [ ] **Step 5: Verify the hostname serves Komodo**

Run (from anywhere):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://komodo.sussman.win
```

Expected: `200`. Then open it in a browser and log in with `KOMODO_INIT_ADMIN_USERNAME` / `KOMODO_INIT_ADMIN_PASSWORD` from Task 2.

- [ ] **Step 6: Verify registration is actually closed**

In the browser, log out. The login page must offer **no** signup or "create account" option. If one appears, `KOMODO_DISABLE_USER_REGISTRATION` did not take effect — fix `komodo/.env` and `docker compose --project-directory komodo up -d` again before going further, because the hostname is now public.

---

### Task 4: Create the `docker-registry` Stack in Komodo

Nothing is deployed in this task. The Stack resource is created and left alone, so the risky cutover in Task 6 is a single button press with everything else already verified.

**Files:** none in git — this is Komodo UI configuration.

**Interfaces:**
- Consumes: the `Local` server from Task 2.
- Produces: a Komodo Stack named `docker-registry` with `project_name = docker-registry`. Task 6 deploys it; Task 7 webhooks it.

- [ ] **Step 1: Confirm Komodo can already see the live stack**

In Komodo → Containers, find the running `registry` container. Periphery holds the Docker socket, so Komodo sees every container on the host including ones Portainer owns.

This is the proof that Periphery is wired to the right daemon. If `registry` is not listed, stop — Task 6 will not work.

- [ ] **Step 2: Create the Stack**

Komodo → Stacks → New Stack. Set:

| Field | Value |
|---|---|
| Name | `docker-registry` |
| Server | `Local` |
| Source | Git repo |
| Git provider | `github.com` |
| Repo | `lorainemg/homelab` |
| Branch | `main` |
| File paths | `registry/docker-compose.yml` |
| Run directory | *(leave empty — repo root)* |
| Project name | `docker-registry` |
| Environment | *(leave empty)* |
| Auto update | off |
| Poll for updates | off |

- [ ] **Step 3: Verify the project name is set**

Re-open the Stack's config page and confirm **Project name** reads exactly `docker-registry`.

This is the single field whose omission destroys data. The live volume is `docker-registry_registry-data`; a Stack deploying under project `docker-registry` reattaches to it, and a Stack deploying under project `registry` creates an empty `registry_registry-data` and starts a healthy, empty registry. Do not continue until this reads correctly.

- [ ] **Step 4: Do NOT deploy**

Leave the Stack undeployed. Komodo may show it as `down` or `unknown` — that is expected, because Portainer still owns the running containers. Deploying now would race Portainer for the same project.

---

### Task 5: Take `registry` out of CI

**Files:**
- Modify: `.github/workflows/deploy.yml` (three edits)

**Interfaces:**
- Consumes: nothing.
- Produces: a workflow that no longer touches `registry`, so Task 6 can delete the Portainer stack without a later push recreating it.

- [ ] **Step 1: Delete the `registry` paths-filter entry**

In the `changes` job, remove these three lines:

```yaml
            registry:
              - 'registry/**'
              - '.github/workflows/deploy.yml'
```

With the filter gone, `needs.changes.outputs.stacks` can never contain `registry`, so the dynamic matrix is clean without needing a second output.

- [ ] **Step 2: Remove `registry` from the `workflow_dispatch` matrix list**

Line 59. Change it from:

```yaml
        stack: ${{ github.event_name == 'workflow_dispatch' && fromJSON('["config","monitoring","registry","immich","home-assistant"]') || fromJSON(needs.changes.outputs.stacks) }}
```

to:

```yaml
        stack: ${{ github.event_name == 'workflow_dispatch' && fromJSON('["config","monitoring","immich","home-assistant"]') || fromJSON(needs.changes.outputs.stacks) }}
```

Both halves matter. The hardcoded list drives `workflow_dispatch`; the paths-filter output drives pushes. Editing only one leaves the other still deploying `registry` to Portainer.

- [ ] **Step 3: Simplify the now-pointless name rewrite**

Line 160. The deploy step renames `registry` → `docker-registry`. With `registry` gone from the matrix, that conditional has no remaining caller. Change:

```yaml
          name: ${{ matrix.stack == 'registry' && 'docker-registry' || matrix.stack }}
```

to:

```yaml
          name: ${{ matrix.stack }}
```

- [ ] **Step 4: Verify the workflow is still valid YAML and the matrix is right**

Run (local repo):

```bash
python3 -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/deploy.yml')); print('YAML OK')"
grep -c "registry" .github/workflows/deploy.yml
```

Expected: `YAML OK`, then `0`. Any non-zero count means a reference survived — find it before continuing.

- [ ] **Step 5: Commit and push to `main`**

```bash
git add .github/workflows/deploy.yml
git commit -m "stop deploying the registry stack from CI"
git push
```

- [ ] **Step 6: Verify the push did not deploy registry**

```bash
gh run watch
```

Expected: the run either skips entirely or deploys only stacks you actually touched. No `registry` / `docker-registry` job appears. Portainer still holds the running stack — nothing has changed on the server yet.

---

### Task 6: Cut the stack over to Komodo

The only task that can lose data. Every step before this exists so that this one is short.

**Files:** none in git.

**Interfaces:**
- Consumes: the Stack from Task 4, the CI change from Task 5.
- Produces: `registry` running under Komodo with its original volume.

- [ ] **Step 1: Capture the baseline**

Run (on host):

```bash
curl -s https://registry.sussman.win/v2/_catalog | tee /tmp/registry-catalog-before.json
docker run --rm \
  -v docker-registry_registry-data:/from:ro \
  -v /data/backups:/to \
  alpine tar czf /to/registry-data-before-komodo.tgz -C /from .
ls -lh /data/backups/registry-data-before-komodo.tgz
```

Expected: a JSON object listing your image repositories, and a tarball of non-trivial size. **If the catalog is empty or the tarball is a few hundred bytes, stop** — either the volume name is wrong or there is nothing to migrate, and both mean the rest of this task is not doing what you think.

- [ ] **Step 2: Delete the stack in Portainer**

Portainer → Stacks → `docker-registry` → Delete. This removes the containers and leaves the named volume.

- [ ] **Step 3: Gate on the volume surviving**

Run (on host):

```bash
docker volume ls --filter name=docker-registry_registry-data --format '{{.Name}}'
```

Expected: `docker-registry_registry-data`.

**Hard gate.** If this prints nothing, do not deploy in Komodo. Recreate the volume and restore from the Step 1 tarball first:

```bash
docker volume create docker-registry_registry-data
docker run --rm \
  -v docker-registry_registry-data:/to \
  -v /data/backups:/from:ro \
  alpine tar xzf /from/registry-data-before-komodo.tgz -C /to
```

- [ ] **Step 4: Deploy from Komodo**

Komodo → Stacks → `docker-registry` → Deploy. Watch the log it streams; it should show a `git clone`, then `docker compose -p docker-registry -f registry/docker-compose.yml up -d`.

Confirm the log contains `-p docker-registry`. If it shows `-p registry`, stop and fix the Stack's project name — the deploy has just created an empty volume.

- [ ] **Step 5: Verify the container is attached to the original volume**

Run (on host):

```bash
docker inspect registry --format '{{range .Mounts}}{{.Name}}{{"\n"}}{{end}}'
```

Expected: `docker-registry_registry-data`.

- [ ] **Step 6: Verify the data — the real acceptance test**

Run (on host):

```bash
curl -s https://registry.sussman.win/v2/_catalog | tee /tmp/registry-catalog-after.json
diff <(jq -S . /tmp/registry-catalog-before.json) <(jq -S . /tmp/registry-catalog-after.json) && echo CATALOG_MATCHES
```

Expected: `CATALOG_MATCHES`.

A registry backed by an empty volume starts perfectly healthy and serves `{"repositories":[]}`. A green status badge in Komodo proves nothing here — only the catalog diff does.

---

### Task 7: Wire the webhook and prove push-to-deploy

**Files:**
- Modify: `registry/docker-compose.yml` (a comment, as the deploy trigger)

**Interfaces:**
- Consumes: the deployed Stack from Task 6, `KOMODO_WEBHOOK_SECRET` from Task 2.
- Produces: pushes to `main` touching `registry/` redeploy the stack with no CI involvement.

- [ ] **Step 1: Create the webhook**

GitHub → this repo → Settings → Webhooks → Add webhook:

| Field | Value |
|---|---|
| Payload URL | `https://komodo.sussman.win/listener/github/stack/docker-registry/deploy` |
| Content type | `application/json` |
| Secret | the exact `KOMODO_WEBHOOK_SECRET` value from Task 2 |
| SSL verification | enabled |
| Events | Just the push event |
| Active | checked |

- [ ] **Step 2: Verify GitHub's ping was accepted**

On the webhook's page, open **Recent Deliveries**. The initial `ping` must show a `2xx` response.

A `401` means the secret does not match `komodo/.env`. A `404` means the stack name in the URL is wrong. A timeout means Caddy or the tunnel hostname from Task 3 is not routing.

- [ ] **Step 3: Make a trivial change to trigger a real deploy**

Add a comment to the top of `registry/docker-compose.yml` (local repo):

```yaml
# Deployed by Komodo from this repo, not by CI. Stack: docker-registry.
```

- [ ] **Step 4: Push it to `main`**

```bash
git add registry/docker-compose.yml
git commit -m "note that komodo deploys the registry stack"
git push
```

- [ ] **Step 5: Verify Komodo deployed it and CI did not**

In Komodo → Stacks → `docker-registry` → Updates, a new deploy must appear within seconds, with a log showing the new commit hash.

Then:

```bash
gh run list --limit 1
```

Expected: either no new run, or a run with no `registry` job. **Both halves matter** — Komodo deploying is only half the claim; CI staying out of it is the other half.

- [ ] **Step 6: Re-verify the data survived the redeploy**

```bash
curl -s https://registry.sussman.win/v2/_catalog | jq -S . | diff <(jq -S . /tmp/registry-catalog-before.json) - && echo STILL_MATCHES
```

Expected: `STILL_MATCHES`.

---

### Task 8: Prove the backup restores, and document the evaluation

Closes the two verdict criteria that need setup rather than waiting: backup recoverability and resource cost.

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the running Komodo from Task 2.
- Produces: a verified backup/restore path and a recorded resource baseline.

- [ ] **Step 1: Take a database backup**

Run (on host):

```bash
docker run --rm \
  --network komodo \
  -v /etc/komodo/backups:/backups \
  -e KOMODO_DATABASE_ADDRESS=komodo-mongo:27017 \
  -e KOMODO_DATABASE_USERNAME="$(grep '^KOMODO_DATABASE_USERNAME=' komodo/.env | cut -d= -f2-)" \
  -e KOMODO_DATABASE_PASSWORD="$(grep '^KOMODO_DATABASE_PASSWORD=' komodo/.env | cut -d= -f2-)" \
  -e KOMODO_DATABASE_DB_NAME=komodo \
  ghcr.io/moghtech/komodo-cli km database backup -y
ls -lh /etc/komodo/backups
```

Expected: a dated backup directory or archive.

- [ ] **Step 2: Restore into a throwaway Mongo and confirm the Stack is in it**

Run (on host):

```bash
docker run -d --name komodo-restore-test --network komodo \
  -e MONGO_INITDB_ROOT_USERNAME=test -e MONGO_INITDB_ROOT_PASSWORD=test mongo:8
sleep 10
docker run --rm --network komodo -v /etc/komodo/backups:/backups \
  -e KOMODO_DATABASE_ADDRESS=komodo-mongo:27017 \
  -e KOMODO_DATABASE_USERNAME="$(grep '^KOMODO_DATABASE_USERNAME=' komodo/.env | cut -d= -f2-)" \
  -e KOMODO_DATABASE_PASSWORD="$(grep '^KOMODO_DATABASE_PASSWORD=' komodo/.env | cut -d= -f2-)" \
  -e KOMODO_CLI_DATABASE_TARGET_URI=mongodb://test:test@komodo-restore-test:27017 \
  -e KOMODO_CLI_DATABASE_TARGET_DB_NAME=komodo \
  ghcr.io/moghtech/komodo-cli km database copy -y
docker exec komodo-restore-test mongosh -u test -p test --quiet \
  --eval 'db.getSiblingDB("komodo").Stack.find({},{name:1,_id:0}).toArray()'
```

Expected: output containing `docker-registry`. That is the criterion — the backup reproduces the resource, not merely that a file exists.

- [ ] **Step 3: Tear down the restore test**

```bash
docker rm -f komodo-restore-test
```

- [ ] **Step 4: Record the resource baseline**

Run (on host):

```bash
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' \
  komodo-mongo komodo-core komodo-periphery
```

Note the total memory. This is the number the "resource cost is tolerable" criterion is judged against at the decision date.

- [ ] **Step 5: Document it in the README**

Add a row to the Stacks table in `README.md`:

```markdown
| [komodo/](komodo/) | Komodo Core + Periphery + MongoDB | Second control plane, under evaluation against Portainer until 2026-08-20. Host-managed, never CI-deployed. Owns the `docker-registry` stack, which it deploys from this repo on a GitHub webhook |
```

And amend the `registry/` row's "Why" to note it is deployed by Komodo rather than CI.

`komodo/` is deliberately **not** added to `scripts/bootstrap.sh` — a rebuild-from-scratch should not depend on a stack that may be deleted at the decision date.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "document the komodo evaluation"
git push
```

---

## Verdict checkpoint — 2026-08-20

Not an implementation task. On the decision date, check the four criteria from the spec and pick one of three outcomes: migrate the remaining four stacks, tear Komodo down via the rollback below, or extend once with a written reason.

| Criterion | Where it was measured |
|---|---|
| Push deploys with no CI work | Task 7 Step 5 |
| Rollback is fast from the UI | Not yet exercised — redeploy an older commit from Komodo's Stack page and time it |
| Backups actually restore | Task 8 Step 2 |
| Resource cost is tolerable | Task 8 Step 4 |

## Rollback

If the answer is no, from any point:

```bash
# 1. Restore registry to CI — reverts Task 5
git revert <task-5-commit>

# 2. Recreate the stack in Portainer, named exactly docker-registry,
#    from the unchanged registry/docker-compose.yml. Or just run the
#    Deploy workflow with workflow_dispatch once registry is back in the matrix.

# 3. Remove Komodo (on host)
docker compose --project-directory komodo down
```

Then delete the GitHub webhook, the `komodo.sussman.win` tunnel hostname, and the Caddy block.

The volume reattaches either way, because both control planes use project name `docker-registry`. That one pinned value is what makes the whole experiment reversible.
