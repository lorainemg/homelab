# Portainer → Komodo migration — design

**Date:** 2026-08-20
**Status:** draft, pending review

Move the four remaining stacks — `config`, `monitoring`, `immich`,
`home-assistant` — from Portainer to Komodo, delete the four config-baked
images, relocate the Cloudflare tunnel out of the Portainer stack, and retire
Portainer.

This is the follow-up the [2026-08-06 Komodo evaluation](2026-08-06-komodo-evaluation-design.md)
deferred to its verdict date. That evaluation migrated one stack
(`docker-registry`) and left the other four deliberately untouched. Its four
criteria are reviewed in *Verdict* below; this design is what follows from
answering them yes.

## Concepts

Written out because most of the decisions below turn on details that are
invisible until they bite. Every claim marked *verified* was tested, not
reasoned about.

**Push versus pull.** Portainer is pushed to: GitHub Actions checks out the
repo on a rented runner, reads a compose file, and hands it plus an
environment to Portainer's HTTP API ([deploy.yml:154-162](../../../.github/workflows/deploy.yml#L154-L162)).
Portainer has never seen the repo — only what CI mailed it. Komodo pulls: a
Stack resource records a repo, branch and file path, and Komodo clones and
runs compose itself. The compose file stops being a payload and becomes the
source of truth.

**The build/deploy race.** A pull-based control plane deploys; it does not
build. Today one CI job builds an image and *then*, as a later step in the
same job, calls Portainer — the ordering is free. Split those across two
systems and it disappears: a push fires the Actions build and the Komodo
webhook simultaneously, so Komodo can deploy while the image is still
building, pull the previous `:latest` digest, and report success. Because
`:latest` is mutable there is no version mismatch to notice; a stale deploy
is indistinguishable from a correct one. The canary never hit this because
`registry:2` is an upstream image with nothing to build.

