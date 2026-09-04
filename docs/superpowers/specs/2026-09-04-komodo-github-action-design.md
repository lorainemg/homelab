# A Komodo GitHub Action — design

**Date:** 2026-09-04
**Status:** approved, ready to implement (revised 2026-09-04: Python, not bash — see Layout)

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
├── lib/komodo.py             the whole client: call, await_update, expand,
│                             stack_exists, plus a CLI the actions invoke
├── tests/stub_komodo.py      a stand-in Komodo, replaying real response shapes
├── tests/test_komodo.py      unittest suite against that stub
├── update-stack/action.yml
├── deploy-stack/action.yml
└── run-sync/action.yml
```

Each `action.yml` is a composite action whose single `run:` step is three or
four lines of bash that pass the action's inputs through as environment
variables and invoke
`python3 "$GITHUB_ACTION_PATH/../lib/komodo.py" <subcommand>`. Composite
actions cannot share code through a nested `uses:` when referenced across
repos, because a relative `uses:` resolves against the *caller's* workspace.
Reaching a sibling file works because GitHub checks out the whole action
repository, so `$GITHUB_ACTION_PATH/..` is this repo's
`.github/actions/komodo/`.

**Why Python rather than bash.** An earlier draft of this library was written
in bash and reviewed before being discarded. Every defect the review found was
a bash-specific footgun rather than a logic error: a function returning
non-zero silently aborts its caller under `set -e`, so the missing-stack probe
killed the step it was meant to inform; capturing a 500 body depends on
whether `2>&1` precedes or follows `>/dev/null`; and `envsubst` substitutes
every exported name unless handed an explicit list, so a `$` in a compose file
is a hazard. The call-poll-check logic was correct in both versions. Python
removes that whole class of mistake, uses nothing outside the standard
library (`urllib.request`, `json`, `argparse`), and is the language this
repo's owner reads most comfortably — which, in a repo whose stated purpose is
understanding its own infrastructure, is a first-class requirement rather than
a preference. Python 3 is preinstalled on `ubuntu-latest`, and the homelab
server runs 3.13.7.

### Shared values are passed in, not stored

Every action takes a `vars` input: `NAME=value`, one per line. It reaches the
client as `KOMODO_VARS`, and `${NAME}` in a `links` input or in the TOML that
`run-sync` pushes is expanded from it. Nothing on disk holds those values.

The alternative considered and rejected was a `vars.env` file shipped inside
the action directory, which every caller would get for free because GitHub
checks the action's whole repository out on the runner. It removes the
duplication, but at the price of putting a fact about one particular house
inside a tool two repos share — a caller vendoring the action would also
vendor someone's LAN. Each workflow states the values it uses:

```yaml
        with:
          links: http://${HOMELAB_LAN_IP}:28888/login?t=...
          vars: HOMELAB_LAN_IP=172.20.3.194
```

The cost is that the address appears in each repo that deploys to this Komodo,
and a re-addressed LAN means editing both. That is two greppable lines in two
workflows, against a shared tool that no longer knows anything about a
specific network.

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
| `links` | no | newline-separated; `${VAR}` expanded from `vars` |
| `vars` | no | `NAME=value` per line, for the placeholders in `links` |
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
| `contents-file` | yes | TOML file; `${VAR}` expanded from `vars` |
| `vars` | no | `NAME=value` per line, for the placeholders in the TOML |
| `timeout` | no, default `300` | seconds to wait for the Update |

Renders the file, fails if any `${...}` placeholder survives, pushes it with
`repo`, `branch` and `resource_path` explicitly empty, then runs the sync and
waits exactly as `deploy-stack` does.

The CLI also exposes a fourth subcommand, `render <file>`, which prints the
expanded file and talks to nothing. That exists for `scripts/bootstrap.sh`,
which needs the same rendering on a fresh server where no runner and no API
key exist. One renderer, two callers.

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

The library's only outside contact is HTTP against a base URL, so it is
testable without touching the homelab.

**Unit tests against a stub server.** A `http.server` in
`.github/actions/komodo/tests/stub_komodo.py` replays real Komodo response
shapes captured from the live server: a missing stack (500 with the "Did not
find any Stack" body), an accepted-then-failed update, an
accepted-then-succeeded update, and a non-2xx write. It also validates the
request *path* against the request type, so a read sent to `/execute` is a
test failure rather than a silent pass — later work trusts this harness for
exactly that.

`tests/test_komodo.py` is a `unittest` suite run with
`python3 -m unittest discover`. It asserts that `stack_exists` is false for
the 500 body and true otherwise, that a failed update raises and prints its
stage logs, that a timeout raises rather than passing, that placeholder
expansion leaves no `${` behind and does not disturb a bare `$`, that an
update payload omits fields the caller did not supply, and that a failed
request never echoes a value from the request body — that last one driving
the *error* branch, which is the only path where such a leak could occur.
These run in CI on every push that touches the action.

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
  of `vars.env` over `raw.githubusercontent.com` is replaced by the workflow
  stating the value in its own `vars` input.
- PR #6 in this repo is rewritten on its branch. The `stacks.toml` template
  and the repo-to-contents switch survive unchanged; only the inline curl in
  the workflow is replaced by `run-sync`.
- `scripts/bootstrap.sh` renders through the same library, calling
  `python3 .github/actions/komodo/lib/komodo.py render komodo/stacks.toml`.
  It runs on a fresh server with no GitHub runner and no API key, so it cannot
  use the *action*, but it can use the library the action wraps. That removes
  the duplicate renderer an earlier draft of this design accepted as
  unavoidable. A shell script reaching into `.github/` is the visible cost of
  keeping one canonical copy of both the values and the code that expands
  them.
