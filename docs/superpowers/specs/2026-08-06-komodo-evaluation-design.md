# Komodo evaluation — design

**Date:** 2026-08-06
**Status:** approved, ready to implement

Stand up [Komodo](https://komo.do) v2.3.1 alongside Portainer as a second,
non-load-bearing control plane, migrate exactly one stack (`docker-registry`)
to it, and decide on a fixed date whether to migrate the rest or tear it down.

This is an evaluation with a deliberate exit, not a migration. Portainer keeps
managing `config`, `monitoring`, `immich` and `home-assistant` throughout.

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

**Mongo wire protocol / FerretDB.** Komodo Core stores state in a database that
speaks MongoDB's protocol. That is either MongoDB itself, or FerretDB — a
translation layer that speaks Mongo on the front and stores in Postgres on the
back. There is no SQLite option. Portainer, by contrast, embeds BoltDB inside
its own data volume with no separate server, which is why its backup story is
"copy one volume."

**Cloudflare Access.** A gate at Cloudflare's edge that runs *before* a request
reaches the tunnel. Unauthenticated requests never arrive at the origin at all.
Normally it authenticates a human via an interactive browser login.

**Access service token.** A non-interactive credential — a client ID and secret
sent as HTTP headers — for machines that cannot complete a browser login. A
GitHub Actions runner can present one; a GitHub *webhook* cannot, because
GitHub's webhook sender allows configuring only a payload URL and a shared
secret, not arbitrary headers. That single limitation is what decided this
design's trigger mechanism.

**HMAC webhook signature.** The alternative Komodo offers: GitHub signs each
webhook body with a shared secret and sends the signature in an
`X-Hub-Signature-256` header, which Komodo verifies. It is sound, but it
requires the webhook path to be publicly reachable without Access. This design
rejects that in favour of the service token.

## Decisions

| Question | Decision | Rationale |
|---|---|---|
| Scope | One stack: `docker-registry` | One container, and its volume holds a cache of self-built images that can be re-pushed. Every other live stack is either load-bearing (`config` owns Caddy) or holds data worth grieving |
| Placement | New host-managed `komodo/`, never CI-deployed | Same tier as `portainer/`. Matches the existing rule that a deploy must not be able to break the thing performing the deploy. Bailing out is `docker compose down` plus deleting a directory |
| Deploy mechanism | Git-repo stack — Komodo clones this repo and reads `registry/docker-compose.yml` itself | Tests the capability that actually distinguishes Komodo. Compose files stop being pushed as payloads |
| Trigger | CI calls Komodo's API with an Access service token | Keeps Access covering the entire hostname with no unauthenticated paths. A webhook would require exempting `/listener/*` |
| Deploy tooling | Raw `curl` in the workflow, not a marketplace action | The one published Komodo action cannot send custom headers, so it cannot pass Access. See [Why a raw `curl`](#why-a-raw-curl-and-not-a-marketplace-action) |
| Access boundary | Cloudflare Access over all of `komodo.sussman.win`, no bypass rules | Two independent locks. Strictly better than `portainer.sussman.win`, which today has only Portainer's own login |
| Database | MongoDB, pinned | Komodo's recommended and best-tested option. FerretDB adds a second container and a translation layer to avoid a dependency this host can carry |
| Project name | Pinned to `docker-registry` | The live Portainer stack is named `docker-registry`, not `registry` — see [deploy.yml:168](../../../.github/workflows/deploy.yml#L168). The volume on disk is `docker-registry_registry-data` |
| Portainer | Stays running and untouched | It manages four live stacks. It is also the rollback target |
| Decision date | 14 days after the canary deploys | Without a date, "evaluating" becomes "permanently running two control planes" |

## Architecture

Komodo joins the existing edge path with no new infrastructure beyond its own
containers:

```
GitHub Actions
   ↓  https://komodo.sussman.win/execute/DeployStack
Cloudflare edge          TLS terminates · Access: service token OR your identity
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
  `internal` network so Caddy can resolve it by name. **No published host
  port**, matching the [vaultwarden](../../../vaultwarden/docker-compose.yml)
  precedent: nothing on the LAN may bypass the proxy and the Access policy in
  front of it. Registration disabled by configuration once the first account
  exists.
- **`komodo-periphery`** — `ghcr.io/moghtech/komodo-periphery:2.3.1`. Mounts
  `/var/run/docker.sock`, plus a named volume for its git clones. That volume
  is where this repo lands and where `docker compose` actually runs, so it must
  survive container recreation.

Secrets live in a gitignored `komodo/.env` on the host, with a committed
`komodo/.env.example` template — the same split
[portainer/](../../../portainer/.env.example) uses. Nothing here flows through
GitHub Actions, because this stack is never deployed by CI.

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
| `environment` | empty — `registry` has no secrets, per [deploy.yml:159-161](../../../.github/workflows/deploy.yml#L159-L161) |
| `auto_update` | `false` — image updates stay deliberate, matching the reasoning in [vaultwarden/docker-compose.yml](../../../vaultwarden/docker-compose.yml) |

`project_name` is set redundantly on purpose. It is the one field whose
omission destroys data, and an explicit value documents the constraint for
whoever migrates the next stack.

### `.github/workflows/deploy.yml` — one job added, one stack rerouted

`registry` leaves the Portainer matrix and gains its own job. Not a conditional
inside the existing matrix job: keeping that job's *steps* unchanged means the
four live Portainer stacks carry zero risk from this experiment, and rollback
is deleting one job.

Removing it from the matrix takes two edits, not one. The matrix reads
`fromJSON('["config",...,"registry"]')` on `workflow_dispatch` and
`fromJSON(needs.changes.outputs.stacks)` otherwise — and that second, dynamic
list is produced by paths-filter and still contains `registry`. Deleting the
name from the hardcoded list alone would leave every push-triggered run still
deploying `registry` to Portainer, racing Komodo for the same compose project.

So the `changes` job gains a second output, and the deploy matrix consumes it:

- `stacks` — unchanged, still the raw paths-filter result. The new job reads
  this to decide whether `registry` changed.
- `portainer_stacks` — `stacks` with `registry` filtered out. The existing
  deploy matrix reads this instead, and its `workflow_dispatch` list drops
  `registry` too.

The `registry` paths-filter entry itself stays exactly as it is. Only its
destination changes.

```yaml
  deploy-komodo:
    needs: changes
    if: >-
      github.event_name == 'workflow_dispatch' ||
      contains(fromJSON(needs.changes.outputs.stacks), 'registry')
    runs-on: ubuntu-latest
    env:
      KOMODO_URL: ${{ secrets.KOMODO_URL }}
      KOMODO_API_KEY: ${{ secrets.KOMODO_API_KEY }}
      KOMODO_API_SECRET: ${{ secrets.KOMODO_API_SECRET }}
      CF_ID: ${{ secrets.KOMODO_CF_ACCESS_CLIENT_ID }}
      CF_SECRET: ${{ secrets.KOMODO_CF_ACCESS_CLIENT_SECRET }}
    steps:
      - name: Deploy docker-registry through Komodo
        run: |
          curl --fail-with-body -sS -X POST "$KOMODO_URL/execute/DeployStack" \
            -H 'Content-Type: application/json' \
            -H "X-Api-Key: $KOMODO_API_KEY" -H "X-Api-Secret: $KOMODO_API_SECRET" \
            -H "CF-Access-Client-Id: $CF_ID" -H "CF-Access-Client-Secret: $CF_SECRET" \
            -d '{"stack":"docker-registry"}' | tee response.json
          jq -e '.success == true' response.json
```

Secrets are bound through `env:` rather than interpolated into the `run:`
script, so they are never expanded into a shell command line.

#### Why a raw `curl` and not a marketplace action

A Komodo equivalent of
[cssnr/portainer-stack-deploy-action](https://github.com/cssnr/portainer-stack-deploy-action)
exists — [pandeptwidyaop/komodoactions](https://github.com/marketplace/actions/komodo-stack-deploy),
"Komodo Stack Deploy" — and it is genuinely tidier: about seven lines, with
`wait-for-completion` and a `status` output that would replace the `jq`
assertion above. It was evaluated and rejected for two reasons.

**It cannot send custom headers.** Its `action.yml` exposes `komodo-url`,
`api-key`, `api-secret`, `stack-name` and a few flags — and nothing for
arbitrary HTTP headers. With Access covering the whole hostname, the
`CF-Access-Client-Id` / `CF-Access-Client-Secret` pair is the only way through
the edge, so every run would be rejected by Cloudflare before reaching Komodo.

This is a general consequence of the Access decision, not a quirk of one
action: choosing "no unauthenticated paths" disqualifies any tool that cannot
inject headers. Under a webhook-plus-bypass design this action would work fine.
The verbosity of the `curl` is the recurring price of the security boundary.

**Its supply-chain profile is wrong for these credentials.** One star, no
forks, single maintainer, last pushed 2025-12-25; a composite action that runs
`npm ci` and executes Node at workflow time. It would receive both the Komodo
API key *and* the Access service token — collapsing into one dependency the two
independent locks that motivated this design.

If the verdict is to migrate the remaining stacks, factor this `curl` into a
local composite action under `.github/actions/` so each stack costs a few
lines. Not worth doing for a single stack.

No compose file and no environment are transmitted — only the instruction to
deploy. That is the whole point of the git-repo model.

The `jq` assertion is required, not defensive. Komodo's `/execute` returns an
**Update** object describing the outcome and returns HTTP 200 whether the
underlying `docker compose up` succeeded or failed. Checking only the status
code produces green CI runs for broken deploys.
`cssnr/portainer-stack-deploy-action` makes this decision internally; here it
is explicit and owned.

Two Access headers plus two Komodo headers are all required: the first pair
gets through Cloudflare, the second authenticates to Komodo.

### New GitHub Actions secrets

| Secret | Purpose |
|---|---|
| `KOMODO_URL` | `https://komodo.sussman.win` |
| `KOMODO_API_KEY` / `KOMODO_API_SECRET` | Komodo API credentials, created in its UI |
| `KOMODO_CF_ACCESS_CLIENT_ID` / `KOMODO_CF_ACCESS_CLIENT_SECRET` | Access service token |

### Cloudflare configuration (dashboard, not this repo)

Matching the Vaultwarden precedent of configuring edge policy outside the repo:

1. Access application over **all** of `komodo.sussman.win`. No bypass rules.
2. Policy — *Allow*, your identity. For the UI.
3. Policy — *Service Auth*, matching a new service token. For CI.
4. Tunnel public hostname `komodo.sussman.win` → `http://caddy:80`.

**Ordering constraint:** the Access application must exist before the tunnel
hostname resolves, and Komodo's registration must be closed before either.
Komodo's first-run state accepts new accounts; that window must not overlap
with public reachability.

## Cutover

Every step reversible, and ordered so no step depends on an unverified one.

1. **Snapshot the volume** to a tarball under the data root, and capture the
   current image catalog: `curl -s https://registry.sussman.win/v2/_catalog`.
   Both are the baseline for step 6.
2. **Remove `registry` from the Portainer matrix** in `deploy.yml` and push.
   Portainer still holds the running stack; nothing redeploys it. Doing this
   *before* step 3 prevents a later push recreating the stack in Portainer
   alongside Komodo's copy — two control planes fighting over one project.
3. **Delete the stack in Portainer.** Removes containers, leaves the volume.
4. **Verify the volume survived** — `docker volume ls` must still list
   `docker-registry_registry-data`. Hard gate: if it is gone, restore from
   step 1 before continuing.
5. **Create and deploy the Komodo stack.** Compose reattaches to the existing
   volume because the project name matches.
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

Then remove the Caddy block, the tunnel hostname, and the Access application.

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
| Push deploys with no CI work | A push touching `registry/**` redeploys the stack, and `deploy.yml` never needs to know what changed inside it |
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
| Auth | own login only | Access + own login |
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
- **Retiring Portainer.** Not on the table during the evaluation.
