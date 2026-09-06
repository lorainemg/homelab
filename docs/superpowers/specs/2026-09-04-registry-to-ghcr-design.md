# Retiring `registry.sussman.win` in favour of GHCR

**Status:** implemented 2026-09-06 — the bot runs from GHCR; group-split's flip is
Sussman-Club/group-split#163; the decommission is the homelab PR. See the plan for
what the docs got wrong about package visibility.
**Supersedes:** the "Lock down `registry.sussman.win`, or stop using it" entry in
[LEARNING.md](../../../LEARNING.md).

## Why

The self-hosted registry is open to the internet. Verified 2026-08-25: `GET /v2/`
answers 200 with no auth challenge, and `/v2/_catalog` returns
`["alpine","app","traktv-tg-bot/bot"]` to anyone. The image names are public and
the images are pullable.

Three fixes were weighed. What decided it is that **every client of this registry
is a machine**: a GitHub Actions runner pushes, and the server's Docker daemon
pulls. Neither can open a browser, and neither can attach custom HTTP headers to
its requests.

| Option | Verdict |
|---|---|
| Cloudflare Access | Rejected. Machine access uses service tokens, which are HTTP headers Docker cannot send. Every push would need a `cloudflared` proxy wrapped around it — the most moving parts, on the path CI depends on. |
| `htpasswd` auth on the registry or in Caddy | Workable, but keeps a registry, a password file, its rotation, and an internet-facing service that only we patch. |
| **Move the images to GHCR** | **Chosen.** This repo already builds and pushes `config-agent` to GHCR ([.github/workflows/deploy.yml:42](../../../.github/workflows/deploy.yml)), so the path is proven. Removes a component rather than hardening one. |

GHCR is not a service to stand up. A container package exists the moment something
pushes to `ghcr.io/<owner>/<name>`; both owner namespaces already exist.

## Scope

Three repos and the control plane:

| Repo | Change |
|---|---|
| `lorainemg/traktv-tg-bot` | Push to GHCR instead of `registry.sussman.win`; set the Stack's registry fields in its Komodo payload |
| `Sussman-Club/group-split` | Same |
| `lorainemg/homelab` (this repo) | Drop the Caddy route, drop the `docker-registry` Stack, update `LEARNING.md` and `README.md` |
| Komodo | One `[[image_registry]]` credential |

## The push side

Each GitHub Actions run is handed an automatic `GITHUB_TOKEN`, and it can write
packages **only under its own repo's owner**. That single rule assigns both
namespaces with no token to create:

| Repo | Owner | Pushes to |
|---|---|---|
| `lorainemg/traktv-tg-bot` | `lorainemg` | `ghcr.io/lorainemg/traktv-tg-bot/bot` |
| `Sussman-Club/group-split` | `Sussman-Club` | `ghcr.io/sussman-club/group-split/*` |

Two namespaces, deliberately. Consolidating them would mean either transferring a
repo or storing a long-lived org-write PAT in the other repo's secrets; neither
buys anything, because the pull credential can read both.

Each workflow needs `permissions: packages: write` and a `docker/login-action`
step against `ghcr.io` using `${{ github.actor }}` and `${{ secrets.GITHUB_TOKEN }}`,
exactly as this repo's `build-config-agent` job already does.

## Visibility

Packages are **private by default** on first push, in a personal namespace and an
org namespace alike, and this is independent of the linked repo:

> When you first publish a package, the default visibility is private and only you
> can see the package. […] the package automatically inherits the access
> permissions (but not the visibility) of the linked repository.
> — [GitHub docs](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)

**Decision: private, and with inherited permissions removed.**

> *Revised 2026-09-06, after the first push:* private, inheritance **kept**. The
> package came out Public (see the plan, Task 3) and was flipped by hand; once
> Private, an anonymous pull is refused, which settles the question below.
> Removing inheritance would also take away the workflow's automatic push
> access, so it costs a way to break the deploy and buys nothing.

Private alone is not sufficient here, because of the asymmetry in that quote:
*permissions* are inherited from the linked repo even though visibility is not. By
default GHCR does not hold its own access list — it forwards "may this account
pull?" to the linked repository. For `lorainemg/traktv-tg-bot`, which is public,
it is unclear whether that forwarding answers "yes" for every GitHub user. Two
GitHub docs pages read differently on the point (2026-09-04) and the question was
not settled:

> a user who has read access to the linked repository will also have read access
> to the package

versus a private package does not become publicly readable merely by being linked
to a public repository.

Rather than resolve it, remove the dependency: each package's settings allow
**removing inherited permissions**, after which the package keeps an explicit
access list of its own and the repository's visibility stops entering into it.
That is one action per package and it makes the ambiguity irrelevant.

