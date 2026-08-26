# Homelab — working agreement

## This is a learning project

I'm using this repo to *learn* infrastructure — Docker and Compose, reverse
proxies, CI/CD, observability, container orchestration — not to get features
shipped as fast as possible. The infrastructure being useful is a happy
side effect; me understanding it is the point.

**Use the `teaching-while-building` skill for anything substantive here.** In
short, that means:

- One idea per turn, then stop and ask. Long explanations lose me.
- Explain the concept *before* asking me to pick between options — define the
  terms, don't just present a decision.
- Hand me the decisions that shape things (which stack owns what, how a
  failure should behave, retention and backup policy). Write the boilerplate
  yourself (compose scaffolding, Dockerfiles, CI yaml, repetitive config).
- Keep `LEARNING.md` at the repo root current: read it when a session starts,
  update it when a concept lands or a new gap shows up.

**Exception:** when I ask for something to be *fixed* — a service is down, a
deploy broke, a secret leaked — just fix it. Don't teach at me mid-incident.

## Don't duplicate the README

[README.md](README.md) is the source of truth for architecture, the stack
table, the CI/CD pipeline, and rebuild-from-scratch steps. Read it before
proposing changes; update it *there* when the infrastructure changes, not here.

## Things that bite

- **Pushing to `main` deploys.** `.github/workflows/deploy.yml` builds the
  affected images and pushes them to the live server on every push. Treat a
  commit to main as a production deploy — no "just committing this to save it".
- **The server is remote.** Docker/compose commands run from this repo touch
  *this machine*, not the homelab. To act on the real thing, go through the
  Komodo API (see the `komodo-api-access` memory) or SSH.
- **Two stacks are host-managed and never deployed:** `tunnel/` (holds the
  Cloudflare tunnel — a bad deploy of the stack holding the tunnel would sever
  the path used to repair it, Komodo's own UI included) and `komodo/` (the
  control plane itself). Neither appears in `.github/workflows/deploy.yml` nor
  as a Komodo Stack. Every other stack is deployed by Komodo from this repo.
- **Secrets never enter git.** `.env` files are gitignored; committed
  `deploy.env` files hold only non-secret variables; real stack secrets live
  in root-owned host `.env` files. GitHub Actions holds only the Komodo URL
  and webhook secret. Enable the gitleaks hook once per clone:
  `cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`
- **Verify before writing down a verdict.** When an experiment says "X doesn't
  work", read X's source and try an alternate config before recording it —
  this repo has already recorded one wrong verdict that way (see the floci
  commits).

## Evaluations and experiments

Design specs and plans for infrastructure experiments live in
`docs/superpowers/specs/` and `docs/superpowers/plans/`. When an evaluation
concludes, record the verdict *and the reasoning* in the repo — the whole
value of the experiment is the part I'll have forgotten in three months.
