# GHCR Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Trakt bot's and group-split's images off the internet-facing
`registry.sussman.win` and onto private GHCR packages, then decommission the
registry.

**Architecture:** Both apps are .NET Aspire, and each declares its registry in a
single `AddContainerRegistry(...)` line that sets *both* the push target and the
image names written into the compose file Komodo deploys. Changing that one line
therefore flips push and pull together — there is no period where both registries
serve the same images. Komodo authenticates the pull itself, via a stored registry
account plus two fields on each Stack.

**Tech Stack:** .NET Aspire CLI, GitHub Actions, GitHub Container Registry,
Komodo v2.3.1, Docker Compose.

**Spec:** [docs/superpowers/specs/2026-09-04-registry-to-ghcr-design.md](../specs/2026-09-04-registry-to-ghcr-design.md)

## Global Constraints

- **Komodo is pinned to v2.3.1** (`komodo/docker-compose.yml:28,50`). The
  `registry_provider` / `registry_account` Stack fields are verified present in
  that exact tag.
- **Pushing to `main` deploys, in all three repos.** Every commit below is a
  production deploy. Branch and merge deliberately; never commit "just to save it".
- **Secrets never enter git.** The PAT goes into the server's `komodo/.env` only.
  Not this repo's `.env.example`, not a workflow literal, not Komodo's UI.
- **The credential must exist before the first flipped push** (Task 1 before
  Tasks 2 and 4), because the flip changes pull names at the same moment.
- **The shared action must be on `main` before the bot's commit** (Task 0
  before Task 2). The bot pins `update-stack@main`, so a `registry-provider`
  input that exists only on a branch fails the bot's deploy as an unknown input.
- **The local `group-split` clone is stale.** `/mnt/Data/work/Sussman Club` last
  committed 2025-11-24; GitHub's `main` moved on 2026-09-04. Pull before editing
  or you will silently revert nine months of work.
- **Registry identifiers, used verbatim throughout:**
  - `registry_provider` = `ghcr.io`
  - `registry_account` = `lorainemg`
  - bot images → `ghcr.io/lorainemg/traktv-tg-bot/*`
  - group-split images → `ghcr.io/sussman-club/group-split/*`

---

### Task 0: Let the shared `update-stack` action set the registry fields

Found on 2026-09-05, when this plan was checked against the repos: the bot no
longer builds its own Komodo payload. Since bot commit `8d2bb0e` it calls this
repo's `update-stack` composite action, which had no way to pass
`registry_provider` / `registry_account`. Setting the fields by hand in the
Komodo UI was rejected — a partial `UpdateStack` would preserve them, but the
setting would live only in Mongo, the same gap `LEARNING.md` already lists for
`group-split`'s `ignore_services`.

**Files:**
- Modify: `.github/actions/komodo/client/komodo.py` (`stack_config`)
- Modify: `.github/actions/komodo/client/cli.py` (`update-stack` flags)
- Modify: `.github/actions/komodo/update-stack/action.yml` (two inputs)
- Modify: both test files under `.github/actions/komodo/client/tests/`

**Interfaces:**
- Produces: `registry-provider` and `registry-account` inputs on `update-stack`,
  which Task 2 sets in the bot's workflow.

- [x] **Step 1: `stack_config` carries the pair, test first** — two keyword
  arguments defaulting to `None`; given both, the config gains
  `registry_provider` and `registry_account`. Given exactly one, it raises
  `KomodoError` naming the missing input: Komodo looks the stored account up by
  provider *and* username, so a half pair would skip the login silently and
  fail minutes later on `docker pull` instead.
- [x] **Step 2: the CLI passes them through** — `--registry-provider` and
  `--registry-account` on `update-stack`, forwarded to `stack_config`.
- [x] **Step 3: the action declares them** — two optional inputs, wired through
  `env:` to the flags, added only when non-empty so "not given" survives the
  whole chain. The manifest test fails on a declared-but-unwired input.
- [x] **Step 4: Commit** — `9997510 let update-stack set a stack's registry
  login`. 38 tests pass.
- [x] **Step 5: Merge to `main`** — PR #11, squash-merged 2026-09-06 as
  `778f21a`. The merge deployed nothing (no filter in `deploy.yml` watches
  `.github/actions/`) and `test-actions.yml` passed on `main`.

---

### Task 1: Give Komodo a GHCR credential

