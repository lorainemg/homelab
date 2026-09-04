# Learning log

What has actually landed, what is still shaky, and what is next. Read at the
start of a session; update when a concept lands or a new gap appears.

## Covered

- **Push vs pull control planes** — Portainer is *pushed to*: CI reads a compose
  file and hands it to an HTTP API, so Portainer never sees the repo. Komodo
  *pulls*: a Stack records repo/branch/path and Komodo clones and runs compose
  itself. The compose file stops being a payload. (2026-08-20)
- **`[skip ci]` only stops GitHub Actions** — a webhook still fires and a poller
  never sees a commit message at all, so a naive pull-based control plane would
  redeploy on config-agent's own hourly backup commits, forever. Komodo avoids
  it with `webhook_force_deploy: false`: its trigger is *file-scoped*, not
  commit-scoped. (2026-08-20)
- **The build/deploy race** — a pull-based control plane deploys but does not
  build. Today one CI job builds *then* deploys, so ordering is free; split
  across two systems, a push starts both at once and the deploy can land the
  previous `:latest` digest and report success. Mutable tags make a stale
  deploy indistinguishable from a correct one. (2026-08-20)
- **Compose resolves relative bind paths against the compose file's own
  directory** — not the working directory. Verified: `-f stack/compose.yml` and
  `--project-directory stack` produce identical absolute sources, parent-
  relative paths included. This is what lets Komodo and `bootstrap.sh` share
  one set of files. (2026-08-20)
- **Bind mounts pin inodes, and git replaces files rather than editing them** —
  so a single-file bind mount from a git checkout is frozen at the version
  present when the container started. Mount the *directory*. Verified by
  experiment. (2026-08-20)
- **Dockerfile instructions have compose equivalents once the files are on the
  host** — `COPY config` → a directory mount, `COPY`+`ENTRYPOINT` → the
  `entrypoint:` key, `mkdir` of a writable path → a named volume shadowing that
  subdirectory. Which is what makes "delete the image" a real option for
  config-only builds. (2026-08-20)
- **Where a secret is visible is where it is stored** — Komodo's Environment box
  is Mongo rendered on a public page; a host `.env` is on disk and nowhere
  else. Untracked files survive `git pull --force` but not a re-clone.
  (2026-08-20)
- **Keep the recovery path out of the blast radius** — the tunnel is the only
  way in, and the control plane's UI is behind it. Anything deployed
  automatically can be broken by a deploy, so the tunnel lives where no deploy
  can reach it. (2026-08-20)
- **Komodo's `branch` and `commit` fields are two different git commands** —
  `branch` is interpolated into `git clone <url> <path> -b <branch>`, and git's
  `-b` takes only branch names and tags, so a bare hash there fails the clone;
  on the pull path it fails one step later, after `git checkout -f <hash>` has
  already succeeded, which looks like it worked. `commit` is interpolated into
  `git reset --hard <commit>` after the clone or pull, so *that* is the field a
  rollback pins. Read from `lib/git/src/clone.rs` and `pull.rs` and reproduced
  against a real repo — the migration plan had asserted the opposite.
  (2026-08-23)
- **The quiet wrong state is the dangerous one** — a rollback pinned in the
  `commit` field leaves `branch: main` on display, webhooks still firing and
  deploys still green, while the stack is frozen forever. Preferring the option
  that *announces* it is set is a real tiebreaker, and here it lost to the
  option that actually works — so unpinning has to be part of the procedure
  rather than a tidy-up. (2026-08-23)
