# Floci evaluation — design

**Date:** 2026-08-08
**Status:** approved, ready to implement

Stand up [floci](https://github.com/floci-io/floci) — a local AWS emulator — as
a new non-load-bearing stack, and run one narrow experiment against it: **can
its emulated ECS keep a container alive?**

This is not a migration and not a control-plane evaluation. It is a single
falsifiable question with a purpose-built canary, a fixed list of kill criteria,
and an exit date. Nothing in the running homelab depends on the answer.

The motivation is learning AWS. The experiment exists because the obvious next
question — "could this host my apps?" — deserves an answer from evidence rather
than from argument.

## Concepts

Written out because the whole design turns on a distinction that is easy to
miss.

**Emulator vs. control plane.** Portainer and Komodo are *control planes*: they
start containers and keep them running. Floci is an *API emulator*: it answers
HTTP requests that look like AWS. When you call `s3:PutObject` against it, it
replies with what real S3 would reply. These are different categories of
software, and the difference is not fidelity — it is scope. An emulator
reproduces what a service **says**; a platform reproduces what it **does**.

**Reconciliation.** The property that actually separates the two. A control
plane holds a declared desired state and continuously works to make reality
match it: *this container should be running, so if it dies, start another*.
Software without a reconciliation loop can still start containers — it just
never notices when they stop. Whether floci's ECS reconciles is the entire
question this evaluation answers, and it cannot be inferred from its service
list, because a service list describes API coverage, not behaviour.

**ECS's four nouns.** Amazon's container service names things differently from
compose, and the mapping is worth internalising because it is most of what this
experiment teaches:

| ECS | Compose equivalent | What it is |
| --- | --- | --- |
| Task definition | a `services:` entry | Immutable, versioned template: image, memory, env, ports |
| Task | a running container | One live instance of a task definition |
| Service | `deploy: replicas:` + restart policy | The controller that keeps N tasks running |
| Cluster | the host | Where tasks are placed |

The *service* is the reconciling part. `--desired-count 1` is a promise, not an
instruction — the point is what happens after the task dies.

**Why floci needs the Docker socket.** Floci's higher-fidelity services (Lambda,
ECS, EKS, RDS, ElastiCache, OpenSearch) are not reimplementations — they launch
*real* Docker containers. RDS gives you an actual Postgres. That fidelity is
only achievable by handing floci the ability to create containers, which means
mounting `/var/run/docker.sock`. Anything holding that socket can start a
container that mounts the host filesystem, so it is root-equivalent on the host.
This is an acceptable trade on a laptop and a deliberate one here — see
[Security](#security).

**Dev compose vs. release image.** Floci's and floci-ui's `docker-compose.yml`
files at their repo roots are *contributor* artifacts, not install instructions:
they use `build:`, `Dockerfile.dev`, and source bind-mounts for hot reload.
Copying them would mean compiling a pnpm/bun monorepo on the server. The
deployable definitions are the published images. In particular, floci-ui's dev
compose splits into `floci-ui` (Vite dev server, 4500) and `floci-api` (4501)
purely so the frontend can hot-reload — **there is no published `floci/floci-api`
image, and none is needed.** The release `docker/Dockerfile` is a three-stage
build that compiles the frontend, compiles the API, and combines them into one
Alpine image whose entrypoint is the API binary serving the built frontend from
`./public` on port 4500.

## Goals

- Answer, with evidence, whether floci's emulated ECS reconciles a dead task.
- Learn the ECS vocabulary by using it against something real.
- Get floci and its console running in the homelab as an AWS sandbox, which
  stays useful regardless of how the experiment turns out.

## Non-goals

- Replacing Portainer or Komodo. Floci is not a candidate for that, and the
  Portainer/Komodo evaluation (ending 2026-08-20) proceeds untouched.
- Moving any real workload. Not Immich, not Home Assistant, not the Trakt bot.
- Exposing floci to the internet in any form.
- Deciding anything about the Trakt bot's deploy pipeline. That conversation is
  downstream of this result and is explicitly deferred.

## Architecture

A new `floci/` stack, deployed by Komodo via GitHub webhook exactly like
[registry/](../../../registry/).

```text
floci/
├── docker-compose.yml     floci + floci-ui, pinned images
├── .env.example           credential names, no values (.env is gitignored)
└── canary/
    ├── main.go            the test service
    └── Dockerfile         golang builder → alpine runtime
```

### Why Komodo deploys it, not the host

[portainer/](../../../portainer/) and [komodo/](../../../komodo/) are
host-managed because CI deploys *through* them — a bad deploy must not be able
to break the thing performing deploys. Floci is not in that path. Nothing
deploys through it and nothing depends on it, so the rule does not apply.

Deploying it through Komodo is a small bonus: it gives the Komodo evaluation a
second real stack to manage alongside `docker-registry`, at no cost.

**This changes if the experiment succeeds.** The moment floci hosts anything
real, you have Komodo deploying floci and floci hosting workloads — and
redeploying the floci stack from Komodo's UI would destroy every container floci
spawned. That is the point at which floci must become host-managed. Recorded
here so it is a decision later rather than an outage.

### The stack

```yaml
services:
  floci:
    image: floci/floci:1.6.0
    restart: unless-stopped
    ports:
      - "4566:4566"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - floci-data:/app/data
    environment:
      FLOCI_STORAGE_MODE: persistent
      FLOCI_SERVICES_DOCKER_NETWORK: floci_default
      FLOCI_HOSTNAME: floci
      FLOCI_TLS_ENABLED: "false"
      FLOCI_AUTH_VALIDATE_SIGNATURES: "true"
    networks:
      floci_default:
        aliases:
          - localhost.floci.io

  floci-ui:
    image: floci/floci-ui:0.2.0
    restart: unless-stopped
    depends_on: [floci]
    ports:
      - "127.0.0.1:4500:4500"
    environment:
      FLOCI_ENDPOINT: http://floci:4566
      AWS_REGION: us-east-1
      AWS_ACCESS_KEY_ID: ${FLOCI_ACCESS_KEY_ID}
      AWS_SECRET_ACCESS_KEY: ${FLOCI_SECRET_ACCESS_KEY}
    networks: [floci_default]

volumes:
  floci-data:

networks:
  floci_default:
    name: floci_default
```

Credentials are not in the file and there is no `env_file`: Komodo's periphery
deploys from a git clone, and `.env` is gitignored, so the file would not exist
where compose runs. The `${FLOCI_*}` references are filled by compose
interpolation — from the Komodo Stack's `environment` field when deployed, or
from `floci/.env` (which compose reads automatically for interpolation) when
brought up by hand. `floci-data` is a named volume rather than upstream's
`./data` bind for the same reason: a relative path would resolve inside
periphery's clone volume, where a re-clone could destroy it.

Five details that are load-bearing:

**4566 is published to the LAN; nothing else is.** Upstream's compose publishes
~140 host ports (`4566`, `6379-6399`, `7001-7099`, `9200-9299`). The ranges stay
unpublished — nothing connects to emulated RDS/ElastiCache/OpenSearch from
outside yet, and `6379` would collide with Immich's Valkey. Only the API port is
exposed, deliberately, so the AWS CLI can be driven from a laptop and other
machines or CI can reach it later without a tunnel.

**This makes `FLOCI_AUTH_VALIDATE_SIGNATURES` load-bearing rather than defence in
depth.** With 4566 on the LAN, the SigV4 secret is the only thing between any
device on the network — including a webpage open in any browser in the house,
which can POST to LAN addresses — and a service that can launch containers
mounting the host filesystem. Two consequences follow, and neither is optional:

- **No `test`/`test`.** Credentials are randomly generated, kept in `floci/.env`
  (gitignored, with a committed `.env.example`), following the same pattern as
  [komodo/](../../../komodo/) and [home-assistant/](../../../home-assistant/).
- **Validation must be proven working before the port goes on the LAN.** See
  [Implementation order](#implementation-order).

The UI stays on `127.0.0.1` regardless: it holds those credentials server-side
and has no login, so LAN-exposing 4500 would hand over everything signature
validation is protecting.

**`name: floci_default` must stay.** Komodo deploys with a compose project name,
which normally prefixes networks to `<project>_floci_default`.
`FLOCI_SERVICES_DOCKER_NETWORK` is a hardcoded literal handed to the containers
floci spawns — if the network gets prefixed and the variable does not, spawned
containers land where floci cannot reach them. The explicit `name:` pins it.
Same class of hazard as the `project_name` pin in the Komodo design.

**`FLOCI_TLS_ENABLED: "false"`.** Upstream defaults it on; it costs cert trust
configuration in every SDK client. Off is defensible here but is a real
trade now that 4566 is on the LAN: SigV4 never puts the secret on the wire, so
plaintext does not leak credentials, but it does leak *payloads* to anyone
sniffing the network. Acceptable while the only traffic is an nginx canary.
**Revisit before anything real flows through it** — the moment Secrets Manager
holds a live token or S3 holds anything private, this should be on.

**The UI's four environment variables are mandatory.** The release Dockerfile
sets only `PORT`. The API talks to floci through the AWS SDK, which refuses to
sign a request without credentials even against a fake endpoint. Omit them and
the page loads fine while every panel sits empty — a miserable thing to debug.

**Pinned tags, no `latest`.** Consistent with Immich v3, Caddy 2.8 and Komodo
2.3.1. Note that floci publishes parallel `-compat` tags (`1.6.0-compat`) and
floci-ui's own compose references `latest-compat`; what `-compat` denotes is not
documented anywhere found. If the UI misbehaves against `1.6.0`, try
`1.6.0-compat` before debugging further.

### The canary service

A ~40-line Go HTTP service. Built for this experiment rather than borrowed,
because reconciliation is invisible unless the workload tells you which instance
you are talking to.

| Route | Returns | Why |
| --- | --- | --- |
| `GET /` | hostname, PID, start time, uptime | A new hostname and reset uptime is the proof a replacement happened |
| `GET /healthz` | `200 OK` | What an ECS health check targets |
| `GET /crash` | exits non-zero | Triggers failure from inside, on demand |

`/crash` is why this is not nginx. Killing a container from outside conflates
"floci noticed" with "Docker's own restart policy noticed". Crashing the process
from within, then asking whether a task with a *different hostname* appears,
isolates the reconciliation loop as the only possible cause.

The `/` handler's payload is the instrument the whole experiment reads, so it is
written by hand rather than scaffolded — what it reports determines what the
experiment can prove.

**The image is built on the server** with `docker build`, tagged locally, and
never pushed. Floci launches containers through the host's Docker daemon, so a
local tag resolves without a registry. This deliberately sidesteps whether
floci's ECS supports `repositoryCredentials` for `registry.sussman.win` —
one unknown at a time. If the canary passes, private registry pulls become the
next experiment rather than a confound in this one.

## The experiment

### Working against it

Driven from the laptop with the real AWS CLI. Since 4566 is on the LAN, no
tunnel is needed for the API — point straight at the server. The console still
requires one, because 4500 stays on loopback:

```bash
ssh -L 4500:localhost:4500 <server>     # only for the UI
```

Configure the API as a named profile rather than exporting environment
variables, so it cannot leak into a shell that later talks to real AWS —
`~/.aws/config`:

```ini
[profile floci]
region = us-east-1
endpoint_url = http://<server>:4566
```

with matching keys under `[floci]` in `~/.aws/credentials`. Then every command is
`--profile floci`, and forgetting the flag fails loudly instead of quietly doing
something somewhere else.

```bash
aws --profile floci ecs create-cluster --cluster-name homelab
aws --profile floci ecs register-task-definition \
  --family canary \
  --container-definitions '[{"name":"web","image":"canary:local","memory":128}]'
aws --profile floci ecs create-service --cluster homelab --service-name canary \
  --task-definition canary --desired-count 1
```

The `canary:local` image is built on the server, so it must exist in the host's
Docker daemon before `create-service` runs — the CLI call is remote, but the
image reference is resolved locally by floci.

### Kill criteria

Fixed before the experiment runs, so a failure reads as data rather than as
something to rationalise.

1. Does `create-service --desired-count 1` produce a running container at all?
2. **Hit `/crash`. Does a container with a new hostname appear?**
3. Does `aws ecs describe-services` report desired and running counts that match
   what `docker ps` shows?
4. Do services survive restarting the floci stack?
5. Do they survive a host reboot?
6. Does any of it appear in floci-ui?

**Criterion 2 is the evaluation.** The rest are texture.

Criterion 6 is expected to fail: floci-ui's documented panels are S3, EC2,
Lambda, EKS, VPC, RDS and Secrets Manager — ECS is not among them. Confirming it
costs ten seconds and settles whether the console could ever show a hosted
workload.

### Outcomes

**If criterion 2 fails** — floci is an AWS sandbox, not a control plane. Record
it in this document, keep the stack for learning the API surface, and close the
hosting question. Total cost: one afternoon and one throwaway container.

**If criterion 2 passes** — the next experiment is private registry pulls
(`repositoryCredentials` against `registry.sussman.win`), and floci must be
converted to host-managed *before* anything real moves onto it. Promotion is a
separate design; nothing in this one implies it.

## Implementation order

The LAN binding is safe only if signature validation actually works, and
"working" is not observable from a successful API call — an unvalidated floci
answers happily too. So the port comes last:

1. Bring the stack up with `ports: ["127.0.0.1:4566:4566"]` and validation on.
2. Confirm a correctly-signed call **succeeds** (`aws --profile floci sts
   get-caller-identity`).
3. Confirm a call with a **wrong secret fails**. This is the actual test — if a
   bad secret still succeeds, validation is not in effect and the LAN binding
   must not be applied.
4. Confirm the UI's panels populate with the real credentials.
5. Only then change the bind to `4566:4566` and redeploy.

Step 3 is the one worth being pedantic about. Every other step passes whether or
not validation is on, so skipping it means discovering the setting never took
effect at the worst possible moment.

## Security

Floci holds the Docker socket, which makes anything able to call it
root-equivalent on the host: a caller can create a Lambda or ECS task, have
floci launch a container mounting the host filesystem, and own the machine. On a
laptop the blast radius is the laptop. Here it is the machine holding Immich's
photo library and Home Assistant.

**Floci does support authentication, and this design enables it.** By default it
does not validate SigV4 signatures — the access key ID is used only to partition
accounts, and any secret is accepted. `FLOCI_AUTH_VALIDATE_SIGNATURES=true`
turns on real signature verification, so a caller must hold the secret key to
produce a valid request. That is genuine authentication and it is set above.

It is not, however, sufficient to justify exposing the service, for three
reasons:

1. **There is no IAM policy enforcement.** Floci partitions resources by account
   but never denies a call based on a policy. Authentication is therefore
   all-or-nothing: one valid credential grants every action, including the
   container-launching ones. The secret key is not an API credential, it is the
   root password for this server.
2. **The UI is a credentialed proxy and bypasses it entirely.** floci-ui holds
   the access key server-side and signs on the caller's behalf, and has no login
   of its own. Anyone who reaches port 4500 acts with full credentials no matter
   how strictly floci validates signatures.
3. **It is an opt-in control, off by default, in a six-month-old project.** Fine
   as a layer; not something to make the only barrier between the internet and
   root on the box holding the photo library.

Enabling it is still worth doing, and not only for defence in depth: with
validation off you learn the habit that any credentials work, which is precisely
the wrong instinct to carry to real AWS.

Therefore:

- **No Caddy route, no tunnel hostname, no DNS entry.** Not now, not "temporarily".
- **4566 is on the LAN by choice; the SigV4 secret is the control.** Accepted
  deliberately so the CLI, other machines and CI can reach it without a tunnel.
  Note that a host firewall is *not* a fallback here: Docker inserts its
  iptables rules ahead of ufw, so published ports are reachable regardless of
  ufw rules. The interface binding and the credential are the only controls.
- **4500 stays on `127.0.0.1`.** Verified in `packages/api/src/index.ts`: the
  server registers CORS and a logger, then mounts its routes with **no
  authentication guard of any kind** — no session handling exists anywhere in
  the repo. The UI signs requests with credentials it holds server-side, so
  reaching it is equivalent to holding them, and LAN-exposing it would defeat
  signature validation entirely.
- **Bring the UI's SSH forward up only when using it, then drop it.** That same
  file sets `Access-Control-Allow-Origin: *` on every route except
  `/api/secretsmanager`. With no auth behind it, any page open in the browser
  can call those endpoints and read the responses — and while the forward is
  live, `http://localhost:4500` is reachable from that browser. CORS is enforced
  by browsers against pages; it is not an access control and stops nothing else.
- **The UI binds `127.0.0.1:4500` only**, reached with
  `ssh -L 4500:localhost:4500 <server>`. It signs requests with credentials it
  holds server-side and has no login of its own, so reaching it *is* holding the
  credentials.
- If phone access is ever wanted, the only acceptable route is Caddy behind
  Cloudflare Access — the header pattern already solved during the Komodo work.

The canary's `/crash` endpoint is reachable only from inside `floci_default` and
does nothing but exit; it is not an exposure.

## Interaction hazards

**Komodo's "Stop All Containers".** [komodo/docker-compose.yml](../../../komodo/docker-compose.yml)
labels Mongo `komodo.skip:` so one misclick cannot stop the database. Containers
floci spawns are created directly through the socket and carry **no such label** —
they appear to Komodo as unmanaged strays and a sweep would take them. This is
survivable precisely because the canary is disposable, and is a strong argument
against putting anything valuable behind floci while it is Komodo-deployed.

**Volume growth.** `FLOCI_STORAGE_MODE: persistent` writes to `./data`. Emulated
RDS/OpenSearch containers are real engines with real disk appetite. Worth a look
at `docker system df` before and after.

## Timebox

**Decision date: 2026-09-05.**

Two weeks after the Komodo evaluation ends on 2026-08-20, deliberately — judging
two infrastructure changes in the same week produces one bad decision and one
unexamined one. On 2026-09-05 either the hosting question is closed, or a
promotion design exists. If neither, the stack comes down.

## Open questions

Recorded rather than guessed, to be answered during implementation:

- How is the expected secret registered for an access key ID under
  `FLOCI_AUTH_VALIDATE_SIGNATURES=true`? See
  `docs/configuration/multi-account.md` — an account map is likely required.
  This is a **prerequisite**, not a curiosity: if validation cannot be made to
  work, the LAN binding is not acceptable and the design falls back to loopback
  plus an SSH forward. Empty UI panels are the expected symptom of getting it
  wrong.
- Does `FLOCI_AUTH_PRESIGN_SECRET` need setting for anything used here? It
  governs pre-signed URL verification, which the canary does not exercise.
- What does the `-compat` tag suffix denote? Undocumented; floci-ui's own compose
  prefers it.
- Does floci's ECS implement `repositoryCredentials`? Deferred out of this
  experiment on purpose.
- Does emulated ECS support task volumes? Irrelevant to a stateless canary,
  decisive for anything real.
- Floci's Docker Hub tag list is dominated by nightlies and the project is
  roughly six months old. Treat that as a maturity signal when weighting results.
