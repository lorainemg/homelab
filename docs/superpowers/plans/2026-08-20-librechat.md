# LibreChat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run LibreChat v0.8.7 at `librechat.sussman.win`, deployed by Komodo straight from this repo, backed by an existing Microsoft Foundry resource on its v1 API, with images and Office/PDF attachments both working and no open signup window.

**Architecture:** A new `librechat/` stack (api + mongo + meilisearch + rag parser + pgvector) published through the existing Caddy + Cloudflare Tunnel path. Komodo clones this repo and runs the repo-relative `librechat/docker-compose.yml`, so the `config/` directory is mounted from the clone and LibreChat's `CONFIG_PATH` points at its YAML rather than baking it into an image and flowing it through `config-agent`. `.github/workflows/deploy.yml` is untouched.

**Tech Stack:** Docker Compose, LibreChat v0.8.7, MongoDB 8, Meilisearch v1.35.1, pgvector 0.8.0, Komodo v2.3.1, Caddy 2, Cloudflare Tunnel, GitHub webhooks, Microsoft Foundry (Azure OpenAI).

**Spec:** [docs/superpowers/specs/2026-08-20-librechat-design.md](../specs/2026-08-20-librechat-design.md)

## Global Constraints

- **Branch:** all repo work happens on `librechat`, branched from `main`.
- **Compose project name is `librechat`**, pinned by the top-level `name:` key in the compose file itself. It prefixes every volume; any other value makes compose silently create empty ones and LibreChat starts healthy with no history.
- **`librechat/` is Komodo-managed and never CI-deployed.** It must not appear in any paths-filter or deploy matrix in `.github/workflows/deploy.yml`. If it does, CI and Komodo are both driving the same compose project.
- **No relative state bind mounts; the only relative bind is the `./config` directory.** Komodo runs compose from its own clone; Docker creates a *directory* for a missing bind source, so upstream's `./.env`, `./data-node`, `./uploads` and `./logs` mounts would put live state inside a working copy that gets re-pulled. A directory mount is required because git replaces files during a pull.
- **Image tags are pinned.** `ghcr.io/danny-avila/librechat:v0.8.7`, `mongo:8.0.20`, `getmeili/meilisearch:v1.35.1`, `pgvector/pgvector:0.8.0-pg15-trixie`. The rag-api image publishes only `:latest` and is pinned by digest.
- **Secrets never in git.** Only `librechat/.env.example` with placeholders is committed. The filled values go into `librechat/.env` in Komodo's checkout with mode `600`, not Komodo's Environment field and not GitHub Actions secrets, which cannot reach a stack CI does not deploy.
- **Secret delivery is not yet production-ready.** Komodo's `docker compose config` step can render interpolated `.env` values into its normalized config log. Resolve that exposure before deploying this stack publicly.
- **`ALLOW_REGISTRATION=false` from the very first boot.** Accounts are made with the bundled CLI. There is never a window where a public login page accepts signups.
- **Commit messages:** single line, casual, no trailers — matching the existing log (`add the komodo stack`, `route komodo.sussman.win to komodo`).
- **Host commands** run on the homelab server over SSH. Repo commands run in the local clone. Each step says which.

---

### Task 1: Confirm the Foundry deployment answers before building anything

Host-only (or any machine with the key). Nothing is created here. This runs first because the symptoms it catches — a wrong URL, or an auth scheme the resource rejects — surface later as an empty model picker in a UI that is otherwise perfectly healthy, which is the slowest possible way to find out.

**Interfaces:**
- Produces: a confirmed `AZURE_BASE_URL`, and confirmation that the resource accepts `Authorization: Bearer <key>` — which is how LibreChat's OpenAI-compatible client authenticates.

- [ ] **Step 1: Confirm `models` answers, with Bearer auth**

```bash
curl -sS "$AZURE_BASE_URL/models" -H "Authorization: Bearer $AZURE_API_KEY"
```

This is the exact call `models.fetch` makes. A JSON list means the URL and the auth scheme are both right, and its contents are the deployment names that will appear in the picker.

If it returns 401 but the same request with `-H "api-key: $AZURE_API_KEY"` succeeds, the resource wants the Azure-style header. Add it in `librechat/config/librechat.yaml` rather than changing the URL:

```yaml
      headers:
        api-key: "${AZURE_API_KEY}"
```

- [ ] **Step 2: Confirm a completion**

```bash
curl -sS "$AZURE_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $AZURE_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"<deployment-from-step-1>","messages":[{"role":"user","content":"say ok"}],"max_tokens":5}'
```

On the v1 API `model` is the deployment name, sent in the body — there is no deployment in the path and no `api-version` query parameter.

