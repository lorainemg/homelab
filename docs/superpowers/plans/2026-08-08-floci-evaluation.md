# Floci Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up floci + its console as a Komodo-deployed stack and run the ECS reconciliation experiment defined in [the spec](../specs/2026-08-08-floci-evaluation-design.md).

**Architecture:** One new `floci/` stack (two published images, no Dockerfile), a ~40-line Go canary built on the server and never pushed, and a staged rollout: deploy on loopback → prove signature validation rejects bad secrets → flip 4566 to the LAN → run the experiment.

**Tech Stack:** Docker Compose, Komodo (stack + GitHub webhook), Go stdlib, AWS CLI v2.

## Global Constraints

- Images pinned exactly: `floci/floci:1.6.0`, `floci/floci-ui:0.2.0`. Never `latest`.
- `FLOCI_AUTH_VALIDATE_SIGNATURES: "true"` — never turned off to make something work. If validation cannot be made to work, the fallback is loopback + SSH forward, not disabling it.
- The network is pinned `name: floci_default` and must match `FLOCI_SERVICES_DOCKER_NETWORK`.
- `floci-ui` binds `127.0.0.1:4500` in every task. It never gets a LAN bind, a Caddy route, or a tunnel hostname.
- 4566 binds `127.0.0.1` until Task 6, and moves to the LAN only after Task 5's wrong-secret test fails a request.
- Credentials live in the Komodo Stack `environment` field and (for manual runs) `floci/.env`, which `.gitignore` already covers via `*/.env`. Nothing secret is committed — gitleaks runs pre-commit and must stay clean.
- The canary image is `canary:local`, built on the server, never pushed to any registry.
- Commit messages: single line, casual, no trailers.
- User checkpoint: the canary's `/` handler is written by Loraine (Task 2, Step 4). Do not write it for them.

---

### Task 1: The floci stack files

**Files:**
- Create: `floci/docker-compose.yml`
- Create: `floci/.env.example`

**Interfaces:**
- Produces: compose interpolation variables `FLOCI_ACCESS_KEY_ID`, `FLOCI_SECRET_ACCESS_KEY` (Task 4 supplies values via Komodo; Task 5 uses the same values in `~/.aws/credentials`), service DNS names `floci:4566` and `floci-ui:4500` on network `floci_default`.

- [ ] **Step 1: Write `floci/docker-compose.yml`**

```yaml
# Floci — local AWS emulator + console, under evaluation until 2026-09-05.
# See docs/superpowers/specs/2026-08-08-floci-evaluation-design.md.
#
# Deployed by Komodo (stack `floci`) on a GitHub webhook, like registry/.
# Credentials come from Komodo's stack environment (or floci/.env by hand);
# there is no env_file because periphery's git clone never contains .env.
services:
  floci:
    image: floci/floci:1.6.0
    restart: unless-stopped
    # Loopback until signature validation is proven to reject bad secrets
    # (see the spec's "Implementation order"), then flipped to "4566:4566".
    ports:
      - "127.0.0.1:4566:4566"
    volumes:
      # Floci launches real containers for ECS/Lambda/RDS — root-equivalent,
      # which is why the SigV4 secret below is load-bearing.
      - /var/run/docker.sock:/var/run/docker.sock
      # Named volume, not upstream's ./data: a relative bind would land inside
      # periphery's clone volume, where a re-clone could destroy it.
      - floci-data:/app/data
    environment:
      FLOCI_STORAGE_MODE: persistent
      # Hardcoded literal handed to spawned containers; must equal the pinned
      # network name below or spawned containers land where floci can't reach.
      FLOCI_SERVICES_DOCKER_NETWORK: floci_default
      FLOCI_HOSTNAME: floci
      # Revisit before anything private flows through it — see the spec.
      FLOCI_TLS_ENABLED: "false"
      # The only barrier between the LAN and the Docker socket.
      FLOCI_AUTH_VALIDATE_SIGNATURES: "true"
    networks:
      floci_default:
        aliases:
          - localhost.floci.io

  floci-ui:
    image: floci/floci-ui:0.2.0
    restart: unless-stopped
    depends_on: [floci]
    # Loopback forever: the console holds the credentials and has no login,
    # so reaching it is holding them. ssh -L 4500:localhost:4500 to use it.
    ports:
      - "127.0.0.1:4500:4500"
    environment:
      FLOCI_ENDPOINT: http://floci:4566
      # All four are mandatory: the release image sets only PORT, and the AWS
      # SDK refuses to sign without credentials — omit them and every panel
      # sits empty with no error.
      AWS_REGION: us-east-1
      AWS_ACCESS_KEY_ID: ${FLOCI_ACCESS_KEY_ID}
      AWS_SECRET_ACCESS_KEY: ${FLOCI_SECRET_ACCESS_KEY}
    networks: [floci_default]

volumes:
  floci-data:

networks:
  floci_default:
    # Pinned so Komodo's project name can't prefix it away from
    # FLOCI_SERVICES_DOCKER_NETWORK above.
    name: floci_default
```

