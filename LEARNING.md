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

## Shaky

- Komodo's Resource Sync (stacks declared as TOML in the repo) — deliberately
  untouched so far; it is the obvious next capability after this migration.
- Restoring Komodo's Mongo — now *run* (2026-08-20) and it works, but the
  procedure as originally written tested the wrong thing: it copied from the
  live database instead of opening a backup file. A test that cannot fail is
  not a test.

## Next

- Exercise a rollback from Komodo's UI — the last unproven verdict criterion.
- Execute
  [the migration design](docs/superpowers/specs/2026-08-20-portainer-to-komodo-migration-design.md).

## Open questions

- Could `config-agent` eventually disappear entirely, or is a program that
  merges repo config into directories holding runtime state simply necessary?
- Is `cloudflared` on `:latest` a problem worth fixing, given the tunnel is the
  one thing that must never surprise you?
