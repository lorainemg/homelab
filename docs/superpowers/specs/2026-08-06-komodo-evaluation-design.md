# Komodo evaluation — design

**Date:** 2026-08-06
**Status:** approved, ready to implement

Stand up [Komodo](https://komo.do) v2.3.1 alongside Portainer as a second,
non-load-bearing control plane, migrate exactly one stack (`docker-registry`)
to it, and decide on a fixed date whether to migrate the rest or tear it down.

This is an evaluation with a deliberate exit, not a migration. Portainer keeps
managing `config`, `monitoring`, `immich` and `home-assistant` throughout, and
is the rollback target.

## Concepts

Written out because most of this design turns on details that are invisible
until they bite.

**Control plane.** The thing that starts, stops and updates containers, as
opposed to the containers doing the actual work. Portainer is this repo's
control plane. It matters that it can be replaced without the workloads
noticing — `docker compose` is the real interface, and both Portainer and
Komodo are just wrappers that call it.

**Why Portainer is load-bearing here.** It is not merely a UI. The last step of
every deploy — [deploy.yml](../../../.github/workflows/deploy.yml) — calls
Portainer's HTTP API to push a compose file and its environment. Replacing
Portainer means replacing that API call. The UI is the part being evaluated for
taste; the API is infrastructure.

**Komodo Core and Periphery.** Komodo splits into two programs. *Core* serves
the web UI and API and owns the database. *Periphery* is a small stateless
agent that runs on each managed server, holds the Docker socket, and executes
what Core tells it to. Core never touches Docker itself. This split exists so
Komodo can manage many servers over the network — but it applies even when
there is one server and both run on it, so a single-node install still needs
both. That is the main reason Komodo costs three containers where Portainer
costs one.

**Compose project name.** When `docker compose` brings up a stack it labels
everything with a *project name*, and prefixes the names of any volumes and
networks declared inside the file. A file declaring `volumes: registry-data:`
deployed under project `docker-registry` produces a real volume called
`docker-registry_registry-data`. Change the project name and compose no longer
recognises the old volume — it silently creates an empty new one and the
service starts perfectly healthy with no data. This is the single largest
hazard in this design and the reason for the `project_name` pin below.

**GitOps.** Instead of CI pushing files *into* the control plane, the control
plane reads the git repo itself and reconciles. The repo becomes the source of
truth rather than a source that gets copied. Komodo supports this natively for
stacks; Portainer CE only approximates it by polling. This capability is the
main reason the switch is worth evaluating at all.

**Webhook, and why it is needed.** Komodo does *not* poll git. Its
`poll_for_updates` and `auto_update` settings watch container registries for
newer image digests and never pull the repo. So something must tell Komodo a
commit happened: GitHub posts to a URL of the form
`/listener/github/stack/<name>/deploy` on every push, and Komodo responds by
pulling the repo and running compose.

**HMAC webhook signature.** The webhook URL must be reachable without a login,
since GitHub cannot log in. It is authenticated instead by a shared secret:
GitHub signs each request body with it and sends the signature in an
`X-Hub-Signature-256` header, which Komodo verifies against its own copy. A
request with a wrong or missing signature is rejected. Knowing the URL is not
enough to trigger anything.

**Mongo wire protocol.** Komodo Core stores state in a database that speaks
MongoDB's protocol — either MongoDB itself, or FerretDB, which speaks Mongo on
the front and stores in Postgres on the back. There is no SQLite option.
Portainer, by contrast, embeds BoltDB inside its own data volume with no
separate server, which is why its backup story is "copy one volume."

## Decisions

| Question | Decision | Rationale |
|---|---|---|
| Scope | One stack: `docker-registry` | One container, and its volume holds a cache of self-built images that can be re-pushed. Every other live stack is either load-bearing (`config` owns Caddy) or holds data worth grieving |
| Placement | New host-managed `komodo/`, never CI-deployed | Same tier as [portainer/](../../../portainer/docker-compose.yml). Matches the existing rule that a deploy must not be able to break the thing performing the deploy. Bailing out is `docker compose down` plus deleting a directory |
| Deploy mechanism | Git-repo stack — Komodo clones this repo and reads `registry/docker-compose.yml` itself | Tests the capability that actually distinguishes Komodo. Compose files stop being pushed as payloads |
| Trigger | GitHub webhook straight to Komodo | `registry` leaves CI entirely — no deploy step, no API call, no new Actions secrets. The strongest possible test of the GitOps claim |
| Authentication | Komodo's own login, plus the webhook HMAC secret | Parity with how `portainer.sussman.win` is exposed today. Additional edge authentication was considered and rejected: it is disproportionate for a two-week evaluation of a non-load-bearing stack, and it would disqualify most Komodo tooling |
| Database | MongoDB, pinned | Komodo's recommended and best-tested option. FerretDB adds a container and a translation layer to avoid a dependency this host can carry |
| Project name | Pinned to `docker-registry` | The live Portainer stack is named `docker-registry`, not `registry` — see [deploy.yml:160](../../../.github/workflows/deploy.yml#L160). The volume on disk is `docker-registry_registry-data` |
| Portainer | Stays running and untouched | It manages four live stacks, and it is the rollback target |
| Decision date | 14 days after the canary deploys | Without a date, "evaluating" becomes "permanently running two control planes" |

## Architecture

Komodo joins the existing edge path with no new infrastructure beyond its own
containers:

```
git push
   ↓
GitHub  ──webhook, HMAC-signed──> https://komodo.sussman.win/listener/...
   ↓
Cloudflare edge          TLS terminates
   ↓  outbound-only tunnel (no router ports open)
cloudflared              portainer stack, host-managed
   ↓  http://caddy:80    Docker DNS over `internal`
config-caddy
   ↓  http://komodo-core:9120
komodo-core  ──→  mongo            (state: resources, users, audit log)
      ↓  websocket
komodo-periphery ──→ /var/run/docker.sock
      ↓  git clone https://github.com/lorainemg/homelab
      ↓  docker compose -p docker-registry -f registry/docker-compose.yml up -d
   registry  ──→  docker-registry_registry-data
```

The two control planes are peers. Neither is on the other's critical path:
both are reached through a tunnel that neither owns, and both are started by
hand from the host.

GitHub Actions does not appear in this path at all — that is the point.

## Components

### `komodo/docker-compose.yml` (new, host-managed)

Three services, all pinned. Modelled on
[portainer/docker-compose.yml](../../../portainer/docker-compose.yml) — brought
up with `docker compose --project-directory komodo up -d`, never by CI.

- **`mongo`** — image pinned to a `mongo:8` patch release. Carries the
  `komodo.skip` label, which excludes it from Komodo's own *Stop All
  Containers* action. Without the label, one misclick in the UI stops the
  database out from under Core. Data in a named volume.
- **`komodo-core`** — `ghcr.io/moghtech/komodo-core:2.3.1`. Joins the external
  `internal` network so Caddy can resolve it by name. Holds
  `KOMODO_WEBHOOK_SECRET`. Registration disabled by configuration once the
  first account exists.
- **`komodo-periphery`** — `ghcr.io/moghtech/komodo-periphery:2.3.1`. Mounts
  `/var/run/docker.sock`, plus a named volume for its git clones. That volume
  is where this repo lands and where `docker compose` actually runs, so it must
  survive container recreation.

Secrets live in a gitignored `komodo/.env` on the host, with a committed
`komodo/.env.example` template — the same split
[portainer/.env.example](../../../portainer/.env.example) uses. Nothing here
flows through GitHub Actions, because this stack is never deployed by CI.

This is a deliberate exception to the repo's "secrets are GitHub Actions
secrets" convention, and it is the same exception `portainer/` already makes:
host-managed stacks are brought up by hand, so their secrets live on the host.

### `config/Caddyfile` — one new block

```
http://komodo.sussman.win {
    reverse_proxy komodo-core:9120
}
```

Safe to add mid-flight: a Caddyfile change rebuilds the `config-agent` image
and deploys the `config` stack, but the agent copies the file in and Caddy
hot-reloads it under `--watch`. The caddy container is never recreated, so the
tunnel → caddy → Portainer path carrying the deploy's own API response
survives.

A `komodo.sussman.win` public hostname is added to the tunnel, pointed at
`http://caddy:80` like every other service.

### The Komodo Stack resource

Created once in Komodo's UI, configured as:

| Field | Value |
|---|---|
| Name | `docker-registry` |
| `project_name` | `docker-registry` — set explicitly, even though the name alone would produce it |
| `repo` | `lorainemg/homelab` |
| `branch` | `main` |
| `file_paths` | `["registry/docker-compose.yml"]` |
| `run_directory` | repo root |
| `environment` | empty — `registry` has no secrets, per [deploy.yml:151-152](../../../.github/workflows/deploy.yml#L151-L152) |
| `auto_update` | `false` — image updates stay deliberate |

`project_name` is set redundantly on purpose. It is the one field whose
omission destroys data, and an explicit value documents the constraint for
whoever migrates the next stack.

### The GitHub webhook

Configured in this repo's *Settings → Webhooks*:

| Field | Value |
|---|---|
| Payload URL | `https://komodo.sussman.win/listener/github/stack/docker-registry/deploy` |
| Content type | `application/json` |
| Secret | the same value as `KOMODO_WEBHOOK_SECRET` in `komodo/.env` |
| Events | push only |

The webhook fires on every push to any branch, but the Stack is pinned to
`main`, so a push elsewhere redeploys the same `main` content — wasteful but
harmless during a two-week evaluation. If it proves noisy, the fix is a
Komodo *Procedure* keyed to the branch name rather than filtering in GitHub.

### `.github/workflows/deploy.yml` — `registry` removed, nothing added

`registry` leaves the Portainer deploy path entirely. No replacement step: the
webhook handles it.

Removing it takes two edits, not one. The matrix reads
`fromJSON('["config",...,"registry"]')` on `workflow_dispatch` and
`fromJSON(needs.changes.outputs.stacks)` otherwise — and that second, dynamic
list is produced by paths-filter and still contains `registry`. Deleting the
name from the hardcoded list alone would leave every push-triggered run still
deploying `registry` to Portainer, racing Komodo for the same compose project.

So:

- Drop `registry` from the `workflow_dispatch` matrix list.
- Have the `changes` job emit a second output — `stacks` minus `registry` —
  and point the deploy matrix at it.
- Delete the `registry`/`docker-registry` name rewrite at
  [deploy.yml:160](../../../.github/workflows/deploy.yml#L160), which now has
  no remaining caller.

The `registry` paths-filter entry itself can also go, since nothing in the
workflow consumes it any more. Keeping or removing it is cosmetic; removing it
is cleaner, and restoring it is what rollback does.

No new Actions secrets. The workflow gets shorter, not longer — which is the
single clearest signal of whether Komodo is earning its keep.

## Cutover

Every step reversible, and ordered so no step depends on an unverified one.

1. **Snapshot the volume** to a tarball under the data root, and capture the
   current image catalog: `curl -s https://registry.sussman.win/v2/_catalog`.
   Both are the baseline for step 6.
2. **Remove `registry` from CI** as described above, and push. Portainer still
   holds the running stack; nothing redeploys it. Doing this *before* step 3
   prevents a later push recreating the stack in Portainer alongside Komodo's
   copy — two control planes fighting over one project.
3. **Delete the stack in Portainer.** Removes containers, leaves the volume.
4. **Verify the volume survived** — `docker volume ls` must still list
   `docker-registry_registry-data`. Hard gate: if it is gone, restore from
   step 1 before continuing.
5. **Create and deploy the Komodo stack**, then add the GitHub webhook.
   Compose reattaches to the existing volume because the project name matches.
6. **Verify the data, not the container.** `curl -s
   https://registry.sussman.win/v2/_catalog` must list the same repositories
   captured in step 1.

Step 6 is the acceptance test and cannot be substituted with a health check. A
registry backed by an empty volume starts healthy and serves
`{"repositories":[]}` — every dashboard green, all data gone. For any stateful
migration the test is a data assertion, never a status badge.

## Rollback

From any point, three moves:

1. Restore the `registry` entry to the Portainer matrix in `deploy.yml`.
2. Recreate the stack in Portainer from the unchanged
   [registry/docker-compose.yml](../../../registry/docker-compose.yml), named
   `docker-registry`.
3. `docker compose --project-directory komodo down` and delete the directory.

Then remove the Caddy block, the tunnel hostname, and the GitHub webhook.

The volume reattaches either way, because both control planes use project name
`docker-registry`. That one pinned value is what makes the entire experiment
reversible.

## Verdict

Decided on a fixed date 14 days after the canary deploys. Three outcomes are
possible — migrate the remaining four stacks, tear Komodo down, or extend once
with a written reason. "Keep both running indefinitely" is not an outcome.

Komodo passes if all four hold:

| Criterion | Test |
|---|---|
| Push deploys with no CI work | A push touching `registry/**` redeploys the stack with `deploy.yml` uninvolved |
| Rollback is fast from the UI | Redeploying a previous commit's compose from Komodo's UI takes under a minute, with no git revert and no CI run |
| Backups actually restore | `km database backup` restored into a fresh Mongo reproduces the stack and its configuration — recoverable, not merely present |
| Resource cost is tolerable | Steady-state memory for mongo + core + periphery stays within budget on a node already running ~25 containers including Ollama |

Plus the subjective one, which carries a veto in both directions: after two
weeks, whether you reach for Komodo's UI over Portainer's without thinking
about it.

The cost being weighed:

| | Portainer (today) | Komodo (canary) |
|---|---|---|
| Containers | 1 | 3 |
| State | one BoltDB volume | a MongoDB server |
| Backups | copy `portainer_data` | `km database backup` — first-party, but new |
| Git-native stacks | polling only | native |
| Beyond stacks | — | Resource Sync, Procedures, Actions, Alerters |

## Out of scope

- **Migrating the other four stacks.** Deliberately deferred to the verdict.
  Each carries the same `project_name` hazard, and `config` additionally holds
  Caddy and needs its own sequencing.
- **Resource Sync.** Declaring stacks as TOML in this repo is Komodo's most
  interesting capability, but learning it at the same time as the deploy model
  confounds the evaluation. It is the obvious follow-up if the verdict is yes.
- **Relocating secrets into Komodo.** The canary was chosen precisely because
  it has none. Where secrets live is the hard part of a full migration and
  belongs to that design.
- **Hardening Komodo's public exposure.** Deferred with the rest of the
  migration. It becomes worth revisiting if and when Komodo takes over stacks
  that matter; during the evaluation it is parity with Portainer, and parity is
  the correct bar for a thing that might be deleted in two weeks.
- **Retiring Portainer.** Not on the table during the evaluation.