- [ ] **Step 2: Write `floci/.env.example`**

```bash
# Compose interpolation for manual bring-up (Komodo deploys supply these via
# the stack's environment field instead). Copy to floci/.env and fill in.

# Exactly 12 digits — floci treats a 12-digit access key ID as the account ID.
FLOCI_ACCESS_KEY_ID=000000000000

# The SigV4 secret. This is effectively root on the host once 4566 is on the
# LAN, so generate it: openssl rand -hex 24
FLOCI_SECRET_ACCESS_KEY=replace-me
```

- [ ] **Step 3: Verify the compose file parses and interpolates**

Run:

```bash
cd /mnt/Data/work/homelab
FLOCI_ACCESS_KEY_ID=000000000000 FLOCI_SECRET_ACCESS_KEY=x \
  docker compose -p floci -f floci/docker-compose.yml config --quiet && echo OK
```

Expected: `OK` and nothing else. A warning about unset variables means an interpolation name is misspelled.

- [ ] **Step 4: Verify `.env` stays ignored**

Run: `touch floci/.env && git check-ignore floci/.env && rm floci/.env`
Expected: prints `floci/.env` (the existing `*/.env` rule covers it).

- [ ] **Step 5: Commit**

```bash
git add floci/docker-compose.yml floci/.env.example
git commit -m "add the floci stack"
```

---

### Task 2: The canary service

**Files:**
- Create: `floci/canary/go.mod`
- Create: `floci/canary/main.go`
- Create: `floci/canary/main_test.go`
- Create: `floci/canary/Dockerfile`