**Verification:** both calls return JSON, and `AZURE_BASE_URL` is recorded for Task 4.

---

### Task 2: Scaffold the `librechat/` stack in the repo

Repo-only. Nothing is deployed — this task produces reviewable files and a compose file that resolves.

**Files:**
- Create: `librechat/docker-compose.yml`
- Create: `librechat/config/librechat.yaml`
- Create: `librechat/.env.example`
- Verify: `.gitignore` (no change expected — confirm `*/.env` covers `librechat/.env`)

**Interfaces:**
- Consumes: the external `internal` network created by `scripts/bootstrap.sh`.
- Produces: five containers — `librechat`, `librechat-mongo`, `librechat-meilisearch`, `librechat-rag`, `librechat-vectordb`. Only `librechat` joins `internal`; Task 3 reverse-proxies to `librechat:3080`. Task 4 deploys this compose.

- [ ] **Step 1: Create `librechat/docker-compose.yml`**

Derived from upstream's compose with six deliberate departures, each listed in the spec's Components section: every tag pinned, no `./.env` bind, named volumes instead of relative state binds, the `config/` directory bind-mounted read-only with `CONFIG_PATH` set, no published ports and no `user:` override, and `admin-panel` dropped.

`container_name`s are namespaced rather than upstream's `chat-mongodb` / `vectordb` / `rag_api`, because `internal` is a shared bridge and generic names collide with whatever gets added next.

- [ ] **Step 2: Create `librechat/config/librechat.yaml` from the Task 1 values**

`version: 1.3.13` (the `CONFIG_VERSION` v0.8.7 ships), one `custom` endpoint, and `fileConfig` limits. LibreChat reads it through `CONFIG_PATH=/app/librechat-config/librechat.yaml`.

A `custom` endpoint rather than the built-in `azureOpenAI` one, because the resource is reached through its v1 (OpenAI-compatible) API. `azureOpenAI` would build `.../openai/deployments/<name>/chat/completions?api-version=<ver>` from an instance name — a different URL shape carrying an api-version that has to be maintained. The same resource also supplies the RAG API's `text-embedding-3-small` deployment.

Nothing identifying goes in the file: `baseURL` and `apiKey` are both `${...}` references resolved from the container environment, and `models.fetch` pulls the deployment list from `GET <baseURL>/models` so no deployment name is committed. `models.default` is still required by the schema (min 1 entry) — use a public model name as the placeholder. `titleModel: "current_model"` avoids a second hardcoded name.

- [ ] **Step 3: Create `librechat/.env.example`**

Every variable the compose file references, with generation commands. `CREDS_IV` is exactly 16 bytes (32 hex chars) — unlike the other three, which are 32 bytes. The filled file is placed in the Komodo checkout, not the Stack Environment field.

- [ ] **Step 4: Verify the compose resolves**

```bash
cp librechat/.env.example librechat/.env.validate
docker compose --project-directory librechat --env-file librechat/.env.validate config
rm librechat/.env.validate
```

**Verification:** exit 0, `name: librechat`, every volume rendered as `librechat_*`, no unresolved `${...}`, `CONFIG_PATH` is present, and the `config/` directory bind resolves to an absolute path next to the compose file.

---

### Task 3: Publish the hostname

Repo change plus one Cloudflare change. Deliberately before the stack exists, so the edge path can be verified separately from the application — a 502 here is a *success*, and it isolates a tunnel or Caddy mistake from a LibreChat mistake.

**Files:**
- Modify: `config/Caddyfile`

- [ ] **Step 1: Add the Caddy block**

```
http://librechat.sussman.win {
    reverse_proxy librechat:3080
}
```

- [ ] **Step 2: Add the tunnel hostname**

`librechat.sussman.win` → `http://caddy:80`, like every other service.

- [ ] **Step 3: Push and let CI deploy the config stack**

The Caddyfile change deploys `config`; Caddy mounts the directory and hot-reloads it under `--watch`, so the caddy container is never recreated and the tunnel → caddy → Komodo path survives.

**Verification:** `curl -sS -o /dev/null -w '%{http_code}' https://librechat.sussman.win` returns 502. Anything else — a 530, a Cloudflare error page, a connection failure — is a tunnel or DNS problem, and it is much cheaper to find now.

---

### Task 4: Create the Komodo Stack and deploy

Komodo UI. No webhook yet — that comes after the stack is known good.

- [ ] **Step 1: Create the Stack resource**

