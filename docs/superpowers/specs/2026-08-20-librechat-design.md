# LibreChat — design

**Date:** 2026-08-20
**Status:** approved, ready to implement

Run [LibreChat](https://github.com/danny-avila/LibreChat) v0.8.7 as the
homelab's chat front-end, published at `librechat.sussman.win`, deployed by
Komodo straight from this repo, and backed by an existing Microsoft Foundry
(Azure OpenAI) deployment.

This is the second stack Komodo owns, after `docker-registry`, and the first
one with secrets — which the
[Komodo evaluation](2026-08-06-komodo-evaluation-design.md) deliberately left
out of scope. It is therefore also the test of the one question that
evaluation could not answer.

## Concepts

Written out because most of what follows turns on details that are invisible
until they bite.

**Why this is a Komodo stack and not a CI stack.** Every application stack in
this repo today is pushed into Portainer by
[deploy.yml](../../../.github/workflows/deploy.yml). `registry` is the one
exception: Komodo clones this repo on a GitHub webhook and runs `docker
compose` itself. LibreChat follows `registry` because it needs a config file
(`librechat.yaml`) to sit next to its compose file, and a control plane that
already has the repo checked out can bind-mount that file directly. Under
Portainer the same file would have to be baked into an image and flowed
through `config-agent`, the way the Caddyfile and HA yaml are. Komodo's
git-native stacks make an entire delivery mechanism unnecessary — which is
precisely the capability the evaluation set out to test.

**Relative paths in a git-repo stack.** Because Komodo runs compose from its
own clone, every relative path in the compose file resolves inside that clone.
Upstream's compose leans on this heavily — it bind-mounts `./.env`,
`./data-node`, `./images`, `./uploads` and `./logs`. Docker does not error on a
missing bind source; it silently creates an empty directory. Pointed at a
control plane's working copy, that means databases written into a git clone
that gets re-pulled, and a `.env` that materialises as a *directory* where the
container expects a file. Every one of those binds is replaced below.

**Compose project name.** Same hazard the komodo evaluation documented: the
project name prefixes the real volume names, and changing it makes compose
silently create empty replacements rather than fail. Pinned with a top-level
`name:` key in the compose file itself, so it cannot drift with the Komodo
resource name, the directory name, or a hand-run `docker compose`.

**What `rag_api` is actually for here.** LibreChat routes uploaded files
through `parseText`
([packages/api/src/files/text.ts:53](https://github.com/danny-avila/LibreChat/blob/v0.8.7/packages/api/src/files/text.ts#L53)).
With no `RAG_API_URL` set it falls back to `parseTextNative`, which is a plain
`readFileAsString`. A `.docx` is a zip archive, so that fallback hands the
model binary garbage — and because `.docx`, `.pdf` and `.xlsx` are all in the
accepted-upload list
([file-config.ts:181](https://github.com/danny-avila/LibreChat/blob/v0.8.7/packages/data-provider/src/file-config.ts#L181)),
the failure is silent rather than an error the user can act on. The `rag_api`
container's `/text` endpoint is what parses Office and PDF formats. It is
included here as a **document parser**, not as retrieval: `RAG_USE_FULL_CONTEXT`
injects the whole parsed document into the prompt instead of embedding and
vector-searching it. That also means no embeddings provider and no embeddings
key.

**Why `vectordb` is still present.** `rag_api` initialises a vector store at
startup and will not boot without one, even when every request only ever hits
`/text`. The pgvector container is a dependency of the parser, not a feature
being used. It is the honest cost of Word and PDF support.

**Getting a generated file back is a different problem from reading one.**
Nothing in the parsing path can produce a document. LibreChat *can* serve files
back — `/api/files/download/:userId/:file_id` is a real authenticated route
([files.js:521](https://github.com/danny-avila/LibreChat/blob/v0.8.7/api/server/routes/files/files.js#L521))
— but a file only becomes downloadable once a record exists for it, and the
only tool wired to `createFile` is the built-in code interpreter
([Code/process.js:469](https://github.com/danny-avila/LibreChat/blob/v0.8.7/api/server/services/Files/Code/process.js#L469)).
MCP tool results never reach it: `formatToolContent` turns `image` content into
artifacts and `ui://` resources into widgets, and renders anything else —
including a `.docx` returned as a binary resource — as `Resource URI: … / MIME
Type: …` text
([mcp/parsers.ts:216](https://github.com/danny-avila/LibreChat/blob/v0.8.7/packages/api/src/mcp/parsers.ts#L216)).
So no MCP server can hand the user a file, however good it is at making one.
Document *authoring* is out of scope here and revisited at the end.

**Access token vs refresh cookie.** LibreChat sets exactly one httpOnly cookie,
`refreshToken`
([AuthService.js:673](https://github.com/danny-avila/LibreChat/blob/v0.8.7/api/server/services/AuthService.js#L673));
the access token lives in browser memory and travels as an `Authorization:
Bearer` header
([jwtStrategy.js:10](https://github.com/danny-avila/LibreChat/blob/v0.8.7/api/strategies/jwtStrategy.js#L10)).
A plain browser navigation to some *other* hostname therefore carries no
credential LibreChat's API would accept. This rules out reusing LibreChat's
login to protect any sidecar route — noted here because it is the first thing
anyone will try when they get to document delivery.

## Decisions

| Question | Decision | Rationale |
|---|---|---|
| Control plane | Komodo, git-repo stack on a GitHub webhook | Second stack after `docker-registry`. Lets `librechat.yaml` be bind-mounted from the clone instead of baked into an image and flowed through `config-agent` |
| Project name | Pinned `librechat` via top-level `name:` | The hazard the komodo spec called the largest in that design. Pinning in the file makes it independent of the Komodo resource name |
| Version | `ghcr.io/danny-avila/librechat:v0.8.7` | Upstream's compose runs `librechat-dev:latest`. This repo pins everything; `v0.8.7` is the newest published release tag |
| Provider | Microsoft Foundry (Azure OpenAI), via `librechat.yaml` | Already owned. No second bill, and the key stays server-side |
| Document reading | `rag_api` + `vectordb`, `RAG_USE_FULL_CONTEXT=true` | The only way `.docx`/`.pdf` attachments parse at all. Full-context mode keeps it a parser, not a retrieval system — no embeddings provider, no embeddings key |
| Document authoring | Out of scope | No MCP server can return a file (see Concepts). Revisited below with the three real options |
| Search | Meilisearch, included | Conversation search is core to the product; excluding it means `SEARCH=false` and a visibly degraded UI |
| Admin panel | Excluded | Upstream ships it in the same compose; it is a second published surface and a second session secret for a single-user install |
| Authentication | LibreChat's own login, registration off from first boot | Parity with `komodo.sussman.win` and `portainer.sussman.win`. Accounts are made with the bundled CLI, so there is never an open-signup window to race |
| Secrets | The Komodo Stack's own environment | This stack is not CI-deployed, so Actions secrets cannot reach it. Extends the existing host-managed exception to a Komodo-managed one |
| Host ports | None published | Caddy reaches the api over `internal`; the databases are not on `internal` at all |
| State | Named volumes | Matches `komodo/`'s own mongo. Bind mounts under the data root would need the `user:`/UID dance upstream's compose does |

## Architecture

LibreChat joins the existing edge path with no new infrastructure:

```
git push (librechat/**)
   ↓
GitHub  ──webhook, HMAC-signed──> https://komodo.sussman.win/listener/...
   ↓
komodo-periphery
   ↓  git clone https://github.com/lorainemg/homelab
   ↓  docker compose -p librechat -f librechat/docker-compose.yml up -d

browser
   ↓
Cloudflare edge          TLS terminates
   ↓  outbound-only tunnel (no router ports open)
cloudflared              portainer stack, host-managed
   ↓  http://caddy:80    Docker DNS over `internal`
config-caddy
   ↓  http://librechat:3080
librechat ──────────────────────────────→ Microsoft Foundry (outbound HTTPS)
   │
   ├── librechat-mongo         conversations, users        ─┐
   ├── librechat-meilisearch   conversation search          │ private `librechat`
   └── librechat-rag           /text document parsing       │ network only
          └── librechat-vectordb   pgvector (parser dep)    ─┘
```

Only the `api` container joins `internal`. Everything else sits on a private
`librechat` network, so nothing on the shared bridge can reach the database,
the search index or the parser — the same split
[komodo/docker-compose.yml](../../../komodo/docker-compose.yml) uses for its
own mongo.

## Components

### `librechat/docker-compose.yml` (new, Komodo-managed)

Derived from
[upstream's compose](https://github.com/danny-avila/LibreChat/blob/v0.8.7/docker-compose.yml)
with six deliberate departures, each forced by something above:

1. **Every tag pinned.** `librechat:v0.8.7`, `mongo:8.0.20`,
   `meilisearch:v1.35.1`, `pgvector:0.8.0-pg15-trixie`. The rag-api image
   publishes only `:latest`, so it is pinned **by digest**.
2. **No `./.env` bind.** Upstream mounts the env file into the container.
   `.env` is gitignored, so it does not exist in Komodo's clone and Docker
   would create a directory in its place. Variables are declared explicitly
   under `environment:` instead — the same reason
   [immich/docker-compose.yml](../../../immich/docker-compose.yml) does it.
3. **Named volumes instead of `./data-node`, `./images`, `./uploads`,
   `./logs`.** Same reason: relative binds would put live state inside a git
   working copy that gets re-pulled on every deploy.
4. **`librechat.yaml` bind-mounted read-only from the clone.** The one
   relative bind that is *correct* here, and the reason this is a Komodo
   stack.
5. **No published ports and no `user: ${UID}:${GID}`.** Nothing needs a host
   port, and with named volumes there is no host-ownership problem to solve.
6. **`admin-panel` dropped.**

`container_name`s are namespaced (`librechat`, `librechat-mongo`,
`librechat-rag`, …) rather than upstream's `chat-mongodb` / `vectordb` /
`rag_api`, because `internal` is a shared bridge and generic names collide
with whatever gets added next.

### `librechat/librechat.yaml` (new)

Holds the Foundry endpoint definition — `instanceName`, `deploymentName` and
`apiVersion` per model group, with the key referenced as `${AZURE_API_KEY}` so
the value stays in the environment. Also carries the `fileConfig` limits.

Bind-mounted read-only at `/app/librechat.yaml`. Editing it is a commit, the
webhook fires, the stack redeploys — which is the whole point of putting this
stack on Komodo.

### `librechat/.env.example` (new, committed; `.env` gitignored)

Template listing every variable, with generation commands for the four secrets
LibreChat requires (`CREDS_KEY`, `CREDS_IV`, `JWT_SECRET`, `JWT_REFRESH_SECRET`)
plus `MEILI_MASTER_KEY`, `POSTGRES_PASSWORD` and `AZURE_API_KEY`. Same split as
[komodo/.env.example](../../../komodo/.env.example) and
[portainer/.env.example](../../../portainer/.env.example).

The filled values are pasted into the Komodo Stack's *Environment* field, not
onto the host. Komodo writes them out next to the compose file at deploy time.
This is the first stack in the repo whose secrets live in a control plane
rather than in GitHub Actions or in a file on the host, and it is the thing
worth forming an opinion about while reviewing this.

### `config/Caddyfile` — one new block

```
http://librechat.sussman.win {
    reverse_proxy librechat:3080
}
```

Safe to add mid-flight for the reason the komodo spec already established: a
Caddyfile change rebuilds `config-agent` and deploys the `config` stack, but
the agent copies the file in and Caddy hot-reloads it under `--watch`. The
caddy container is never recreated.

A `librechat.sussman.win` public hostname is added to the tunnel, pointed at
`http://caddy:80` like every other service.

`TRUST_PROXY=1` is set on the api so LibreChat reads the client IP from the
`X-Forwarded-For` Caddy sets, rather than rate-limiting every user as one.

### The Komodo Stack resource

| Field | Value |
|---|---|
| Name | `librechat` |
| `project_name` | `librechat` — also pinned in the file |
| `repo` | `lorainemg/homelab` |
| `branch` | `main` |
| `run_directory` | `librechat` |
| `file_paths` | `["docker-compose.yml"]` |
| `environment` | the filled `.env.example` |
| `auto_update` | `false` |

`run_directory` is `librechat/`, not the repo root as `docker-registry` uses,
so that `./librechat.yaml` resolves next to the compose file.

### `.github/workflows/deploy.yml` — unchanged

Deliberately. No paths-filter entry, no matrix entry, no Actions secret. If
this stack ever appears in that file, two control planes are fighting over one
compose project.

## Cutover

Nothing is being migrated, so this is an install rather than a cutover. Ordered
so no step depends on an unverified one.

1. **Confirm the Foundry deployment works** from the host, before anything is
   built: a `curl` against the chat completions endpoint with the key. A wrong
   `apiVersion` or deployment name otherwise surfaces as an empty model list in
   a UI that is otherwise perfectly healthy.
2. **Merge the stack files.** Nothing deploys — Komodo has no Stack resource
   for them yet.
3. **Add the Caddy block and the tunnel hostname.** `librechat.sussman.win`
   resolves and returns a 502, which confirms the whole edge path up to the
   missing container.
4. **Create the Komodo Stack** with the environment filled in, and deploy.
5. **Create the first account** — `docker exec -it librechat npm run
   create-user` — with `ALLOW_REGISTRATION=false` already in effect. There is
   no window during which the login page accepts signups.
6. **Verify against the acceptance tests below**, then add the GitHub webhook
   so subsequent pushes deploy themselves.

The webhook goes on last on purpose: until step 5 passes there is no reason to
let a push redeploy anything.

## Acceptance

Status badges do not count for any of these.

| Criterion | Test |
|---|---|
| The edge path works | `librechat.sussman.win` serves the login page over the tunnel |
| Registration really is closed | The signup form is absent, and a direct `POST /api/auth/register` is rejected |
| Foundry is wired correctly | A chat completes, and the model list matches the deployments in `librechat.yaml` |
| Images work | An image attachment gets a substantive answer, not a refusal to see it |
| Word and PDF actually parse | A `.docx` upload is answered from its real contents. This is the one that silently half-works: the failure mode is a fluent answer about garbage, so the test must use a document whose contents the tester can check |
| State survives redeploy | Push a trivial change, let the webhook redeploy, confirm conversation history is still there — the project-name hazard, tested rather than assumed |

## Rollback

Three moves, in this order:

1. Delete the Stack in Komodo (removes containers, leaves volumes).
2. Remove the Caddy block and the tunnel hostname.
3. Delete the GitHub webhook.

Then `docker volume rm` the five `librechat_*` volumes if the data is not
wanted. Nothing else in the repo is touched by this change, so there is no
step 4.

## Out of scope

- **Document authoring.** "Have it write me a Word document and hand it back"
  is a separate design, because the delivery seam is the hard part rather than
  the writing. Three real options, none free:
  - *Azure Assistants with `code_interpreter`.* The only option that reuses
    the Foundry resource already being paid for **and** produces a genuine
    in-chat download: assistant `file_path` annotations are resolved through
    `retrieveAndProcessFile` into LibreChat's own storage
    ([Threads/manage.js:589](https://github.com/danny-avila/LibreChat/blob/v0.8.7/api/server/services/Threads/manage.js#L589)),
    served by the authenticated download route. Gated on whether the Foundry
    resource exposes the Assistants API at all — Microsoft has been steering
    new work toward the Foundry Agent Service, and availability is per-region
    and per-model. **Check this before considering the other two.**
  - *LibreChat's Code Interpreter API.* Works, genuinely sandboxed, real
    downloads — but it is a paid subscription and document contents leave the
    homelab, which no other service here does.
  - *An MCP server plus a separately-protected download route.* Self-hosted
    and free, but it cannot return the file through LibreChat (see Concepts),
    so the file has to be served from somewhere else, and that somewhere else
    cannot borrow LibreChat's session. It would need its own Cloudflare Access
    policy or signed URLs. Note also that the obvious candidate,
    `GongRzhe/Office-Word-MCP-Server`, was **archived** on 2025-12-31;
    `vivekVells/mcp-pandoc` is the maintained alternative but is stdio-only,
    so it would have to be baked into a custom LibreChat image alongside
    pandoc and `uv`.
- **MCP servers generally.** Worth doing, but it should be its own effort
  rather than a rider on a first deploy.
- **The local Ollama already running** in the home-assistant stack. It is
  reachable at `http://ollama:11434` over `internal` and would be a two-line
  custom endpoint, but mixing a second provider into the first deploy
  confounds the Foundry configuration if anything goes wrong.
- **Backups.** The named volumes are not yet in any backup routine. Real, and
  the same gap `docker-registry` has.
- **Observability.** The monitoring stack does not scrape this. LibreChat
  exposes no Prometheus endpoint without extra work.
- **Migrating this stack to Portainer.** It is a Komodo stack on purpose; if
  the Komodo verdict goes the other way, this stack moves with the rest and
  `librechat.yaml` becomes a `config-agent` payload at that point.