**Interfaces:**
- Produces: HTTP on `:8080` — `GET /` (JSON including a `"hostname"` field — the experiment's instrument), `GET /healthz` (200 `ok`), `GET /crash` (process exits 3). Handler names: `rootHandler`, `healthzHandler`, `crashHandler`, all `func(http.ResponseWriter, *http.Request)`. Task 7 depends on `"hostname"` changing when ECS replaces the task.

- [ ] **Step 1: Write `floci/canary/go.mod`**

```
module canary

go 1.26
```

- [ ] **Step 2: Write the failing test `floci/canary/main_test.go`**

```go
package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

func TestHealthzReturns200(t *testing.T) {
	rec := httptest.NewRecorder()
	healthzHandler(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz: got %d, want 200", rec.Code)
	}
}

// The experiment reads "hostname" from / to prove a replacement task is a new
// container. Whatever else the handler reports, this field is load-bearing.
func TestRootReportsThisHostname(t *testing.T) {
	rec := httptest.NewRecorder()
	rootHandler(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("root: got %d, want 200", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("root: body is not JSON: %v", err)
	}
	host, _ := os.Hostname()
	if body["hostname"] != host {
		t.Fatalf("root: hostname = %v, want %q", body["hostname"], host)
	}
}

// crashHandler is deliberately untested: it calls os.Exit, which would kill
// the test process. A subprocess harness costs more than the three lines it
// would cover; Task 7 exercises it for real.
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd floci/canary && go test ./...`
Expected: compile FAIL — `undefined: healthzHandler`, `undefined: rootHandler`.

- [ ] **Step 4: Write `floci/canary/main.go` — scaffold, then Loraine writes the `/` handler**

Write this file exactly, with the TODO left in place:

```go
// Canary for the floci ECS-reconciliation experiment: a server whose only job
// is to identify which instance of itself is answering, and to die on demand.
// See docs/superpowers/specs/2026-08-08-floci-evaluation-design.md.
package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"time"
)

var startTime = time.Now()

func healthzHandler(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	fmt.Fprintln(w, "ok")
}

// crashHandler kills the process from the inside. Killing from outside
// (docker kill) would conflate floci noticing with Docker's restart policy
// noticing; a non-zero exit from within isolates ECS reconciliation as the
// only thing that could bring a replacement back.
func crashHandler(w http.ResponseWriter, r *http.Request) {
	log.Println("crash requested, exiting 3")
	os.Exit(3)
}

func rootHandler(w http.ResponseWriter, r *http.Request) {
	// TODO(loraine): this response is the instrument the whole experiment
	// reads — write it. Requirements and materials:
	//   - JSON, with at least a "hostname" field (the test enforces this):
	//     a replacement task has a new container ID, and Docker sets the
	//     container's hostname to that ID, so a changed hostname is the
	//     proof a replacement happened.
	//   - startTime is package-level above; uptime (time.Since) resetting
	//     to ~zero corroborates a fresh process rather than a reconnect.
	//   - os.Getpid() distinguishes "new process, same container" from
	//     "new container".
	//   - Optional extras that would let the experiment prove more:
	//     os.Environ() filtered to AWS_* shows what ECS metadata floci
	//     injects into tasks (real ECS sets AWS_EXECUTION_ENV etc. —
	//     whether floci bothers is itself a finding).
	// encoding/json's Encoder on w, or fmt.Fprintf a literal — your call.
}

func main() {
	http.HandleFunc("/", rootHandler)
	http.HandleFunc("/healthz", healthzHandler)
	http.HandleFunc("/crash", crashHandler)
	log.Println("canary listening on :8080")
	log.Fatal(http.ListenAndServe(":8080", nil))
}
```

**USER CHECKPOINT — stop here.** Ask Loraine to implement `rootHandler` (5–10 lines). Do not proceed to Step 5, and do not write the handler body, until they have.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd floci/canary && go test ./...`
Expected: `ok  canary` — both tests pass against their handler.

- [ ] **Step 6: Write `floci/canary/Dockerfile`**

Same builder→runtime shape as the Trakt bot's:

```dockerfile
FROM golang:1.26-alpine AS builder
WORKDIR /app
COPY go.mod ./
COPY *.go ./
RUN go build -o canary .

FROM alpine:3.20
WORKDIR /app
COPY --from=builder /app/canary .
CMD ["./canary"]
```

(No `go.sum` and no `go mod download`: the canary is stdlib-only.)

- [ ] **Step 7: Verify the image builds and runs locally**

Run:

```bash
docker build -t canary:local floci/canary
docker run -d --rm --name canary-smoke -p 127.0.0.1:8080:8080 canary:local
sleep 1
curl -s http://localhost:8080/ ; echo
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/healthz
docker stop canary-smoke
```

Expected: the `/` JSON with a hostname matching the container ID, then `200`.

- [ ] **Step 8: Commit**

```bash
git add floci/canary
git commit -m "add the canary service for the floci ecs experiment"
```

---

### Task 3: Document the stack in the README

**Files:**
- Modify: `README.md` — the Stacks table (after the `komodo/` row) and the repo-layout tree (after the `komodo/` line)

- [ ] **Step 1: Add the Stacks table row**

After the `| [komodo/](komodo/) | ... |` row, add:

```markdown
| [floci/](floci/) | Floci (local AWS emulator) + its console, and a canary test service | Local AWS lab, **under evaluation** until 2026-09-05: can emulated ECS actually host a workload? Deployed by Komodo like the registry stack |
```

- [ ] **Step 2: Add the repo-layout tree lines**

After the `├── komodo/ ...` line in the layout block, add:

```
├── floci/            docker-compose.yml (floci + console), .env.example
│   └── canary/       throwaway Go service the ECS experiment observes
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "document the floci stack"
```

---

### Task 4: Wire it into Komodo and deploy on loopback

Manual steps against live infrastructure — Komodo's UI at `https://komodo.sussman.win` and this repo's GitHub settings. Mirrors the `docker-registry` wiring in [the Komodo spec](../specs/2026-08-06-komodo-evaluation-design.md).

**Interfaces:**
- Consumes: the committed stack from Tasks 1–3, pushed to `main`.
- Produces: a live `floci` Komodo stack that redeploys on every push; the generated credentials Task 5 verifies.

- [ ] **Step 1: Push `main`**

Run: `git push`
Expected: existing webhook fires for `docker-registry` only — `floci` isn't registered yet, nothing deploys it.

- [ ] **Step 2: Generate the credentials**

Run:

```bash
printf 'FLOCI_ACCESS_KEY_ID=%s\nFLOCI_SECRET_ACCESS_KEY=%s\n' \
  "$(shuf -i 100000000000-999999999999 -n 1)" "$(openssl rand -hex 24)"
```

Keep the output for Steps 3 and Task 5. Do not write it into any committed file.

- [ ] **Step 3: Create the Komodo Stack resource** (UI)

| Field | Value |
|---|---|
| Name | `floci` |
| `project_name` | `floci` — explicit, same rationale as `docker-registry` |
| `repo` | `lorainemg/homelab` |
| `branch` | `main` |
| `file_paths` | `["floci/docker-compose.yml"]` |
| `run_directory` | repo root |
| `environment` | the two `FLOCI_*` lines from Step 2 |
| `auto_update` | `false` |

- [ ] **Step 4: Deploy from the Komodo UI and verify**

Deploy the stack, then:

```bash
ssh <server> docker ps --filter name=floci --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Expected: `floci-floci-1` and `floci-floci-ui-1`, both `Up`, ports showing `127.0.0.1:4566` and `127.0.0.1:4500` only.

- [ ] **Step 5: Add the GitHub webhook** (repo *Settings → Webhooks*)

| Field | Value |
|---|---|
| Payload URL | `https://komodo.sussman.win/listener/github/stack/floci/deploy` |
| Content type | `application/json` |
| Secret | same value as `KOMODO_WEBHOOK_SECRET` in `komodo/.env` on the server |
| Events | push only |

- [ ] **Step 6: Prove the webhook redeploys**

Push any commit (Task 5's spec edit will do, or an empty `git commit --allow-empty -m "poke the floci webhook"`). In Komodo's UI, the `floci` stack shows a fresh deploy; GitHub's webhook page shows a `200`.

---

### Task 5: The signature-validation gate

The step the spec is pedantic about: every check except the wrong-secret one passes identically whether validation works or is silently ignored.

**Interfaces:**
- Consumes: credentials from Task 4 Step 2.
- Produces: the `floci` profile in `~/.aws/config`, used by every later command; the go/no-go decision for Task 6.

- [ ] **Step 1: Configure the laptop profile**

`~/.aws/config`:

```ini
[profile floci]
region = us-east-1
endpoint_url = http://localhost:4566
```

`~/.aws/credentials`:

```ini
[floci]
aws_access_key_id = <the 12-digit ID from Task 4>
aws_secret_access_key = <the secret from Task 4>
```

(`endpoint_url` points at localhost for now; Task 6 rewrites it to the server.)

- [ ] **Step 2: Forward 4566 and prove a good signature succeeds**

```bash
ssh -L 4566:localhost:4566 <server>   # keep open
aws --profile floci sts get-caller-identity
```

Expected: JSON whose `Account` is the 12-digit access key ID.

- [ ] **Step 3: Prove a bad secret FAILS — the gate**

```bash
AWS_ACCESS_KEY_ID=<same 12-digit ID> AWS_SECRET_ACCESS_KEY=wrong \
  aws --region us-east-1 --endpoint-url http://localhost:4566 \
  sts get-caller-identity
```

Expected: an error (`SignatureDoesNotMatch`, `InvalidClientTokenId`, 403 — any refusal).

**If it succeeds instead, STOP.** Validation is not in effect: check `docker logs floci-floci-1` for how the env var was read, and floci's `docs/configuration/application-yml.md` for an alternate spelling. If it cannot be made to reject bad secrets, the LAN flip (Task 6) is cancelled — 4566 stays `127.0.0.1`, Task 6 collapses to updating the spec to say so, and Task 7 proceeds over the SSH forward. Record the finding in the spec's open questions either way.

- [ ] **Step 4: Verify the console**

With `ssh -L 4500:localhost:4500 <server>` up, open `http://localhost:4500`: panels load without errors. Create a bucket (`aws --profile floci s3 mb s3://gate-check`) and confirm it appears in the Storage panel; delete it (`aws --profile floci s3 rb s3://gate-check`). Empty panels with the CLI working = the UI's credentials don't match the stack environment — recheck Task 4 Step 3. Drop the 4500 forward when done.

- [ ] **Step 5: Record the gate result in the spec**

In the spec's Open Questions, replace the account-map question's text with what was actually observed (how validation learned the secret, exact error returned on a bad one). Commit:

```bash
git add docs/superpowers/specs/2026-08-08-floci-evaluation-design.md
git commit -m "record how floci signature validation behaves"
```

---

### Task 6: Flip 4566 to the LAN

Only reachable if Task 5 Step 3 refused the bad secret.

**Files:**
- Modify: `floci/docker-compose.yml` — the floci `ports:` entry

- [ ] **Step 1: Change the bind**

In `floci/docker-compose.yml`, change:

```yaml
    ports:
      - "127.0.0.1:4566:4566"
```

to:

```yaml
    ports:
      - "4566:4566"
```

and update the comment above it (it currently says "Loopback until…") to say the gate passed and when, e.g. `# LAN-bound since 2026-08-XX: signature validation verified to reject bad secrets.`

- [ ] **Step 2: Commit and let the webhook deploy it**

```bash
git add floci/docker-compose.yml
git commit -m "put floci's api on the lan"
git push
```

Expected: Komodo redeploys `floci` (visible in its UI).

- [ ] **Step 3: Verify from the laptop, no tunnel**

Point the profile at the server — in `~/.aws/config`, change the `floci` profile's `endpoint_url` to `http://<server>:4566`. Then, with no SSH session open:

```bash
aws --profile floci sts get-caller-identity        # succeeds
AWS_ACCESS_KEY_ID=123456789012 AWS_SECRET_ACCESS_KEY=wrong \
  aws --region us-east-1 --endpoint-url http://<server>:4566 \
  sts get-caller-identity                          # refused
curl -s -o /dev/null -w "%{http_code}\n" http://<server>:4500 || true
```

Expected: success, refusal, and the `curl` to 4500 fails to connect (UI still loopback-only).

---

### Task 7: The experiment

**Interfaces:**
- Consumes: `canary:local` (built here on the server), the `floci` profile, the canary's `GET /` `"hostname"` field.
- Produces: a Results section appended to the spec — the evaluation's deliverable.

- [ ] **Step 1: Build the canary on the server**

Straight from the git URL, so no repo checkout path matters:

```bash
ssh <server> docker build -t canary:local \
  'https://github.com/lorainemg/homelab.git#main:floci/canary'
```

Expected: `naming to docker.io/library/canary:local`. The image is never pushed.

- [ ] **Step 2: Create cluster, task definition, service**

```bash
aws --profile floci ecs create-cluster --cluster-name homelab
aws --profile floci ecs register-task-definition \
  --family canary \
  --container-definitions '[{"name":"web","image":"canary:local","memory":128}]'
aws --profile floci ecs create-service --cluster homelab \
  --service-name canary --task-definition canary --desired-count 1
```

- [ ] **Step 3: Criterion 1 — does a container appear?**

```bash
ssh <server> "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep canary"
```

Note the container name — call it `<canary-ctr>` below. Read its identity:

```bash
ssh <server> docker run --rm --network floci_default curlimages/curl \
  -s http://<canary-ctr>:8080/
```

Record the hostname value.

- [ ] **Step 4: Criterion 2 — THE test: crash it, watch for a replacement**

```bash
ssh <server> docker run --rm --network floci_default curlimages/curl \
  -s --max-time 5 http://<canary-ctr>:8080/crash || true
sleep 15
ssh <server> "docker ps --format '{{.Names}}\t{{.Status}}' | grep canary"
```

Then hit `/` on whatever is running. **Pass:** a canary container is up whose reported hostname differs from Step 3's and whose uptime is seconds. **Fail:** no canary running, or the same hostname (never died / restart-policy restart, not reconciliation). Give it up to two minutes before calling a fail — real ECS reconciles in seconds, but note the actual latency either way.

- [ ] **Step 5: Criteria 3–6 — texture**

```bash
# 3: does floci's bookkeeping match reality?
aws --profile floci ecs describe-services --cluster homelab --services canary \
  --query 'services[0].{desired:desiredCount,running:runningCount}'
ssh <server> "docker ps | grep -c canary"

# 4: does the service survive a floci restart?
ssh <server> docker restart floci-floci-1
sleep 30
ssh <server> "docker ps --format '{{.Names}}\t{{.Status}}' | grep canary"
aws --profile floci ecs describe-services --cluster homelab --services canary \
  --query 'services[0].runningCount'

# 6: anything in the console? (expected: no — ECS has no panel)
# ssh -L 4500:localhost:4500 <server>, look, drop the tunnel.
```

Criterion 5 (host reboot) is optional and Loraine's call — it takes the whole homelab down for a minute. If run: `ssh <server> sudo reboot`, wait, repeat the criterion-4 checks.

- [ ] **Step 6: Record the results in the spec**

Append to the spec:

```markdown
## Results (2026-08-XX)

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Service start produces a container | ... |
| 2 | **Crash → replacement with new hostname** | ... |
| 3 | describe-services matches docker ps | ... |
| 4 | Survives floci restart | ... |
| 5 | Survives host reboot | ... / not run |
| 6 | Visible in floci-ui | ... |

**Verdict:** [criterion 2's answer, in one sentence, and what it means for the
hosting question per the spec's Outcomes section.]
```

Fill every cell with what actually happened, including latencies and exact errors. Commit:

```bash
git add docs/superpowers/specs/2026-08-08-floci-evaluation-design.md
git commit -m "record the floci ecs experiment results"
git push
```

- [ ] **Step 7: Clean up the experiment's leftovers**

```bash
aws --profile floci ecs delete-service --cluster homelab --service canary --force
ssh <server> "docker ps -a | grep canary"   # confirm floci actually removed them
```

If floci left containers behind, `docker rm -f` them by name and note that in the Results too — teardown fidelity is also a finding.