Nothing else works until the server can authenticate a pull. The credential is a
config file in this repo plus one line in the server's `.env`; the spec's "The
pull side" records why the Komodo UI route was dropped.

**Files:**
- Create: `komodo/registries.config.toml` — the `[[image_registry]]` block with
  `token = "${GHCR_PULL_TOKEN}"`
- Modify: `komodo/docker-compose.yml` — mount it read-only at
  `/config/registries.config.toml` in Core
- Modify: `komodo/.env.example` — document `GHCR_PULL_TOKEN`
- Server only, never committed: `/home/lorainemg/homelab/komodo/.env`

**Interfaces:**
- Produces: a registry account that Tasks 2 and 4 reference as
  `registry_provider = "ghcr.io"`, `registry_account = "lorainemg"`.

- [x] **Step 1: The repo side** — the three files above, 2026-09-06. Core's
  config loader expands `${VAR}` from its environment before parsing, so the
  committed file holds no secret. Core reads every `*config.*` file under
  `/config` once at startup and extends arrays across them, so the bundled
  default survives. Verified by parsing the TOML and rendering the compose file.

- [x] **Step 2: Create the read-only PAT** — done 2026-09-06.

  GitHub → Settings → Developer settings → Personal access tokens → Tokens
  (classic) → Generate new token.

  - Note: `komodo-ghcr-pull`
  - Expiration: 1 year
  - Scopes: **`read:packages` only.** Not `write:packages`, not `repo`.

  Copy the value; it is shown once.

- [x] **Step 3: Put it in the server's `.env`**

  Append one line to `/home/lorainemg/homelab/komodo/.env` (owned by
  `lorainemg`, mode 600):

```
GHCR_PULL_TOKEN=<the PAT from Step 2>
```

  Nowhere else: not this repo's `.env.example`, not a workflow, not Komodo's UI.

- [x] **Step 4: Merge, then bring the server checkout to `main`**

  Core runs from `/home/lorainemg/homelab/komodo` (the compose `working_dir`
  label on the live container), and that clone sits on `komodo-migration`, 30
  commits behind `origin/main` with a clean tree (checked 2026-09-06). Its
  `komodo/` differs from `main` only in comments and in `stacks.toml`, which
  nothing on the host reads, so the mount is the only functional change.

```bash
ssh home 'cd homelab && git switch main && git pull --ff-only'
```

  Done 2026-09-06 with one variation: the checkout is on `ghcr-pull-token`,
  not `main`, because this repo's PR merges only when the whole plan is done
  and Core needed the mount before that. Switch it to `main` after the merge.

- [x] **Step 5: Recreate Core only** — run by hand 2026-09-06 22:29 UTC (the
  classifier refuses this command from a session).

```bash
ssh home 'cd homelab && docker compose --project-directory komodo up -d core'
```

  Compose sees the new mount and recreates `komodo-core`; Mongo and Periphery
  are untouched and Periphery reconnects on its own. The UI is unreachable for
  the seconds Core takes to start.

- [x] **Step 6: Verify from the startup log, not the UI** — log shows
  `domain: "ghcr.io"`, `username: "lorainemg"`, masked token.

```bash
ssh home 'docker logs komodo-core 2>&1 | grep -o "image_registries: .\{0,160\}" | tail -1'
```

  Expected: `domain: "ghcr.io"`, `username: "lorainemg"` and
  `token: "##############"`. The sanitizer masks a non-empty token and prints an
  empty one as `""`, so `token: ""` means the variable did not expand — fix the
  `.env` line and repeat Step 5. Settings → Providers and
  `ListImageRegistryAccounts` read Mongo only and will never show this account;
  the log and a real deploy are the only checks.

- [x] **Step 7: No further commit**

  The repo side went in with Step 1. Nothing on the server is tracked.

---

### Task 2: Point the Trakt bot at GHCR

**Files:**
- Modify: `/mnt/Data/study/traktv-tg-bot/apphost.cs:27`
- Modify: `/mnt/Data/study/traktv-tg-bot/.github/workflows/deploy-main.yml`

> Use `/mnt/Data/study/traktv-tg-bot`. A second, stale clone exists at
> `/mnt/Data/_Projects/traktv-tg-bot` (last commit 2026-03-26) — not that one.
> Branch from `origin/main`: the local checkout sits on `use-komodo-action`,
> already merged as `8d2bb0e`, with an unrelated uncommitted `CLAUDE.md` edit
> that stays out of this work.