**Config from the checkout.** Komodo's Periphery clones into an ordinary host
directory — `/etc/komodo/stacks/<stack-name>/` (*verified*: the live registry
stack runs from `/etc/komodo/stacks/docker-registry/registry/docker-compose.yml`)
— and runs compose there. Because [komodo/docker-compose.yml:63](../../../komodo/docker-compose.yml#L63)
binds `/etc/komodo` to the identical path inside the container, that directory
means the same thing to Periphery and to the Docker daemon. So a compose file
Komodo deploys can bind-mount its own config straight out of the checkout,
and a thin `FROM upstream` + `COPY config` image stops being necessary.

**Bind paths resolve against the compose file's directory.** Not against the
working directory. *Verified*: `docker compose -f stack/docker-compose.yml`
(Komodo's form) and `docker compose --project-directory stack` ([bootstrap.sh:32](../../../scripts/bootstrap.sh#L32))
produce identical absolute sources, including for parent-relative paths like
`../home-assistant/ha-config`. This is what keeps `git clone && ./scripts/bootstrap.sh`
working on a fresh machine with no Komodo at all.

**Inode pinning — mount directories, never files.** A bind mount attaches the
container to a file's identity on disk, not to its path. Git does not edit in
place: it writes a new file and renames it over the old one, producing a new
inode. *Verified*, against a checkout put through Komodo's exact git sequence:

```
checkout  : version: ONE | inode 23638
after pull: version: TWO | inode 23688
file-mount sees: version: ONE
dir-mount  sees: version: TWO
```

A single-file mount goes stale and stays stale until the container is
recreated — which for Caddy is exactly what this repo's config design exists
to avoid. Every config mount in this design is therefore a directory.

**Untracked files survive a pull.** *Verified*: an untracked `.env` placed in
the checkout survives `git checkout -f main` followed by
`git pull --rebase --force origin main`, and compose auto-loads it from the
stack's own directory. It does **not** survive a re-clone (new machine,
repointed repo, deleted and recreated stack), which makes placing it a
documented setup step rather than a one-time accident.

**Blast radius and the recovery path.** The tunnel is the only way into this
network from outside, and Komodo's own UI sits behind it. Anything deployed
automatically can be broken by a deploy; if the tunnel is inside such a stack,
a bad deploy can remove the path you would use to repair it, leaving physical
access as the only recourse. The tunnel therefore lives where no deploy can
reach it.

## Decisions

| Question | Decision | Rationale |
|---|---|---|
| Config delivery | Bind-mount config directories out of Komodo's checkout | Deletes four images and the race along with them. Also fixes a portability gap: today `tempo.yml` in a fresh clone is inert, because the copy that runs lives in a CI-built image |
| Mount granularity | Directories only, never single files | Inode pinning. A single-file mount is silently frozen after the first pull |
| Secrets | An untracked `.env` per stack, placed in Komodo's checkout | One mechanism serves both Komodo and plain `docker compose`, so the fresh-machine story stays a single list of files. Keeps secrets off a page served at `komodo.sussman.win`, which has only Komodo's own login in front of it |
| Builds | Keep exactly one image: `config-agent`. Delete the `prometheus`, `promtail`, `tempo` and `otelcol` Dockerfiles | Images are for software, mounts are for config. `config-agent` is the only remaining case that ships a program rather than a file |
| Sequencing | CI triggers Komodo for `config` only, after the build step | The race applies solely to the one stack that still has an image to build. The other three deploy from the webhook with CI uninvolved |
| Caddyfile | Moves to `config/caddy/Caddyfile`, mounted read-only into Caddy | `--watch` reloads on every pull, so the Caddyfile leaves both the image and the agent's sync list. Directory rule forces the new subdirectory |
| Tunnel | New host-managed `tunnel/` stack | The tunnel outlives control planes — this migration is the proof. Making it independent means the next one need not touch it |
| Portainer | Retires once the last stack has moved; it is the rollback target until then | Same posture the evaluation took, one level up |
| Project names | Pinned to `config`, `monitoring`, `immich`, `home-assistant` | The named-volume hazard. Unlike `docker-registry`, these already match their directory names — but they are pinned explicitly, because omission is what destroys data |
| Stack count | Unchanged at seven | `portainer/` becomes `tunnel/`. Retiring Portainer removes a container, not a directory |

## Verdict

The [evaluation spec](2026-08-06-komodo-evaluation-design.md#verdict) set four
criteria and a decision date of 2026-08-20. Their state on that date:

| Criterion | Status | Evidence |
|---|---|---|
| Push deploys with no CI work | **Partly shown** | The GitHub webhook is `active` with last response `200`, and Komodo correctly *declines* to act on pushes that do not touch the stack's files — ~14 pushes to `main` since 2026-08-07 produced zero deploys, which is `webhook_force_deploy: false` behaving as designed. What the update log cannot distinguish is whether the one real deploy was webhook-triggered or hand-run |
| Rollback is fast from the UI | **Not exercised** | The implementation plan flagged this as untested and it has stayed that way |
| Backups actually restore | **Passed** | Exercised 2026-08-20: the `2026-08-20_01-00-01` scheduled nightly restored into an emptied throwaway Mongo and reproduced both Stack resources *with their `project_name`*, plus the `Local` server. Backups have run unattended nightly since 2026-08-08 (`max_backups: 14`). Note that [Task 8 Step 2 of the evaluation plan](../plans/2026-08-06-komodo-evaluation.md) had to be corrected first: as written it ran `km database copy` from the *live* database and never opened a backup file |
| Resource cost is tolerable | **Passed** | Measured 2026-08-20: `mongod` 149.6 MB, `core` 63.6 MB, `periphery` 20.8 MB — 234 MB and ~2% CPU combined, against a host using 5.74 GB of 18.83 GB |

One remains open, and it is the cheaper of the two:

- **Rollback from the UI** — redeploy an older commit's compose from Komodo's
  Stack page and time it. Worth closing before `immich` moves, since it is the
  procedure you would reach for at exactly the wrong moment.

The **push-to-deploy** criterion can be finished at the same time: a whitespace
commit touching `registry/docker-compose.yml` should produce a Komodo deploy
with no Actions run. Neither blocks starting the migration at `monitoring`.

## Architecture

```
git push
   ↓
GitHub ─┬─ webhook, HMAC-signed ──> https://komodo.sussman.win/listener/...
        │                              (config, monitoring, immich,
        │                               home-assistant, docker-registry)
        └─ Actions: build config-agent ──> GHCR ──> then trigger Komodo
                                                     (config only)
   ↓
Cloudflare edge                TLS terminates
   ↓  outbound-only tunnel
cloudflared                    tunnel/ — host-managed, never deployed
   ↓  http://caddy:80
config-caddy                   ./caddy mounted ro from the checkout, --watch
   ↓  http://komodo-core:9120
komodo-core ──→ mongo
      ↓  websocket
komodo-periphery ──→ /var/run/docker.sock
      ↓  git pull into /etc/komodo/stacks/<stack>/
      ↓  docker compose -p <stack> -f <stack>/docker-compose.yml up -d
   the stack, reading its config from the checkout beside it
```

GitHub Actions appears once, for one stack, to build one image.

## Components

### The four config-baked images, deleted

`monitoring/{promtail,tempo,otelcol}/Dockerfile` are pure `FROM upstream` +
`COPY config`. Each becomes an upstream image plus a directory mount:

```yaml
image: grafana/tempo:2.4.2
volumes:
  - ./tempo:/etc/tempo:ro
```

Once the Dockerfile is gone, `monitoring/tempo/` holds nothing but
`tempo.yml`, so mounting the directory hides nothing the image wanted.

`monitoring/prometheus/Dockerfile` is not a pure config copy — it also
materializes the Home Assistant scrape token at startup. Its three jobs map
onto compose keys one for one:

```yaml
image: prom/prometheus:latest
entrypoint: ["/bin/sh", "/etc/prometheus/docker-entrypoint.sh"]
volumes:
  - ./prometheus:/etc/prometheus:ro
tmpfs:
  - /run/prometheus:uid=65534,gid=65534,mode=0700
```

Three details that are not optional:

- The token is written to a tmpfs so it never dirties the checkout, where the
  next `git pull --force` would fight it. tmpfs rather than a named volume
  because the entrypoint runs as `nobody` (uid 65534) and *a fresh named
  volume comes up empty and root-owned* — the write fails with `EACCES`. The
  data suits tmpfs anyway: `HA_TOKEN` is rewritten on every start, so the
  token never needs to persist and never reaches disk. All verified against
  `prom/prometheus:latest`.
- The tmpfs is mounted **outside** `/etc/prometheus`, not inside it. Docker
  cannot create a mountpoint inside a read-only bind mount (*verified*: the
  container dies with `Read-only file system`), and the placeholder directory
  that would fix it cannot be committed — `.gitignore` carries a blanket
  `**/secrets/`. Putting the tmpfs at `/run/prometheus` removes the problem
  rather than working around it. `prometheus.yml`'s `credentials_file` and
  `docker-entrypoint.sh` both point there.
- `docker-entrypoint.sh` is mode `644` in git — the deleted Dockerfile used
  `COPY --chmod=755` — which is why the entrypoint invokes it through
  `/bin/sh` rather than executing it directly.

### Making a config edit actually take effect

Bind-mounting config out of the image is only a third of the job. *Verified on
`monitoring`, 2026-08-23*: "deploy" is three independent things, and two of them
are off by default.

1. **Something has to notice the commit.** Komodo's webhook runs
   `DeployStackIfChanged` unless `webhook_force_deploy` is set, and that compares
   `deployed_contents` against `remote_contents` — the *files named in
   `file_paths`*, i.e. the compose file alone. A change to `prometheus.yml`
   updates `latest_hash` and deploys nothing, with no error. Config-mounting
   stacks therefore need `webhook_force_deploy: true`, at the price of a redeploy
   on every push to the branch.
2. **The file has to arrive.** This part is free — the bind mount is live, and
   Komodo's `git pull` updates the checkout in place.
3. **The process has to re-read it.** `docker compose up` recreates a container
   only when its service definition changes, compared via the
   `com.docker.compose.config-hash` label. A bind-mounted file is not part of that
   definition, so compose correctly leaves the container running with its old
   config in memory. A `post_deploy` command restarting the affected services
   closes the gap, and keeps the whole sequence inside Komodo's own deploy rather
   than adding a second actor.

Prometheus's `--web.enable-lifecycle` + `POST /-/reload` was considered for (3)
and rejected: the same flag exposes `/-/quit` on the published port 9090, letting
anything on the LAN stop monitoring.

### `config/` — the one surviving build

`config-agent` stays a built image, because it ships a program: alpine, git,
curl and `agent.sh`. What leaves the image is the *config* it carries. Its
`/src` becomes mounts from the checkout:

```yaml
volumes:
  - ../home-assistant/ha-config:/src/ha-config:ro
  - ../home-assistant/mosquitto:/src/mosquitto:ro
```

The agent keeps its file-by-file sync into the live directories — it cannot be
replaced with a mount, because those directories also hold runtime state
(`.storage/`, `custom_components/`, the HA database, mosquitto's generated
`passwd`) that a directory mount would shadow. It also keeps its hourly
push-back of UI-made HA edits, from its own clone at `/work/repo`, untouched.

The Caddyfile leaves the agent's job list entirely. `config/Caddyfile` moves to
`config/caddy/Caddyfile` and is mounted `./caddy:/etc/caddy:ro` on the Caddy
service, so a pull updates the file on disk and `--watch` reloads it in place —
no image, no agent, and no container recreation in the path.

### `tunnel/` — new, host-managed

`cloudflared` moves out of `portainer/docker-compose.yml` into its own
directory, brought up by hand like `komodo/`:

```
docker compose --project-directory tunnel up -d
```

Absent from every paths-filter and deploy matrix, and from Komodo's stacks. The
`CLOUDFLARE_TUNNEL_TOKEN` moves to `tunnel/.env` with a committed
`tunnel/.env.example`. `scripts/bootstrap.sh` swaps `portainer` for `tunnel` at
the head of its `STACKS` list.

### Secrets

Each stack that needs them gets an untracked `.env` in Komodo's checkout, at
`/etc/komodo/stacks/<stack>/<stack>/.env`:

| Stack | Keys |
|---|---|
| `config` | `HOMELAB_PUSH_TOKEN`, `HA_TOKEN` (plus the committed `config/deploy.env` values) |
| `monitoring` | `GRAFANA_ADMIN_PASSWORD`, `HA_TOKEN` |
| `immich` | `DB_PASSWORD` (plus committed `immich/deploy.env`) |
| `home-assistant` | none |

Compose auto-loads `.env` from the stack directory, so no compose file
references it. The committed `deploy.env` files fold into each
`.env.example`. The corresponding GitHub Actions secrets are deleted with the
deploy steps that consumed them, except `HOMELAB_PUSH_TOKEN`, which the
config-agent build still needs, and `GITHUB_TOKEN`, which is automatic.

### `.github/workflows/deploy.yml` — build-only

Everything except the `config-agent` build is deleted: the four image builds,
the `Assemble stack env` step, the Portainer deploy step, the stack matrix,
and the `PORTAINER_URL` / `PORTAINER_API_TOKEN` secrets. What remains is one
job that builds `config-agent` on changes to `config/config-agent/**`, pushes
it to GHCR, and then triggers Komodo's `config` listener — in that order, in
one job, which is what closes the race.

## Cutover

One stack at a time, in ascending order of what breaks if it goes wrong. Each
step is reversible and no step depends on an unverified one.

1. **Move the tunnel first, while Portainer still works.** Create `tunnel/`,
   bring it up, confirm every published hostname still answers, then remove
   `cloudflared` from `portainer/docker-compose.yml`. Doing this first means
   every later step has an intact recovery path.
2. **`monitoring`** — the least load-bearing of the four, and the one that
   exercises every new mechanism at once (three pure config mounts, the
   Prometheus entrypoint override, a `.env`). It is also the stack with the
   most named-volume exposure: all four of its data volumes are project-
   prefixed, so it is where the pinned project name earns its keep. Delete the
   stack in Portainer,
   confirm `monitoring_{grafana,loki,prometheus,tempo}_data` still exist,
   create the Komodo stack pinned to project name `monitoring`, place the
   `.env`, deploy.
   **Acceptance:** Grafana serves its existing dashboards and Prometheus
   returns a non-empty result for a query spanning before the cutover. Not a
   health check — a stack backed by an empty volume starts perfectly healthy.
3. **`immich`** — the one holding data worth grieving, though less exposed to
   the project-name hazard than it looks: its only named volume is
   `immich_model-cache`, a rebuildable ML cache. The library and the Postgres
   data are bind mounts (`UPLOAD_LOCATION=/data/immich/library`,
   `DB_DATA_LOCATION=/data/immich/postgres`), and bind paths carry no project
   prefix, so they reattach regardless of what compose calls the project.
   **Acceptance:** the existing photo library is browsable and the asset count
   matches the pre-cutover number.
4. **`home-assistant`** — declares no named volumes at all; every piece of
   state is bind-mounted from the data root, so the project-name hazard does
   not apply. The config-agent seam is what makes it the fiddliest.
   **Acceptance:** HA boots with its existing automations and the voice
   pipeline answers.
5. **`config` last**, because it owns Caddy and every other cutover is verified
   through it. Move the Caddyfile to `config/caddy/`, switch the agent's `/src`
   to checkout mounts, rewire CI to build-and-trigger.
   **Acceptance:** an edit to the Caddyfile reaches Caddy without the container
   being recreated, and an edit to `agent.sh` reaches the agent through the CI
   build followed by the Komodo trigger.
6. **Retire Portainer.** Stop the stack, keep `portainer_data` for one month,
   then delete it. Remove the `portainer.sussman.win` tunnel hostname and its
   Caddy block.

Steps 2–5 each carry a data assertion rather than a status check, for the
reason the evaluation spec gave: an empty volume produces a green dashboard.

## Rollback

Per stack, at any point, without touching the others:

1. Delete the Komodo stack (containers go, volumes stay).
2. Recreate it in Portainer under the identical project name, from the
   unchanged compose file, with its env restored from `.env`.
3. Restore that stack's entry to the matrix in `deploy.yml`.

The volume reattaches either way, because the project name is pinned on both
sides. If the tunnel move itself is the problem, `cloudflared` returns to
`portainer/docker-compose.yml` and comes back up from the host.

Whole-migration rollback is the same three moves times four, plus reverting the
Dockerfile deletions. Nothing here is one-way until step 6 deletes
`portainer_data`, which is why that step waits a month.

## Out of scope

- **Resource Sync.** Declaring these stacks as TOML in this repo is the obvious
  next step, and deliberately not this one: learning it while also moving four
  stacks confounds both.
- **Hardening Komodo's public exposure.** The evaluation deferred this on the
  grounds that the canary held no secrets. That reasoning has now expired — but
  the decision to keep secrets in host `.env` files rather than in Komodo's UI
  keeps the exposure at parity with Portainer's, so it stays a separate piece
  of work rather than a blocker.
- **Pinning `cloudflared`.** It runs `:latest` today; moving the container is
  not the moment to also change what it runs.
- **The `caddy_caddy_data` / `caddy_caddy_config` leftovers** from the old
  pre-rename `caddy` stack, still on the host. Unrelated cleanup.
- **`registry`.** Already on Komodo, already git-backed, unchanged by this.