- **`!pattern` in dorny/paths-filter is not an exclusion** — the action defaults
  to `predicate-quantifier: some`, so a filter fires if *any* pattern matches,
  and a negated pattern matches every path it does not name. The
  `home-assistant` filter's `'!home-assistant/ha-config/**'` was therefore true
  for every commit in the repo, and every push to `main` ever made redeployed
  Home Assistant. `predicate-quantifier: every` would fix the negation but break
  the alternation the other filters need, so the exclusion was rewritten as a
  positive single-level glob (`home-assistant/*`). Fixed in `7da4b0f` on `main`.
  Reproduced with picomatch before and after, against the real CI outcome.
  (2026-08-23)
- **A deploy that restarts the proxy cannot report its own success** — the
  `config` stack contains Caddy, and CI reaches Portainer *through* Caddy. So
  redeploying `config` severs the connection the success response was travelling
  on, and the job reports a Cloudflare 502 having actually deployed fine. For
  this one stack a red CI run is not evidence; check the containers. Same shape
  as the tunnel rule above, applied to the reporting path instead of the
  recovery path. (2026-08-23)
- **Komodo's GitHub listener takes a hand-made payload, but only with a
  `ref`** — CI can trigger a deploy by signing a body with
  `KOMODO_WEBHOOK_SECRET` (HMAC-SHA256, the same scheme GitHub uses) and
  POSTing it to `/listener/github/stack/<name>/deploy`. The listener filters on
  `ref` against the Stack's branch, so `{"ref":"refs/heads/main"}` deploys and a
  bare `{}` does not. No API key needed, so CI holds nothing that can do more
  than redeploy one stack. (2026-08-23)
- **A 200 from that listener proves nothing; only a new Update does** — the
  first test fired at `docker-registry`, which has `webhook_force_deploy:
  false`: Komodo pulled, saw an unchanged compose file, and correctly did
  nothing. 200, no Update — indistinguishable from rejection. Re-running it
  against `monitoring` (`webhook_force_deploy: true`, deploys unconditionally)
  took its update count 11 → 12 and settled it. Choosing a target that *can*
  fail is half the test. (2026-08-23)
- **The checkout-mounted design, proven end to end** — a Caddyfile edit pushed
  at 01:00:25 was deployed by Komodo at 01:00:26, Caddy logged `config file
  changed; reloading`, and `StartedAt` did not move: no image build, no
  container recreation, no dropped connection. The mirror case works too — an
  `agent.sh` edit builds in CI, and only *then* does CI trigger the deploy that
  recreates `config-agent`, while Caddy sits untouched. Both halves of Task 7's
  design verified. (2026-08-23)
- **`webhook_enabled: true` is a door, not a knock** — it opens Komodo's
  listener for that stack; GitHub still needs its own webhook per stack pointing
  at `/listener/github/stack/<name>/deploy`. Forgetting it means a push lands on
  `main`, CI goes green, and nothing deploys — no error anywhere. (2026-08-23)
- **A missing GitHub Actions secret is an empty string, not an error** —
  `${{ secrets.KOMODO_URL }}` for a secret that was never created expands to
  nothing, so `curl "$EMPTY/listener/..."` failed with exit code 3 (malformed
  URL) rather than anything mentioning secrets. Worth knowing which exit code
  means what: curl 3 is a bad URL, 6 is DNS, 7 is connection refused, 22 is an
  HTTP error status under `-f`. (2026-08-23)
- **LibreChat's first Komodo shape was only partly valid** — the pinned
  `librechat` project name, named volumes, and no-CI deployment were sound, but
  a single-file bind mount freezes at the inode present when the container
  starts. LibreChat supports `CONFIG_PATH`, so its YAML now lives under a
  checkout-mounted directory. Its Stack also needs
  `webhook_force_deploy: true` and `post_deploy: docker restart librechat`:
  otherwise a commit can pull the changed YAML without restarting the API.
  The secret values follow the rest of the repo into the checkout `.env`, not
  Komodo's Environment field. (2026-08-25)
- **A host `.env` is not enough if Compose interpolates it** — Komodo runs and
  logs `docker compose config` before `up`; values written as `${SECRET}` in
  `environment:` therefore appear in the merged config unless Komodo knows how
  to redact them. `env_file:` does not solve this: Compose expands those values
  into the same normalized config output. LibreChat therefore keeps the
  narrower explicit environment mapping, but its secret delivery remains an
  open design issue before production. (2026-08-25)

- **A generated volume name is a landmine under a green deploy** — Aspire
  derives the bot's Postgres volume from an apphost hash, and an unpinned
  `curl | bash` CLI install meant a new Aspire version changed it:
  `apphost-e7f9f553c0` → `apphost-48ef807faa`. Compose doesn't warn — it
  creates the unknown name empty, Postgres initialises a blank database, and
  every container reports healthy. Caught only because the migration plan's
  gate demanded the *same volume name*, not healthy containers; recovered by
  cloning the old volume's bytes into the new name and proving it with a row
  count (128 watch_statuses). Durable fix: name the volume explicitly in the
  AppHost (`WithDataVolume`). (2026-08-25)
- **`[skip ci]` survives a squash merge** — GitHub's default squash message
  pastes in every branch commit's subject, and honors the tag *anywhere* in
  the message. A `[skip ci]` written for a direct-push plan silently cancelled
  the deploy when the plan changed to a PR. If a branch commit carries the tag,
  edit the squash message before merging. (2026-08-25)
- **Verdict on `komodo_client` (npm) for CI: works, not worth it** — the
  client pulls in `mogh_auth_client`, which reads `localStorage` at import
  time; in Node that needs `--experimental-webstorage --localstorage-file`
  (Node 22; newer Node needs only the file flag — verified on real Node 22 in
  Docker after wrongly "verifying" on local Node 26). A pin plus two runtime
  flags to adopt a typed client lost to 30 lines of curl+jq. Both variants
  push compose + env in one `UpdateStack` and poll `GetUpdate`, because
  `/execute`'s 2xx means accepted, not deployed. (2026-08-25)

- **`docker compose down` keeps named volumes; `-v` is what takes them** — one
  character is the whole difference between "Portainer stays reversible for a
  month" and "Portainer's stack definitions are gone". `down` removes the
  containers and the network it created and deliberately stops there, treating
  a named volume as data you meant to keep. (2026-08-25)
- **A token-run `cloudflared` keeps its routing table at Cloudflare** — given
  only `TUNNEL_TOKEN` and `tunnel run` there is no `config.yml` anywhere: the
  ingress rules are fetched from the dashboard at connect time. So "which
  hostname reaches which container" is not greppable in this repo, and retiring
  a hostname is a click in Zero Trust → Networks → Tunnels → Public Hostnames
  rather than a commit. The other shape — a mounted `config.yml` with `ingress:`
  rules — would put that mapping back under git, at the cost of a redeploy per
  hostname change. (2026-08-25)
- **Prose about infrastructure drifts silently, and confident prose drifts
  longest** — three instances in one session. `CLAUDE.md` still said
  `portainer/` held the tunnel, two days after Task 2 moved it to `tunnel/`.
  `bootstrap.sh`'s `STACKS` list had never gained `trakt-tg-bot`, quietly
  voiding its "rebuild the whole machine" promise — which is what settled the
  decision to shrink it to `(tunnel komodo)` rather than maintain it. And the
  README's `bootstrap.sh` paragraph went false *during* this session, from an
  edit made twenty minutes earlier. None of it has a compiler or a test. The
  habit that catches the third kind: after changing what a script does, grep
  the docs for the script's name. (2026-08-25)
- **`docker ps --format` hands you `.Labels` as one comma-joined string** — so
  `{{index .Labels "com.docker.compose.project"}}` dies with `cannot index
  slice/array with type string`. `index` needs a map, which is what `docker
  inspect` returns; the `docker ps` accessor is `{{.Label "..."}}`. Worth
  remembering as the general shape: the same field name means different types
  in different docker subcommands. (2026-08-25)
- **`RAG_USE_FULL_CONTEXT` does not remove RAG startup dependencies** — the
  lite RAG image initializes `OpenAIEmbeddings` before serving requests even
  when full-context mode avoids retrieval queries. The same Foundry v1 endpoint
  and key work for it, provided the resource has an embedding deployment; this
  resource exposes `text-embedding-3-small`. (2026-08-26)
- **Resource Sync declares whole resources, not fields — and reading the
  callee isn't reading the caller** — the first verdict recorded here tonight
  said sync only sees declared fields, read straight from `partial_derive2`'s
  diff (`None` on the TOML side → no change). The derive does work that way;
  Komodo's sync neutralizes it first: `sync/view.rs` converts the TOML partial
  into a *full* config and back, materializing every undeclared field as
  `Some(default)` — its own comment says "Makes sure things that aren't
  defined in toml ... actually get removed". So a declared resource must
  declare every non-default field or lose it; only *undeclared resources* are
  safe (with `delete: false`). Caught before it wiped `trakt-tg-bot`'s
  CI-written `file_contents` and secrets, because the pending diff was read
  on the live server before `RunSync` — the diff-review gate did its job the
  first time it existed. The bot stack's answer: its shell was seeded by
  `bootstrap.sh` (until 2026-09-02 — see the next entry), its contents stay
  owned by the bot repo's CI, and no TOML ever names it. (2026-08-25)
- **Non-admin Komodo users can create Stacks — the README said they couldn't.**
  Komodo's rule is one line, `user.admin || !core_config().disable_non_admin_create`
  (`resource/stack.rs`, v2.3.1), and that flag is unset here. What creation
  *does* need is Read **plus the specific `Attach` permission on the target
  server** — Read alone fails with "Cannot attach Stack to this Server" — and
  the creator is then granted Write on the new resource automatically.
  Verified end to end as the `trakt-tg-bot-ci` service user with a throwaway
  stack that was never deployed (volume count 72 before and after). The
  recorded "narrow key cannot create stacks" verdict had never been tried —
  the floci mistake again — and it had shaped both `bootstrap.sh` and the
  README. The bot's CI now creates its own Stack if missing; bootstrap no
  longer seeds it. One more wrinkle: `GetStack` on a missing name answers
  HTTP 500 with "Did not find any Stack", not a 404, so the CI branches on
  the body. (2026-09-02)
- **Komodo's `[[VAR]]` interpolation never reaches `links`, and the sync does
  none at all** — `lib/interpolate/src/lib.rs` (v2.3.1) lists the seven stack
  fields that get variables swapped in at deploy time: `file_contents`,
  `environment`, both deploy commands, `compose_cmd_wrapper` and the two
  extra-args lists. `bin/core/src/sync/` has no interpolation call, so
  `stacks.toml` is applied verbatim. The LAN IP in each stack's `links` stays
  literal, and that is the right answer anyway: those links exist for when
  Cloudflare is down, and a name resolved through Cloudflare's DNS would share
  the failure it is meant to survive. (2026-09-03)
- **Komodo's stack state ignores exit codes; it asks whether every container
  shares one state word** — `get_stack_state_from_containers` (`helpers/query.rs`,
  v2.3.1) is a chain of `all(...)` checks: all running → Running, all exited →
  Stopped, any *mix* → Unhealthy. A one-shot service that exited 0 beside running
  ones is a mix, so `group-split` read unhealthy while nothing was wrong. The
  knob is `ignore_services`, whose doc comment names the init-container case;
  set to `["migrations-internal"]` and the state flipped on the next poll.
  It survives that repo's CI because `UpdateStack` is a *partial* merge
  (`resource/mod.rs` diffs the payload against the stored config and writes
  only changed fields), the opposite of the sync's reset-undeclared rule
  above. (2026-09-03)

- **A wildcard DNS record does not publish a hostname; the tunnel's ingress
  list does** — `*.sussman.win` is already a proxied CNAME at the tunnel, so
  every subdomain resolves. But a token-run `cloudflared` fetches an explicit
  per-hostname ingress list that ends in `http_status:404`, so an unlisted
  subdomain resolves, reaches Cloudflare, and gets a 404 that never touches the
  server. Publishing `groupsplit.sussman.win` therefore took two changes on the
  Cloudflare side (ingress rule, plus an explicit CNAME to match the five
  hand-made ones) and one in this repo (the Caddyfile). The README said the
  tunnel "routes `*.{domain}` to Caddy", which was half the story and would have
  sent a future me hunting in Caddy for a 404 Caddy never issued. (2026-09-03)
- **A file Komodo reads verbatim cannot hold a variable, so the render moved
  to CI — and a standing credential came with it** — `links` is outside
  Komodo's interpolation and the sync applies `stacks.toml` word for word, so
  the server's LAN address sat in five links here and in the bot's workflow.
  Now `stacks.toml` is a template, `komodo/vars.env` holds the value once, and
  the `sync-komodo` job renders with `envsubst`, pushes the result into the
  sync as contents (a sync with no `repo` and no `files_on_host` reads its
  `file_contents`, `sync/remote.rs`) and runs it. Rejected on the way: a Komodo
  Variable (a seventh copy nothing could reference) and committing a rendered
  copy back (two `stacks.toml` in git). The cost, taken with eyes open: CI now
  holds an API key that can rewrite every Stack, `post_deploy` commands
  included, where before it held a webhook secret that could only redeploy.
  Scoped to a `homelab-ci` service user with Write on the one sync. (2026-09-03)

## Shaky

- Komodo's Resource Sync (stacks declared as TOML in the repo) — deliberately
  untouched so far; it is the obvious next capability after this migration.
- LibreChat's production acceptance run — the stack has not yet been deployed
  and its volume backup/restore and public-hostname tests remain open.
- LibreChat secret delivery — the host `.env` avoids Git and Komodo's
  Environment page, but Compose expands it into Komodo's normalized config log;
  a secret-aware runtime path is still needed before public deployment.
- Restoring Komodo's Mongo — now *run* (2026-08-20) and it works, but the
  procedure as originally written tested the wrong thing: it copied from the
  live database instead of opening a backup file. A test that cannot fail is
  not a test.
- A plan written days before it runs is prose too, and rots the same way.
  Task 9's own commands carried two bugs — a `{{index .Labels ...}}` template
  that cannot work against `docker ps`, and a volume check missing `ssh home`
  so it silently inspected the laptop and would have read as "the data is
  gone" — and its delete list named `portainer/.env.example`, removed back in
  Task 2. Read each step against reality before running it, not after.

## Next

- Delete `portainer_data` on or after **2026-09-25** — it is the only surviving
  copy of Portainer's stack definitions, kept one month as the migration's last
  undo. `ssh home 'docker volume rm portainer_data'`.
- In the bot repo: pin the Aspire volume name (`WithDataVolume`) and the Aspire
  CLI version, so a CLI update can never re-point the stack at an empty
  database again.
- Komodo Resource Sync: declare the Stacks as TOML in this repo instead of
  creating them by API call, so the control plane's own config is versioned.
  Concrete motivation from 2026-08-25: settling "does `config` have
  `webhook_force_deploy`?" needed an API call against a running Mongo, because
  the only written record (the migration plan's `CreateStack` payload) had
  drifted from reality. Two agents independently suspected a production bug over
  it. With a `stacks.toml` in the repo it is a `grep`, and the record cannot
  drift because it *is* the record. The trade: Komodo's Mongo becomes less
  load-bearing for disaster recovery, but a wrong `project_name` in TOML empties
  volumes exactly like a wrong one in an API call.
- Lock down `registry.sussman.win`, or stop using it. Verified 2026-08-25:
  `GET /v2/` answers 200 with no auth challenge and `/v2/_catalog` returns
  `["alpine","app","traktv-tg-bot/bot"]` to anyone on the internet. Image names
  are public and the images are pullable. Decide between putting it behind
  Cloudflare Access, adding registry auth, or moving those images to GHCR.
- `group-split`'s `ignore_services` lives only in Komodo's Mongo. Decide
  whether to move it into that repo's `deploy.yml` payload so a rebuild keeps it.

## Open questions

- Could `config-agent` eventually disappear entirely, or is a program that
  merges repo config into directories holding runtime state simply necessary?
- Is `cloudflared` on `:latest` a problem worth fixing, given the tunnel is the
  one thing that must never surprise you?
- group-split's `Keycloak__Authority` is a LAN IP
  (`http://172.20.3.194:8080/idp/realms/group-split`), and `keycloak` sits on
  the stack's private network rather than `internal`. So `groupsplit.sussman.win`
  serves the app but cannot complete a browser login from outside the house.
  Fixing it is a change in the group-split repo, not this one — join `keycloak`
  to `internal` and point the authority at a published hostname — but the
  decision worth making first is whether that app should be internet-facing at
  all, or sit behind Cloudflare Access like the registry question above.