| Field | Value |
|---|---|
| Name | `librechat` |
| `project_name` | `librechat` |
| `repo` | `lorainemg/homelab` |
| `branch` | `main` |
| `file_paths` | `["librechat/docker-compose.yml"]` |
| `webhook_enabled` | `true` |
| `webhook_force_deploy` | `true` — every push to `main` runs the post-deploy hook |
| `post_deploy` | `docker restart librechat` |
| `auto_update` | `false` |

`file_paths` is repo-relative. Compose still resolves `./config` from the
directory containing `docker-compose.yml`, so the bind mount reaches the
checkout's `librechat/config/` directory.

- [ ] **Step 2: Place the stack `.env` in the checkout**

Copy the filled `.env.example` to
`/etc/komodo/stacks/librechat/librechat/.env` on the server and set its mode to
`600`. `CreateStack` does not create the checkout, so clone the repo first if
`/etc/komodo/stacks/librechat` does not exist:

```bash
ssh home 'git clone --branch main --depth 1 https://github.com/lorainemg/homelab /etc/komodo/stacks/librechat'
```

Do not put these values in Komodo's Environment field. Compose interpolates the
file into explicit service environment entries, which keeps service scope
narrow but still leaves the Komodo merged-config secret exposure documented in
the evaluation below.

- [ ] **Step 3: Deploy, and watch the rag_api logs specifically**

The api container comes up quickly; `librechat-rag` is the one that fails interestingly, because it initializes the Foundry embeddings client before serving `/text`. A `librechat-rag` restart loop means the embedding variables or `POSTGRES_PASSWORD` are wrong.

**Verification:** all five containers running, and `docker exec config-caddy wget -qO- http://librechat:3080` returns HTML.

---

### Task 5: Create the first account

Host-only. `ALLOW_REGISTRATION=false` is already in effect, which is the point.

- [ ] **Step 1: Run the bundled CLI**

```bash
docker exec -it librechat npm run create-user
```

**Verification:** the account logs in at `https://librechat.sussman.win`, and the signup form is absent.

---

### Task 6: Verify against the acceptance tests

All from a browser, against the public hostname. These are the spec's acceptance criteria; none of them is satisfied by a green status badge.

- [ ] **Step 1: Registration really is closed**

The signup form is gone, and `POST /api/auth/register` is rejected directly.

- [ ] **Step 2: Foundry is wired correctly**

A chat completes, and the model picker lists exactly the deployments Task 1 saw — which proves `models.fetch` reached `<baseURL>/models` rather than falling back to the placeholder.

- [ ] **Step 3: Images work**

An image attachment gets a substantive answer, not a refusal to see it.

- [ ] **Step 4: Word and PDF actually parse**

Upload a `.docx` **whose contents you can check** and ask something only its body answers. This is the test that silently half-works: with the parser misconfigured the model receives the raw bytes of a zip and will still produce a fluent, confident, wrong answer. A vague "summarise this" prompt passes when it should fail.

- [ ] **Step 5: State survives redeploy**

Have a conversation, redeploy from Komodo's UI, confirm the history is still there. This tests the project-name hazard rather than assuming it.

---

### Task 7: Add the GitHub webhook

Last on purpose: until Task 6 passes there is no reason to let a push redeploy anything.

- [ ] **Step 1: Configure it**

| Field | Value |
|---|---|
| Payload URL | `https://komodo.sussman.win/listener/github/stack/librechat/deploy` |
| Content type | `application/json` |
| Secret | the same value as `KOMODO_WEBHOOK_SECRET` in `komodo/.env` |
| Events | push only |

- [ ] **Step 2: Prove it fires**

Push a comment-only change to `librechat/config/librechat.yaml` and confirm
Komodo redeploys with `deploy.yml` uninvolved. Verify that the post-deploy hook
restarts `librechat`, because the Compose service hash does not include the
mounted YAML.

**Verification:** the Actions run for that push does not include a `librechat` deploy, and Komodo's update log shows one.

---

### Task 8: Document it

**Files:**
- Modify: `README.md`
- Modify: `scripts/bootstrap.sh`

- [ ] **Step 1: README** — a stack-table row, a repo-layout line, the LibreChat node in the architecture diagram, the container/stack counts in the intro, and the `create-user` step next to the Mosquitto one as the second manual step.
- [ ] **Step 2: bootstrap.sh** — add `librechat` to `STACKS` so the no-control-plane path still brings up everything.

**Verification:** the diagram renders, and every count in the intro matches reality.

---

## Rollback

Three moves, in this order: delete the Stack in Komodo (removes containers, leaves volumes), remove the Caddy block and the tunnel hostname, delete the GitHub webhook. Then `docker volume rm` the seven `librechat_*` volumes if the data is not wanted. Nothing else in the repo is touched by this change.
