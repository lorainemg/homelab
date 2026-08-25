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

## Shaky

- Komodo's Resource Sync (stacks declared as TOML in the repo) — deliberately
  untouched so far; it is the obvious next capability after this migration.
- Restoring Komodo's Mongo — now *run* (2026-08-20) and it works, but the
  procedure as originally written tested the wrong thing: it copied from the
  live database instead of opening a backup file. A test that cannot fail is
  not a test.

## Next

- Finish [the migration](docs/superpowers/plans/2026-08-20-portainer-to-komodo-migration.md):
  Tasks 1-8 are done: every stack including `trakt-tg-bot` deploys through
  Komodo, and CI is down to one build job. Task 9 (retire Portainer) is all
  that remains.
- In the bot repo: pin the Aspire volume name (`WithDataVolume`) and the Aspire
  CLI version, so a CLI update can never re-point the stack at an empty
  database again.
- Komodo Resource Sync: declare the Stacks as TOML in this repo instead of
  creating them by API call, so the control plane's own config is versioned.

## Open questions

- Could `config-agent` eventually disappear entirely, or is a program that
  merges repo config into directories holding runtime state simply necessary?
- Is `cloudflared` on `:latest` a problem worth fixing, given the tunnel is the
  one thing that must never surprise you?