**Interfaces:**
- Consumes: the Komodo registry account from Task 1.
- Produces: packages under `ghcr.io/lorainemg/traktv-tg-bot/`, which Task 3 makes
  private.

- [x] **Step 1: Grant the workflow permission to write packages**

  In `deploy-main.yml`, the `build-and-deploy` job currently declares:

```yaml
    permissions:
      contents: read
```

  Change to:

```yaml
    permissions:
      contents: read
      packages: write
```

  Without this the automatic `GITHUB_TOKEN` is read-only and the push 403s.

- [x] **Step 2: Add a GHCR login step**

  This repo has no login step at all today, because the old registry needed no
  credentials. Insert immediately **before** the `Push images and prepare env with
  Aspire` step:

```yaml
      # GHCR requires auth even to push to your own namespace. The automatic
      # GITHUB_TOKEN can write packages under this repo's owner and nowhere else,
      # which is exactly the scope wanted here — no stored secret to rotate.
      - name: Log in to GHCR
        uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
```

- [x] **Step 3: Flip the registry declaration**

  `apphost.cs:27`, before:

```csharp
var registry = builder.AddContainerRegistry("registry", "registry.sussman.win", "traktv-tg-bot");
```

  after:

```csharp
var registry = builder.AddContainerRegistry("registry", "ghcr.io", "lorainemg/traktv-tg-bot");
```

  The third argument is the namespace prefixed to every image name, so it must
  carry the GHCR owner. This line sets both where images are pushed and what the
  generated compose file tells Komodo to pull.

- [x] **Step 4: Tell the Komodo stack how to authenticate**

  The `Push the generated compose and env to the stack` step calls
  `lorainemg/homelab/.github/actions/komodo/update-stack@main`. Add two lines
  to its `with:` block, under `env-file:`:

```yaml
          registry-provider: ghcr.io
          registry-account: lorainemg
```

  The action (Task 0) turns these into the two `StackConfig` fields whose doc
  comment reads "used to login to a registry before docker compose up". They
  belong here, in this repo's workflow, and **not** in the homelab repo's
  `komodo/stacks.toml` — that file's header explains that declaring
  `trakt-tg-bot` there would wipe `file_contents` and `environment` on every
  sync. Setting only one of the two fails the step before anything reaches
  Komodo.

- [x] **Step 5: Commit and deploy** — committed as `8a87f8a` on `ghcr-registry`,
  PR lorainemg/traktv-tg-bot#14 (2026-09-06). Merging is the deploy; held
  until Task 1 Step 6 passes.

```bash
cd /mnt/Data/study/traktv-tg-bot
git add apphost.cs .github/workflows/deploy-main.yml
git commit -m "push the bot images to ghcr instead of the home registry"
git push
```

- [x] **Step 6: Watch the run** — the first run failed at "Login to Registry"
  with `did not find token in config`, because Core had not been recreated
  (Task 1 Step 5); the re-run after the restart passed.

```bash
gh run watch --repo lorainemg/traktv-tg-bot
```

  Expected: the login step succeeds, Aspire pushes to `ghcr.io/...`, all three
  Komodo steps pass, and the final step reports the deploy Complete.

  If the deploy fails on an image pull, Task 1 did not take — check the Stack's
  registry fields in Komodo before re-running.

- [x] **Step 7: Confirm the container is running from GHCR** —
  `ghcr.io/lorainemg/traktv-tg-bot/bot:aspire-deploy-20260906223915`.

```bash
ssh home 'docker ps --format "{{.Names}}\t{{.Image}}" | grep -i trakt'
```

  Expected: image names begin `ghcr.io/lorainemg/traktv-tg-bot/`.

---

### Task 3: Lock down the bot's packages

Do this in the same sitting as Task 2. The packages exist from the first push, and
their permissions are inherited until changed.

**Files:** none — GitHub package settings.

**Interfaces:**
- Consumes: packages published by Task 2.

- [x] **Step 1: List what was published** — one package, `traktv-tg-bot/bot`.

  Visit `https://github.com/lorainemg?tab=packages`.
  Expected: one package per image the bot builds, each linked to
  `lorainemg/traktv-tg-bot`.