Anonymous pull is still verified after the first push (see Cutover, step 2) — but
now as a check on work already done, not as the thing the design depends on.

## The pull side

Komodo authenticates on the server's behalf; there is no host-level `docker login`
and no credential file for Periphery to find. Two pieces meet:

1. **The credential, once**, as an `[[image_registry]]` block in
   `komodo/registries.config.toml` (domain `ghcr.io`, username `lorainemg`),
   mounted read-only into Core at `/config/registries.config.toml`. The token
   is written as `${GHCR_PULL_TOKEN}`: Komodo's config loader expands `${VAR}`
   from the process environment before parsing, so the file holds no secret
   and the value lives in `komodo/.env`, which Core already reads. Core checks
   Mongo first, then this file (`bin/core/src/helpers/mod.rs`, v2.3.1).

   The UI route (Settings → Providers) was the first draft. It works live,
   without a restart, but the account would exist only in Mongo, the gap
   `LEARNING.md` already lists for `group-split`'s `ignore_services`. Two
   costs taken instead: a Core restart to load the file, and an unset
   variable expanding to an empty token with no error.

2. **Each Stack names it**, via two `StackConfig` fields — *verified present in
   v2.3.1*, the tag pinned in [komodo/docker-compose.yml](../../../komodo/docker-compose.yml):

   ```rust
   /// Used with `registry_account` to login to a registry before docker compose up.
   pub registry_provider: String,
   /// Used with `registry_provider` to login to a registry before docker compose up.
   pub registry_account: String,
   ```

   Set to `registry_provider = "ghcr.io"` and `registry_account = "lorainemg"`.

**Where those fields go matters.** `trakt-tg-bot` and `group-split` are absent from
[komodo/stacks.toml](../../../komodo/stacks.toml) on purpose — each repo's own CI
creates its Stack and writes its `file_contents` and `environment`, and a
declaration here would wipe them on every sync. So the two registry fields belong
in **those repos' Komodo API payloads**, not in this repo's `stacks.toml`. Adding
them here would reintroduce exactly the bug that comment warns about.

## The pull token

One classic PAT with `read:packages` and nothing else, created by `lorainemg`.

It reads `ghcr.io/lorainemg/*` by ownership, and `ghcr.io/sussman-club/*` by org
membership plus the package's inherited repo permissions. **Assumption to verify
on the first org push:** that a `Sussman-Club` package does grant this account
read access without a per-package grant. If it does not, the fix is to add the
account to that package's access list in its settings — not to widen the token.

The token lands only in the server's `komodo/.env` (mode 600), matching this
repo's convention that real secrets live in host `.env` files and never in git.

## Cutover order

The old registry keeps running until GHCR is proven. Nothing here is reversible in
the other direction once the volume is gone, so the order is load-bearing:

1. Create the PAT, put it in the server's `komodo/.env`, and restart Core.
   **This must come first.** Both apps
   are Aspire and declare their registry in a single `AddContainerRegistry(...)`
   call that sets the push target *and* the image names written into the compose
   file Komodo deploys — so the flip changes push and pull in the same commit, and
   there is no window in which both registries serve the same images. A server
   that cannot authenticate when that commit lands has a failed deploy, not a
   fallback.
2. Flip the registry line and add the GHCR login in each repo's CI, together with
   `registry_provider` / `registry_account` in that repo's Stack payload. One
   commit per repo; each is a deploy.
3. Immediately after each first push: confirm the packages are private, remove
   their inherited permissions, and verify an anonymous `docker pull` is denied.
6. Only then, in this repo: remove the `registry.sussman.win` route from
   [config/caddy/Caddyfile](../../../config/caddy/Caddyfile) and the
   `docker-registry` Stack from `komodo/stacks.toml`.
7. Delete the Cloudflare DNS record and its tunnel hostname.
8. Keep the `docker-registry_registry-data` volume for one month as the undo, then
   `docker volume rm docker-registry_registry-data`.

## Verification

- `curl -s https://registry.sussman.win/v2/_catalog` — must fail after step 7.
- `docker logout ghcr.io && docker pull ghcr.io/lorainemg/traktv-tg-bot/bot:<tag>`
  — must be denied. This is the check that proves the leak is closed rather than
  moved.
- The same pull, authenticated — must succeed.
- Both stacks running, from images whose names begin `ghcr.io/`.

## Out of scope

- The `alpine` and `app` images in the old catalogue. No stack references them;
  they die with the volume rather than being migrated.
- `group-split`'s unreachable Keycloak login, tracked separately in `LEARNING.md`.
- This repo's own `config-agent` image, which already lives in GHCR and is
  untouched.
