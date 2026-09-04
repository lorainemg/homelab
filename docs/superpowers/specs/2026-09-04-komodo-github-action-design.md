# A Komodo GitHub Action — design

**Date:** 2026-09-04
**Status:** approved, ready to implement

Replace the hand-rolled `curl` that talks to Komodo from CI with three
task-named composite actions in `.github/actions/komodo/`, used by both this
repo and [traktv-tg-bot](https://github.com/lorainemg/traktv-tg-bot).

Today the same API conversation is written out four times in two repos, in
three different shapes, and a fourth path (the `config` stack) uses a signed
webhook instead of the API. About 140 lines of workflow are auth headers,
polling loops and error checks. Half of the bot's deploy workflow is Komodo
boilerplate that has nothing to do with the bot.

## Concepts

Written out because each one is a trap that has already been hit at least
once in this repo.

**`/execute` is accepted-not-done, and that includes authorization.** A call
to `/execute` returns an Update with `status: "InProgress"` immediately. The
work happens afterwards, and so does the permission check: calling
`DeployStack` as a user with no rights on that stack returns `InProgress` and
`error: null`, and the refusal lands in the Update as `success: false`
(verified 2026-09-04). So a 2xx from Komodo never means permitted, and never
means finished. Every caller must poll `GetUpdate` and read `success`. This
is the single biggest reason to write the loop once.

**A missing resource is a 500, not a 404.** `GetStack` on a name Komodo does
not know answers HTTP 500 with `"Did not find any Stack matching ..."`. The
bot's CI already branches on the response *body* for this reason. Any
create-if-missing logic has to do the same.

**Komodo picks a sync's source in a fixed order.** `files_on_host`, then a
linked repo, then `repo`, then the stored `file_contents`
(`bin/core/src/sync/remote.rs`, v2.3.1). Pushing contents into a sync whose
`repo` is still set changes nothing visible: Komodo keeps cloning the repo and
silently ignores what was pushed. So the action must declare `repo`, `branch`
and `resource_path` empty every time it pushes contents, which also makes the
repo-to-contents switch happen on first run.

**`links` is not interpolated by Komodo.** It is outside the seven fields
`lib/interpolate/src/lib.rs` substitutes into, and the sync applies its TOML
verbatim. The UI hands each link string straight to an anchor
(`ui/src/pages/resource.tsx`). So a placeholder in a link has to be rendered
by whoever writes the link, which is what makes the substitution below the
action's job rather than Komodo's.

## The design

### Layout

```
.github/actions/komodo/
├── vars.env              values shared by everything that talks to this Komodo
├── lib/komodo.sh         auth, call, await_update, resolve_vars, stack_exists
├── update-stack/action.yml
├── deploy-stack/action.yml
└── run-sync/action.yml
```

Each `action.yml` is a composite action whose single `run:` step sources
`$GITHUB_ACTION_PATH/../lib/komodo.sh`. Composite actions cannot share code
through a nested `uses:` when referenced across repos, because a relative
`uses:` resolves against the *caller's* workspace. Sourcing a sibling file
works because GitHub checks out the whole action repository, so
`$GITHUB_ACTION_PATH/..` is this repo's `.github/actions/komodo/`.

### `vars.env` moves into the action directory

`komodo/vars.env` becomes `.github/actions/komodo/vars.env`, holding
`HOMELAB_LAN_IP` and anything later shared by all callers.

This is the whole reason the LAN address stops being duplicated. When the bot
repo uses this action, GitHub checks this repo out on the runner, so
`vars.env` is physically present next to the action code. The action expands
`${HOMELAB_LAN_IP}` in its `links` input and in the TOML that `run-sync`
pushes. A caller writes the placeholder literally and fetches nothing:

```yaml
links: http://${HOMELAB_LAN_IP}:28888/login?t=...
```

The trade: a value that is conceptually infrastructure config now lives under
`.github/`. That is accepted because its only readers are the action and the
workflows that call it, and one canonical location was the point.

### The three actions

**`update-stack`** — pushes a stack's definition, creating the stack if it is
missing.

| Input | Required | Meaning |
|---|---|---|
| `stack` | yes | stack name |
| `compose-file` | no | file whose contents become `file_contents` |
| `env-file` | no | file whose contents become `environment` |
| `links` | no | newline-separated; `${VAR}` expanded from `vars.env` |
| `create-if-missing` | no, default `false` | create the stack before updating |
| `server` | no, default `Local` | server for the create path only |

Omitted optional inputs are left out of the payload entirely, so the action
never resets a field the caller did not mention. With `create-if-missing`, it
calls `GetStack`, treats `"Did not find any Stack"` in the body as absent,
and calls `CreateStack` with `file_contents: "services: {}"` before updating.

**`deploy-stack`** — deploys and waits.

| Input | Required | Meaning |
|---|---|---|
| `stack` | yes | stack name |
| `timeout` | no, default `300` | seconds to wait for the Update |

Calls `DeployStack`, polls `GetUpdate` until `Complete`, fails the step unless
`success` is true, and prints the Update's stage logs on failure. This also
replaces the `config` stack's signed webhook, so `KOMODO_WEBHOOK_SECRET`
stops being a CI secret in this repo and the `openssl dgst` HMAC step goes
away.

**`run-sync`** — pushes a ResourceSync's contents and runs it.

| Input | Required | Meaning |
|---|---|---|
| `sync` | yes | sync name |
| `contents-file` | yes | TOML file; `${VAR}` expanded from `vars.env` |
| `timeout` | no, default `300` | seconds to wait for the Update |

Renders the file, fails if any `${...}` placeholder survives, pushes it with
`repo`, `branch` and `resource_path` explicitly empty, then runs the sync and
waits exactly as `deploy-stack` does.

### Authentication

All three take `komodo-url`, `api-key` and `api-secret` inputs, sent as
`X-Api-Key` and `X-Api-Secret`. No action ever logs a payload, because stack
environments contain secrets; the library prints request *types* and Komodo's
own log stages, never bodies.

### Error handling

One code path for every call:

1. A non-2xx HTTP status fails the step immediately, printing the response
   body (Komodo puts a readable `error` field there).
2. For `/execute`, poll `GetUpdate` every 5 seconds until `status` is
   `Complete` or the timeout expires. A timeout fails the step.
3. On `success: false`, print each log's `stage`, `stdout` and `stderr`, then
   fail. This is the improvement over today's raw JSON dump, which is
   unreadable in a run log.

## Testing

The library is plain bash whose only outside contact is `curl` against a base
URL, so it is testable without touching the homelab.

**Unit tests against a stub server.** A small Python `http.server` in
`.github/actions/komodo/tests/` replays real Komodo response shapes captured
from the live server: a missing stack (500 with the "Did not find any Stack"
body), an accepted-then-failed update, an accepted-then-succeeded update, and
a non-2xx write. The tests assert that `stack_exists` is false for the 500
body, that a failed update fails the step and prints its stage logs, that a
timeout fails rather than passes, and that placeholder expansion leaves no
`${` behind. These run in CI on every push that touches the action.

**One live smoke test.** After merge, a `workflow_dispatch` run exercises
`run-sync` against the real server, whose rendered output must produce a
pending diff of zero resource updates — the file is byte-identical to what is
already applied. Verified by reading `.info.resource_updates` on
`GetResourceSync`, which is the correct path; `.info.pending.data` does not
exist and silently reads as zero, a false negative that has already fooled one
verification in this repo.

## Consequences

- This repo's CI key needs Execute on the `config` stack, in addition to the
  Write on the `homelab` sync it already has. `KOMODO_WEBHOOK_SECRET` is no
  longer needed by CI, though the per-stack GitHub webhooks that Komodo
  listens on are unaffected.
- PR #12 in the bot repo is superseded and gets closed: the cross-repo fetch
  of `vars.env` over `raw.githubusercontent.com` is replaced by the action's
  own substitution.
- PR #6 in this repo is rewritten on its branch. The `stacks.toml` template
  and the repo-to-contents switch survive unchanged; only the inline curl in
  the workflow is replaced by `run-sync`.
- `scripts/bootstrap.sh` keeps its own rendering, reading the vars file from
  its new path under `.github/actions/komodo/`. It runs on a fresh server with
  no GitHub runner and no API key, so it cannot use the action, and the
  duplication is deliberate: bootstrap must work when CI does not. A shell
  script reaching into `.github/` is the visible cost of keeping one canonical
  copy of the values.