- [x] **Step 2: For each package, confirm visibility is Private** — it was
  Public; flipped by hand 2026-09-06.

  Package → Package settings → Danger Zone → Change visibility.
  **Found 2026-09-06: the package came out Public.** A package created by a
  workflow with the automatic `GITHUB_TOKEN` "inherits the visibility and
  permissions model of the repository where the workflow is run", and this repo
  is public. The "private by default" line the spec quoted applies only to
  packages pushed without a repository link. Set it to Private by hand; the
  setting sticks for every later push. There is no personal-account setting
  that prevents this for a future new image.

- [ ] **Step 3: For each package, remove inherited permissions**

  Package settings → Manage access → remove the inherited repository permissions,
  leaving an explicit access list.

  This is the point of the task. By default GHCR holds no access list of its own
  and forwards "may this account pull?" to the linked repo — and
  `lorainemg/traktv-tg-bot` is public. Two GitHub docs pages read differently on
  whether that forwarding then says yes to everyone (see the spec's Visibility
  section), so the design removes the dependency rather than resolving it.

- [x] **Step 4: Verify a stranger cannot pull** — checked over GHCR's HTTP API
  rather than `docker pull` (no daemon on the workstation): an anonymous token
  from `ghcr.io/token` got 200 on the tag list before the flip and 401 after.

```bash
docker logout ghcr.io
docker pull ghcr.io/lorainemg/traktv-tg-bot/bot:latest
```

  Expected: `denied` or `unauthorized`. **A successful pull here is a failed
  task** — it means the images are readable by anyone and the migration has moved
  the leak rather than closed it. Stop and fix before continuing.

- [x] **Step 5: Verify the credential still works** — the re-run's deploy
  pulled with it.

```bash
echo "<the PAT from Task 1>" | docker login ghcr.io -u lorainemg --password-stdin
docker pull ghcr.io/lorainemg/traktv-tg-bot/bot:latest
docker logout ghcr.io
```

  Expected: pull succeeds. This proves Step 3 did not lock out the server's own
  token.

- [ ] **Step 6: No commit**

  Nothing changed in any repo.

---

### Task 4: Point group-split at GHCR

Structurally identical to Task 2, but this repo already has a login step to edit
rather than one to add, and it pushes to the **org** namespace.

**Files:**
- Modify: `/mnt/Data/work/Sussman Club/src/GroupSplit.AppHost/AppHost.cs:120`
- Modify: `/mnt/Data/work/Sussman Club/.github/workflows/deploy.yml`

- [x] **Step 1: Refresh the stale clone first**

```bash
cd "/mnt/Data/work/Sussman Club"
git fetch origin
git status
git log -1 --date=short --format='%ad %s' origin/main
```

  Checked 2026-09-05: the checkout is on `dev`, 425 commits behind `origin/dev`,
  and its only local changes are line-ending noise in `.gitignore` and
  `.aspire/settings.json` (empty under `git diff --ignore-cr-at-eol`).
  `origin/main` is at 2026-09-05 and `origin/dev` is 29 commits ahead of it.
  The deploy fires on what lands on `main`, so decide before branching whether
  this change rides the next `dev → main` merge or goes to `main` on its own.
  Editing the checkout without fetching would revert nine months of work.

- [x] **Step 2: Grant the workflow permission to write packages**

  In `deploy.yml`, the `build-and-deploy` job declares:

```yaml
    permissions:
      contents: read
```

  Change to:

```yaml
    permissions:
      contents: read
      packages: write
```

- [x] **Step 3: Repoint the existing login step**

  Around line 122, before:

```yaml
      - name: Log in to the container registry
        if: ${{ env.REGISTRY_USERNAME != '' }}
        uses: docker/login-action@v4
        with:
          registry: registry.sussman.win
          username: ${{ env.REGISTRY_USERNAME }}
          password: ${{ env.REGISTRY_PASSWORD }}
```

  after:

```yaml
      - name: Log in to GHCR
        uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
```

  The `if:` guard goes with it: the old step was optional because the old registry
  needed no auth, whereas GHCR always does. The automatic token replaces the
  `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` secrets entirely.

- [x] **Step 4: Drop the now-unused registry secrets from the job env**

  Lines 29-31, before:

```yaml
      # Lifted to job level because the `secrets` context is not available in a
      # step's `if:`, only in job `env:`. Reading them back as `env` there works.
      REGISTRY_USERNAME: ${{ secrets.REGISTRY_USERNAME }}
      REGISTRY_PASSWORD: ${{ secrets.REGISTRY_PASSWORD }}
```

  after: delete all four lines. The comment explains a lifting that only existed to
  serve the `if:` guard removed in Step 3.

  Leave the `^KOMODO_|^GITHUB_|^REGISTRY_` exclusion in the "Export secrets and
  variables as Aspire parameters" step alone — it is harmless once the secrets are
  gone, and it keeps working if they are re-added later.

- [x] **Step 5: Flip the registry declaration**

  `src/GroupSplit.AppHost/AppHost.cs:120`, before:

```csharp
    var registry = builder.AddContainerRegistry("registry", "registry.sussman.win", "group-split");
```

  after:

```csharp
    var registry = builder.AddContainerRegistry("registry", "ghcr.io", "sussman-club/group-split");
```

  Note the owner is `sussman-club`, not `lorainemg` — this repo belongs to the org,
  so its automatic token can write only that namespace.

- [ ] **Step 6a: Make the org create packages private, before the first push**

  Unlike a personal account, an org has two switches that apply to every new
  package. Sussman-Club → Settings → Packages:

  - **Package Creation**: untick **Public**, leave **Private**. New packages
    are then born private instead of inheriting the public repo's visibility.
  - **Default Package Settings**: untick **Inherit access from source
    repository**. New packages keep an explicit access list from the start,
    which is Step 9's "remove inherited permissions" done once for all three
    (`api`, `web`, `migrations-internal`).

  Verify on the first push anyway (Step 9): the docs describe the switches,
  the bot's package proved the docs can read differently from what happens.

- [x] **Step 6: Tell the Komodo stack how to authenticate**

  Find this repo's `UpdateStack` payload — the step that pushes the generated
  compose into Komodo, matching the bot's Task 2 Step 4. Add to its `config`
  object, alongside `file_contents`:

```
                                registry_provider: "ghcr.io",
                                registry_account: "lorainemg",
```

  The account is still `lorainemg` — the credential is the *puller's*, and the one
  PAT reads both namespaces.

- [ ] **Step 7: Commit and deploy** — committed on `ghcr-registry` from a
  worktree off `origin/main` (the clone's 400-odd modified files are
  line-ending noise, empty under `--ignore-cr-at-eol`), PR opened against
  `main` 2026-09-06 with a note to retarget to `dev` if preferred. Held until
  Task 1 Step 6 passes.

```bash
cd "/mnt/Data/work/Sussman Club"
git add src/GroupSplit.AppHost/AppHost.cs .github/workflows/deploy.yml
git commit -m "push group-split images to ghcr instead of the home registry"
git push
```

- [ ] **Step 8: Watch the run and confirm the pull**

```bash
gh run watch --repo Sussman-Club/group-split
ssh home 'docker ps --format "{{.Names}}\t{{.Image}}" | grep -i group'
```

  Expected: images beginning `ghcr.io/sussman-club/group-split/`.

- [ ] **Step 9: Lock down the org packages**

  Repeat Task 3's Steps 1-5 against
  `https://github.com/orgs/Sussman-Club/packages`.

  Watch for one thing here that Task 3 could not test: the PAT belongs to
  `lorainemg`, a *member* of the org, so Step 5's authenticated pull is the check
  that org membership actually grants read after inherited permissions are removed.
  If it fails, add `lorainemg` to that package's access list explicitly — do not
  widen the token's scopes.

- [ ] **Step 10: Delete the dead secrets**

  In the repo's settings, remove the now-unused `REGISTRY_USERNAME` and
  `REGISTRY_PASSWORD` secrets.

---

### Task 5: Decommission the registry in the homelab repo

Only after both stacks are confirmed running from GHCR.

**Files:**
- Modify: `config/caddy/Caddyfile:12-14`
- Modify: `komodo/stacks.toml` (the `docker-registry` block)
- Modify: `LEARNING.md:367`
- Modify: `README.md:39`, `README.md:105`, `README.md:150`, `README.md:169`

- [x] **Step 1: Remove the Caddy route**

  `config/caddy/Caddyfile`, delete:

```
http://registry.sussman.win {
    reverse_proxy registry:5000
}
```

  Caddy hot-reloads from Komodo's checkout, so this takes effect on the next
  deploy of the `config` stack with no rebuild.

- [x] **Step 2: Remove the Stack declaration**

  `komodo/stacks.toml`, delete the whole block:

```toml
[[stack]]
name = "docker-registry"
[stack.config]
server = "Local"
project_name = "docker-registry"
repo = "lorainemg/homelab"
file_paths = ["registry/docker-compose.yml"]
links = ["https://registry.sussman.win", "http://${HOMELAB_LAN_IP}:5000"]
```

  The file's own header notes `delete: false`, so removing a declaration leaves the
  live Stack untouched rather than destroying it. Delete the Stack by hand in the
  Komodo UI afterwards — that is the step that stops the container.

- [x] **Step 3: Keep `registry/docker-compose.yml` for now**

  Leave the file in the repo until the volume is deleted in Task 6. With the Stack
  gone it deploys nothing, and it is the fastest way to bring the old registry back
  if Task 6's grace period turns up a problem.

- [x] **Step 4: Update the written record**

  `LEARNING.md:367` — replace the "Lock down `registry.sussman.win`, or stop using
  it" bullet under **Next** with a **Covered** entry naming what was done, the date,
  and the one non-obvious finding: that GHCR forwards package permissions to the
  linked repo unless inheritance is removed.

  `README.md:105` — delete the `registry/` row from the stack table.
  `README.md:39` — drop the `REGISTRY` node from the architecture diagram, and
  `README.md:169` — drop the `registry/` line from the repo layout tree.

  `README.md:150` — the "Self-hosted CI artifact flow" bullet now describes
  something that no longer exists. Rewrite it for GHCR.

- [x] **Step 5: Commit** — `46af2ff` and the LEARNING.md commit after it, on
  `ghcr-pull-token`; merges with the plan's single PR.

```bash
cd /mnt/Data/work/homelab
git add config/caddy/Caddyfile komodo/stacks.toml LEARNING.md README.md
git commit -m "stop publishing the home registry now that images live in ghcr"
git push
```

- [ ] **Step 6: Verify the route is gone**

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://registry.sussman.win/v2/_catalog
```

  Expected: not 200. A 200 with a JSON catalogue means the `config` stack has not
  redeployed yet — check Komodo before assuming failure.

---

### Task 6: Remove the last traces

- [ ] **Step 1: Delete the Stack in Komodo**

  Komodo UI → Stacks → `docker-registry` → delete. Confirm the `registry`
  container is gone:

```bash
ssh home 'docker ps -a --filter name=registry --format "{{.Names}}"'
```

  Expected: no output.

- [ ] **Step 2: Delete the DNS record and tunnel hostname**

  In Cloudflare, remove the `registry` hostname from the tunnel's public hostname
  list, and its DNS record if one exists separately. Note from prior work: the
  `sussman.win` zone lives in a differently-named Cloudflare account, and a
  wildcard tunnel CNAME can mask which subdomains are actually configured — check
  the tunnel's hostname list, not just DNS.

- [x] **Step 3: Schedule the volume deletion** — in LEARNING.md's Next list,
  dated 2026-10-06, committed with Task 5.

  Add to `LEARNING.md` under **Next**, matching the existing `portainer_data`
  entry's format:

```markdown
- Delete `docker-registry_registry-data` on or after **2026-10-04** — the last
  copy of the old registry's images, kept one month as this migration's undo.
  `ssh home 'docker volume rm docker-registry_registry-data'`.
```

  The `alpine` and `app` images in the old catalogue are not migrated; they die
  with this volume.

- [ ] **Step 4: Commit**

```bash
cd /mnt/Data/work/homelab
git add LEARNING.md
git commit -m "note when to delete the old registry volume"
git push
```

## Final verification

Run all four. The migration is done when every one passes:

```bash
# 1. The old registry is unreachable from the internet.
curl -s -o /dev/null -w '%{http_code}\n' https://registry.sussman.win/v2/_catalog   # not 200

# 2. A stranger cannot pull either app's images.
docker logout ghcr.io
docker pull ghcr.io/lorainemg/traktv-tg-bot/bot:latest                              # denied
docker pull ghcr.io/sussman-club/group-split/web:latest                             # denied

# 3. The pull credential still works.
echo "<PAT>" | docker login ghcr.io -u lorainemg --password-stdin
docker pull ghcr.io/lorainemg/traktv-tg-bot/bot:latest                              # succeeds
docker logout ghcr.io

# 4. Both stacks run from GHCR images.
ssh home 'docker ps --format "{{.Names}}\t{{.Image}}"' | grep -E 'trakt|group'      # ghcr.io/...
```
