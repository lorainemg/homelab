# Komodo GitHub Action Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ~140 lines of hand-rolled Komodo `curl` across two repos with three task-named composite GitHub Actions sharing one tested bash library.

**Architecture:** A single bash library (`lib/komodo.sh`) owns authentication, the HTTP call, placeholder expansion from a `vars.env` beside it, and the poll-an-Update-to-completion loop. Three composite actions (`update-stack`, `deploy-stack`, `run-sync`) each source that library and map named inputs onto one Komodo request. The library is tested against a Python stub HTTP server that replays real Komodo response shapes, so nothing in the test suite touches the live homelab.

**Tech Stack:** Bash 5 (`curl`, `jq`, `envsubst` from gettext — all preinstalled on `ubuntu-latest`), GitHub composite actions, Python 3 `http.server` for the test stub, `bats`-free plain-bash test runner invoked from a workflow.

**Spec:** [docs/superpowers/specs/2026-09-04-komodo-github-action-design.md](../specs/2026-09-04-komodo-github-action-design.md)

## Global Constraints

- Komodo Core version is **2.3.1**. All response shapes below are from that version.
- Authentication is **`X-Api-Key` + `X-Api-Secret` headers**, never a JWT. Endpoints are `POST {url}/read`, `/write`, `/execute` with a body of `{"type": ..., "params": {...}}`.
- **`/execute` returns `status: "InProgress"` immediately**, and permission failures surface only in the resulting Update's `success: false`. A 2xx never means permitted or done.
- **A missing stack is HTTP 500** with `"Did not find any Stack"` in the body, not a 404. Branch on the body.
- **Never print a request or response body that can contain a stack `environment`.** Those hold secrets. Print request *types*, HTTP statuses, and Komodo's own Update log stages only.
- Poll interval is **5 seconds**; default timeout **300 seconds**.
- Shell scripts use `set -euo pipefail`.
- Commit messages: single line, casual, no trailers (repo convention).
- The gitleaks pre-commit hook cannot run on this workstation (Docker permissions). Scan staged changes with the standalone binary at `/tmp/gitleaks/gitleaks git --staged --no-banner --redact .` and commit with `--no-verify`.

---

### Task 1: The stub Komodo server and the test runner

Build the test harness first, so every later task has something to test against. The stub replays four real response shapes; the runner is a plain bash script that starts the stub, sources the library, runs assertions, and reports.

**Files:**
- Create: `.github/actions/komodo/tests/stub_komodo.py`
- Create: `.github/actions/komodo/tests/run_tests.sh`
- Create: `.github/actions/komodo/lib/komodo.sh` (empty placeholder so the runner can source it)

**Interfaces:**
- Consumes: nothing.
- Produces: `stub_komodo.py` listens on a port given as `argv[1]` and serves the scenarios below. `run_tests.sh` defines `assert_eq <actual> <expected> <name>`, `assert_contains <haystack> <needle> <name>`, `assert_fails <command...> <name>`, and exits non-zero if any assertion failed.

- [ ] **Step 1: Write the stub server**

Create `.github/actions/komodo/tests/stub_komodo.py`:

```python
#!/usr/bin/env python3
"""A stand-in for Komodo Core 2.3.1, replaying the response shapes the real
server produces. Scenario is chosen by the request's `type` field, so a test
picks a behaviour by calling the matching Komodo request type."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

# How many times GetUpdate has been asked about each update id, so a test can
# assert that the caller really polls rather than reading status once.
POLLS = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep test output clean

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or "{}")
        rtype = req.get("type", "")
        params = req.get("params", {})

        # Auth is checked the same way the real server does: both headers.
        if not self.headers.get("X-Api-Key") or not self.headers.get("X-Api-Secret"):
            return self._send(401, {"error": "unauthorized"})

        # --- reads -------------------------------------------------------
        if rtype == "GetStack":
            if params.get("stack") == "missing-stack":
                # The real shape: 500, not 404.
                return self._send(500, {
                    "error": "Did not find any Stack matching missing-stack",
                    "trace": [],
                })
            return self._send(200, {"name": params.get("stack"), "_id": {"$oid": "s1"}})

        if rtype == "GetUpdate":
            uid = params.get("id")
            POLLS[uid] = POLLS.get(uid, 0) + 1
            if uid == "u-never":
                return self._send(200, {"_id": {"$oid": uid}, "status": "InProgress"})
            # Stay InProgress on the first poll so the loop must run twice.
            if POLLS[uid] < 2:
                return self._send(200, {"_id": {"$oid": uid}, "status": "InProgress"})
            if uid == "u-fail":
                return self._send(200, {
                    "_id": {"$oid": uid}, "status": "Complete", "success": False,
                    "logs": [{"stage": "Deploy", "stdout": "pulling image",
                              "stderr": "denied: permission on Stack"}],
                })
            return self._send(200, {
                "_id": {"$oid": uid}, "status": "Complete", "success": True,
                "logs": [{"stage": "Deploy", "stdout": "started", "stderr": ""}],
            })

        # --- writes ------------------------------------------------------
        if rtype in ("CreateStack", "UpdateStack", "UpdateResourceSync"):
            if params.get("id") == "boom" or params.get("name") == "boom":
                return self._send(400, {"error": "bad request from stub"})
            # Echo the config back so tests can assert what was sent.
            return self._send(200, {"name": params.get("name") or params.get("id"),
                                    "config": params.get("config", {})})

        # --- executes ----------------------------------------------------
        if rtype in ("DeployStack", "RunSync"):
            target = params.get("stack") or params.get("sync")
            uid = {"fail-me": "u-fail", "hang-me": "u-never"}.get(target, "u-ok")
            return self._send(200, {"_id": {"$oid": uid}, "status": "InProgress"})

        return self._send(404, {"error": f"stub has no case for {rtype}"})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
```

- [ ] **Step 2: Write the test runner with its first assertion**

Create `.github/actions/komodo/tests/run_tests.sh`:

```bash
#!/usr/bin/env bash
# Tests for lib/komodo.sh against a stub Komodo. No network, nothing live.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT=${PORT:-8731}
FAILED=0

assert_eq() { # actual expected name
  if [[ "$1" == "$2" ]]; then echo "  ok   $3"; else
    echo "  FAIL $3"; echo "       expected: $2"; echo "       actual:   $1"; FAILED=1
  fi
}
assert_contains() { # haystack needle name
  if [[ "$1" == *"$2"* ]]; then echo "  ok   $3"; else
    echo "  FAIL $3"; echo "       expected to contain: $2"; echo "       actual: $1"; FAILED=1
  fi
}
assert_fails() { # name command...
  local name=$1; shift
  if "$@" >/dev/null 2>&1; then echo "  FAIL $name (expected non-zero exit)"; FAILED=1
  else echo "  ok   $name"; fi
}

python3 "$HERE/stub_komodo.py" "$PORT" &
STUB=$!
trap 'kill $STUB 2>/dev/null' EXIT
for _ in $(seq 1 50); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/read" && break
  sleep 0.1
done

export KOMODO_URL="http://127.0.0.1:$PORT"
export KOMODO_API_KEY=test-key
export KOMODO_API_SECRET=test-secret
export KOMODO_POLL_INTERVAL=0        # no real sleeping in tests
source "$HERE/../lib/komodo.sh"

echo "stub is up on $PORT"

exit $FAILED
```

- [ ] **Step 3: Create the empty library so the runner can source it**

Create `.github/actions/komodo/lib/komodo.sh`:

```bash
#!/usr/bin/env bash
# Shared helpers for the komodo composite actions. Sourced, never executed.
```

- [ ] **Step 4: Run the harness and verify it starts the stub and exits clean**

```bash
chmod +x .github/actions/komodo/tests/run_tests.sh .github/actions/komodo/tests/stub_komodo.py
.github/actions/komodo/tests/run_tests.sh
```

Expected: prints `stub is up on 8731` and exits 0.

- [ ] **Step 5: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "add a stub komodo server to test the action library against"
```

---

### Task 2: `komodo_call` — one authenticated request

**Files:**
- Modify: `.github/actions/komodo/lib/komodo.sh`
- Modify: `.github/actions/komodo/tests/run_tests.sh`

**Interfaces:**
- Consumes: `KOMODO_URL`, `KOMODO_API_KEY`, `KOMODO_API_SECRET` from the environment.
- Produces: `komodo_call <route> <json-body>` where route is `read`, `write` or `execute`. Prints the response body on stdout. Returns 0 on a 2xx; on any other status prints `komodo <type> failed (HTTP <code>)` plus the response's `.error` field to stderr and returns 1. Never prints the request body.

- [ ] **Step 1: Write the failing tests**

Append to `run_tests.sh`, immediately before `exit $FAILED`:

```bash
echo "komodo_call"
out=$(komodo_call read '{"type":"GetStack","params":{"stack":"immich"}}')
assert_eq "$(jq -r .name <<<"$out")" "immich" "returns the response body"

assert_fails "non-2xx returns non-zero" \
  komodo_call write '{"type":"UpdateStack","params":{"id":"boom","config":{}}}'

err=$(komodo_call write '{"type":"UpdateStack","params":{"id":"boom","config":{}}}' 2>&1 >/dev/null)
assert_contains "$err" "HTTP 400" "reports the status code"
assert_contains "$err" "bad request from stub" "reports komodo's error message"

secret_body='{"type":"UpdateStack","params":{"id":"s","config":{"environment":"TOKEN=hunter2"}}}'
noise=$(komodo_call write "$secret_body" 2>&1 >/dev/null)
assert_eq "$(grep -c hunter2 <<<"$noise")" "0" "never echoes the request body"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: FAIL with `komodo_call: command not found`.

- [ ] **Step 3: Implement `komodo_call`**

Append to `lib/komodo.sh`:

```bash
# komodo_call <read|write|execute> <json-body>
# Prints the response body. Non-2xx is an error, reported without the request.
komodo_call() {
  local route=$1 body=$2 resp code type
  type=$(jq -r '.type // "?"' <<<"$body")
  resp=$(curl -sS -w $'\n%{http_code}' -X POST "$KOMODO_URL/$route" \
    -H "X-Api-Key: $KOMODO_API_KEY" \
    -H "X-Api-Secret: $KOMODO_API_SECRET" \
    -H 'Content-Type: application/json' \
    --data-binary @- <<<"$body") || {
      echo "komodo $type failed: could not reach $KOMODO_URL" >&2; return 1; }
  code=${resp##*$'\n'}
  resp=${resp%$'\n'*}
  if [[ $code != 2* ]]; then
    echo "komodo $type failed (HTTP $code): $(jq -r '.error // "no error field"' <<<"$resp" 2>/dev/null)" >&2
    return 1
  fi
  printf '%s' "$resp"
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: four `ok` lines under `komodo_call`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "add the authenticated komodo call helper"
```

---

### Task 3: `komodo_await` — poll an Update to completion

**Files:**
- Modify: `.github/actions/komodo/lib/komodo.sh`
- Modify: `.github/actions/komodo/tests/run_tests.sh`

**Interfaces:**
- Consumes: `komodo_call` from Task 2.
- Produces: `komodo_await <update-id> <timeout-seconds>`. Polls `GetUpdate` every `KOMODO_POLL_INTERVAL` seconds (default 5) until `status == "Complete"`. Returns 0 when `success` is true. When false, prints each log's stage, stdout and stderr, then returns 1. On timeout prints a timeout message and returns 1.

- [ ] **Step 1: Write the failing tests**

Append to `run_tests.sh`, before `exit $FAILED`:

```bash
echo "komodo_await"
out=$(komodo_await u-ok 30 2>&1); rc=$?
assert_eq "$rc" "0" "succeeds on a successful update"

out=$(komodo_await u-fail 30 2>&1); rc=$?
assert_eq "$rc" "1" "fails on an unsuccessful update"
assert_contains "$out" "Deploy" "prints the failing stage"
assert_contains "$out" "denied: permission on Stack" "prints komodo's stderr"

out=$(komodo_await u-never 1 2>&1); rc=$?
assert_eq "$rc" "1" "fails on timeout rather than passing"
assert_contains "$out" "timed out" "says it timed out"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: FAIL with `komodo_await: command not found`.

- [ ] **Step 3: Implement `komodo_await`**

Append to `lib/komodo.sh`:

```bash
# komodo_await <update-id> <timeout-seconds>
# /execute only means "accepted" — even a permission refusal arrives here, as
# success:false. So the result of any execute is whatever this function says.
komodo_await() {
  local id=$1 timeout=${2:-300} interval=${KOMODO_POLL_INTERVAL:-5}
  local waited=0 update status
  while true; do
    update=$(komodo_call read "$(jq -n --arg id "$id" \
      '{type:"GetUpdate",params:{id:$id}}')") || return 1
    status=$(jq -r '.status // "Unknown"' <<<"$update")
    [[ $status == Complete ]] && break
    if (( waited >= timeout )); then
      echo "komodo update $id timed out after ${timeout}s (last status: $status)" >&2
      return 1
    fi
    (( interval > 0 )) && sleep "$interval"
    waited=$(( waited + (interval > 0 ? interval : 1) ))
  done
  if [[ $(jq -r '.success // false' <<<"$update") == true ]]; then
    echo "komodo update $id completed"
    return 0
  fi
  echo "komodo update $id failed:" >&2
  jq -r '.logs[]? | "--- \(.stage)\n\(.stdout // "")\n\(.stderr // "")"' <<<"$update" >&2
  return 1
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: five `ok` lines under `komodo_await`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "poll komodo updates to completion and print the failing stage"
```

---

### Task 4: `komodo_expand` and `komodo_stack_exists`

Two small helpers: placeholder expansion from `vars.env`, and the create-if-missing probe that has to read a 500 body.

**Files:**
- Create: `.github/actions/komodo/vars.env`
- Modify: `.github/actions/komodo/lib/komodo.sh`
- Modify: `.github/actions/komodo/tests/run_tests.sh`

**Interfaces:**
- Consumes: `komodo_call` from Task 2.
- Produces: `komodo_expand <text>` prints the text with `${NAME}` placeholders replaced from `vars.env`, and returns 1 if any `${` survives. `komodo_stack_exists <name>` returns 0 if the stack exists, 1 if Komodo says it does not, and 2 if the lookup itself failed.

- [ ] **Step 1: Create `vars.env`**

Create `.github/actions/komodo/vars.env`:

```bash
# Values shared by everything that talks to this Komodo. Edit here, nowhere
# else: the composite actions in this directory expand ${NAME} in their
# `links` input and in the TOML that run-sync pushes, and scripts/bootstrap.sh
# reads this same file when it renders komodo/stacks.toml on a fresh server.
HOMELAB_LAN_IP=172.20.3.194
```

- [ ] **Step 2: Write the failing tests**

Append to `run_tests.sh`, before `exit $FAILED`:

```bash
echo "komodo_expand"
assert_eq "$(komodo_expand 'http://${HOMELAB_LAN_IP}:5000')" \
          "http://172.20.3.194:5000" "expands a known placeholder"
assert_eq "$(komodo_expand 'no placeholders here')" \
          "no placeholders here" "leaves plain text alone"
assert_fails "unknown placeholder is an error" komodo_expand 'http://${NOPE}:1'
assert_eq "$(komodo_expand 'cost is $5 and 100% real')" \
          'cost is $5 and 100% real' "leaves a bare dollar sign alone"

echo "komodo_stack_exists"
komodo_stack_exists immich;        assert_eq "$?" "0" "true for an existing stack"
komodo_stack_exists missing-stack; assert_eq "$?" "1" "false on the 500 not-found body"
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: FAIL with `komodo_expand: command not found`.

- [ ] **Step 4: Implement both helpers**

Append to `lib/komodo.sh`:

```bash
# komodo_expand <text>
# Replaces ${NAME} from vars.env beside this library. Only the names declared
# there are substituted, so a bare `$` in a compose file or a link is safe.
komodo_expand() {
  local vars="${KOMODO_VARS_FILE:-$(dirname "${BASH_SOURCE[0]}")/../vars.env}" out names
  # shellcheck disable=SC1090
  set -a; . "$vars"; set +a
  names=$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/${\1}/p' "$vars" | tr '\n' ' ')
  out=$(envsubst "$names" <<<"$1")
  if [[ $out == *'${'* ]]; then
    echo "unresolved placeholder in: $out" >&2
    echo "declared in $(basename "$vars"): $names" >&2
    return 1
  fi
  printf '%s' "$out"
}

# komodo_stack_exists <name>
# Komodo answers a missing stack with HTTP 500 and "Did not find any Stack",
# not a 404, so the body is the only reliable signal.
komodo_stack_exists() {
  local name=$1 resp
  resp=$(komodo_call read "$(jq -n --arg s "$name" \
    '{type:"GetStack",params:{stack:$s}}')" 2>&1)
  if [[ $resp == *"Did not find any Stack"* ]]; then return 1; fi
  if [[ $(jq -r '.name // empty' <<<"$resp" 2>/dev/null) == "$name" ]]; then return 0; fi
  echo "could not determine whether stack $name exists: $resp" >&2
  return 2
}
```

Note: `komodo_expand` uses a trailing `printf '%s'` without a newline so callers can embed it; `envsubst` is given an explicit name list so undeclared `${...}` survives and trips the guard.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: six `ok` lines across the two groups, exit 0.

- [ ] **Step 6: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "expand shared vars and probe whether a stack exists"
```

---

### Task 5: The `deploy-stack` action

The smallest of the three, so it proves the composite-action wiring before the more complex ones.

**Files:**
- Create: `.github/actions/komodo/deploy-stack/action.yml`
- Modify: `.github/actions/komodo/tests/run_tests.sh`

**Interfaces:**
- Consumes: `komodo_call`, `komodo_await`.
- Produces: a composite action with inputs `komodo-url`, `api-key`, `api-secret`, `stack`, `timeout` (default `300`). Fails the step unless the resulting Update reports success.

- [ ] **Step 1: Write the failing test for the deploy path**

Append to `run_tests.sh`, before `exit $FAILED`:

```bash
echo "deploy flow"
deploy() { # <stack> <timeout>
  local resp id
  resp=$(komodo_call execute "$(jq -n --arg s "$1" \
    '{type:"DeployStack",params:{stack:$s}}')") || return 1
  id=$(jq -r '._id."$oid"' <<<"$resp")
  komodo_await "$id" "$2"
}
deploy trakt-tg-bot 30 >/dev/null 2>&1; assert_eq "$?" "0" "a good deploy passes"
out=$(deploy fail-me 30 2>&1); rc=$?
assert_eq "$rc" "1" "a refused deploy fails the step"
assert_contains "$out" "denied: permission on Stack" "surfaces why it failed"
```

- [ ] **Step 2: Run the tests to verify they pass**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: three `ok` lines under `deploy flow`. (This test exercises the library composition the action will use; it passes immediately because Tasks 2 and 3 are done. Its value is locking the flow before the YAML wraps it.)

- [ ] **Step 3: Write the action**

Create `.github/actions/komodo/deploy-stack/action.yml`:

```yaml
name: Komodo deploy stack
description: Deploy a Komodo stack and wait for the result, failing on Komodo's own error.

inputs:
  komodo-url:
    description: Base URL of Komodo Core.
    required: true
  api-key:
    description: API key for a Komodo service user with Execute on this stack.
    required: true
  api-secret:
    description: API secret for that key.
    required: true
  stack:
    description: Name of the stack to deploy.
    required: true
  timeout:
    description: Seconds to wait for the deploy to finish.
    required: false
    default: "300"

runs:
  using: composite
  steps:
    - shell: bash
      env:
        KOMODO_URL: ${{ inputs.komodo-url }}
        KOMODO_API_KEY: ${{ inputs.api-key }}
        KOMODO_API_SECRET: ${{ inputs.api-secret }}
        STACK: ${{ inputs.stack }}
        TIMEOUT: ${{ inputs.timeout }}
      run: |
        set -euo pipefail
        source "$GITHUB_ACTION_PATH/../lib/komodo.sh"

        # /execute is accepted-not-done: the update below is the real result.
        resp=$(komodo_call execute "$(jq -n --arg s "$STACK" \
          '{type:"DeployStack",params:{stack:$s}}')")
        komodo_await "$(jq -r '._id."$oid"' <<<"$resp")" "$TIMEOUT"
```

- [ ] **Step 4: Verify the YAML parses and the sibling path is right**

```bash
python3 -c 'import yaml; d=yaml.safe_load(open(".github/actions/komodo/deploy-stack/action.yml")); print("inputs:", list(d["inputs"]))'
test -f .github/actions/komodo/deploy-stack/../lib/komodo.sh && echo "sibling lib path resolves"
```

Expected: the five input names, then `sibling lib path resolves`.

- [ ] **Step 5: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "add the deploy-stack action"
```

---

### Task 6: The `update-stack` action

**Files:**
- Create: `.github/actions/komodo/update-stack/action.yml`
- Modify: `.github/actions/komodo/tests/run_tests.sh`

**Interfaces:**
- Consumes: `komodo_call`, `komodo_expand`, `komodo_stack_exists`.
- Produces: a composite action with inputs `komodo-url`, `api-key`, `api-secret`, `stack`, `compose-file`, `env-file`, `links`, `create-if-missing` (default `false`), `server` (default `Local`). Builds an `UpdateStack` config containing only the fields whose inputs were given.

Payload assembly lives in the library, not in the action's YAML, so the tests
exercise the code that actually ships. (Controller ruling, 2026-09-04: the
original plan duplicated this block between test and action.)

- [ ] **Step 1: Write the failing test**

Append to `run_tests.sh`, before `exit $FAILED`:

```bash
echo "komodo_stack_config"
tmp=$(mktemp -d)
printf 'services: {}\n' > "$tmp/compose.yaml"
printf 'A=1\n'           > "$tmp/.env"

cfg=$(komodo_stack_config "$tmp/compose.yaml" "$tmp/.env" 'http://${HOMELAB_LAN_IP}:28888/login')
assert_eq "$(jq -r '.links[0]' <<<"$cfg")" "http://172.20.3.194:28888/login" "expands links"
assert_eq "$(jq -r '.file_contents' <<<"$cfg")" "services: {}" "carries the compose file"
assert_eq "$(jq -r '.environment' <<<"$cfg")" "A=1" "carries the env file"

cfg=$(komodo_stack_config "$tmp/compose.yaml" "" "")
assert_eq "$(jq -r 'has("environment")' <<<"$cfg")" "false" "omits fields with no input"
assert_eq "$(jq -r 'has("links")' <<<"$cfg")" "false" "omits links with no input"

cfg=$(komodo_stack_config "" "" $'http://${HOMELAB_LAN_IP}:1\nhttp://${HOMELAB_LAN_IP}:2')
assert_eq "$(jq -r '.links | length' <<<"$cfg")" "2" "splits multiple links"

assert_fails "an unknown placeholder in links fails" \
  komodo_stack_config "" "" 'http://${NOPE}:1'
rm -rf "$tmp"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: FAIL with `komodo_stack_config: command not found`.

- [ ] **Step 3: Implement `komodo_stack_config` in the library**

Append to `lib/komodo.sh`:

```bash
# komodo_stack_config <compose-file> <env-file> <links>
# Builds an UpdateStack config from whichever inputs were supplied. An empty
# argument means "leave that field alone": omitted fields are absent from the
# payload, so an update never clears something the caller did not mention.
komodo_stack_config() {
  local compose=$1 env_file=$2 links=$3 cfg='{}' expanded
  [[ -n $compose ]] && cfg=$(jq --rawfile f "$compose" '.file_contents = $f' <<<"$cfg")
  [[ -n $env_file ]] && cfg=$(jq --rawfile f "$env_file" '.environment = $f' <<<"$cfg")
  if [[ -n $links ]]; then
    expanded=$(komodo_expand "$links") || return 1
    cfg=$(jq --arg l "$expanded" \
      '.links = ($l | split("\n") | map(select(length > 0)))' <<<"$cfg")
  fi
  printf '%s' "$cfg"
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: seven `ok` lines under `komodo_stack_config`, exit 0.

- [ ] **Step 5: Write the action**

Create `.github/actions/komodo/update-stack/action.yml`:

```yaml
name: Komodo update stack
description: Push a stack's compose file, environment and links, creating the stack if it is missing.

inputs:
  komodo-url:
    description: Base URL of Komodo Core.
    required: true
  api-key:
    description: API key for a Komodo service user with Write on this stack.
    required: true
  api-secret:
    description: API secret for that key.
    required: true
  stack:
    description: Name of the stack.
    required: true
  compose-file:
    description: Path to a compose file whose contents become the stack's file_contents.
    required: false
    default: ""
  env-file:
    description: Path to an env file whose contents become the stack's environment.
    required: false
    default: ""
  links:
    description: Newline-separated links for the stack page. ${NAME} is expanded from vars.env.
    required: false
    default: ""
  create-if-missing:
    description: Create the stack before updating it if Komodo does not have it.
    required: false
    default: "false"
  server:
    description: Server to attach a newly created stack to. Only used when creating.
    required: false
    default: Local

runs:
  using: composite
  steps:
    - shell: bash
      env:
        KOMODO_URL: ${{ inputs.komodo-url }}
        KOMODO_API_KEY: ${{ inputs.api-key }}
        KOMODO_API_SECRET: ${{ inputs.api-secret }}
        STACK: ${{ inputs.stack }}
        COMPOSE_FILE: ${{ inputs.compose-file }}
        ENV_FILE: ${{ inputs.env-file }}
        LINKS: ${{ inputs.links }}
        CREATE_IF_MISSING: ${{ inputs.create-if-missing }}
        SERVER: ${{ inputs.server }}
      run: |
        set -euo pipefail
        source "$GITHUB_ACTION_PATH/../lib/komodo.sh"

        if [[ $CREATE_IF_MISSING == true ]]; then
          # `f; found=$?` would abort here: under `set -e` a function returning
          # non-zero kills the step before $? is ever read. Verified 2026-09-04.
          komodo_stack_exists "$STACK" && found=0 || found=$?
          [[ $found == 2 ]] && exit 1
          if [[ $found == 1 ]]; then
            echo "stack $STACK does not exist yet; creating it"
            komodo_call write "$(jq -n --arg s "$STACK" --arg srv "$SERVER" \
              '{type:"CreateStack",params:{name:$s,config:{
                 server_id:$srv, project_name:$s,
                 file_contents:"services: {}", webhook_enabled:false}}}')" > /dev/null
          fi
        fi

        # Only the fields the caller supplied go in, so an omitted input never
        # clears what is already on the stack.
        cfg=$(komodo_stack_config "$COMPOSE_FILE" "$ENV_FILE" "$LINKS")

        komodo_call write "$(jq -n --arg s "$STACK" --argjson c "$cfg" \
          '{type:"UpdateStack",params:{id:$s,config:$c}}')" > /dev/null
        echo "stack $STACK updated"
```

- [ ] **Step 6: Verify the YAML parses**

```bash
python3 -c 'import yaml; d=yaml.safe_load(open(".github/actions/komodo/update-stack/action.yml")); print("inputs:", list(d["inputs"]))'
```

Expected: the nine input names.

- [ ] **Step 7: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "add the update-stack action"
```

---

### Task 7: The `run-sync` action

**Files:**
- Create: `.github/actions/komodo/run-sync/action.yml`
- Modify: `.github/actions/komodo/tests/run_tests.sh`

**Interfaces:**
- Consumes: `komodo_call`, `komodo_await`, `komodo_expand`.
- Produces: a composite action with inputs `komodo-url`, `api-key`, `api-secret`, `sync`, `contents-file`, `timeout` (default `300`). Pushes the rendered TOML with `repo`, `branch` and `resource_path` explicitly empty, then runs the sync and waits.

- [ ] **Step 1: Write the failing test for the source-clearing payload**

Append to `run_tests.sh`, before `exit $FAILED`:

```bash
echo "run-sync payload"
tmp=$(mktemp -d)
cat > "$tmp/stacks.toml" <<'TOML'
[[stack]]
name = "docker-registry"
links = ["http://${HOMELAB_LAN_IP}:5000"]
TOML

payload=$(komodo_sync_config homelab "$tmp/stacks.toml")
assert_contains "$(jq -r '.params.config.file_contents' <<<"$payload")" \
  "http://172.20.3.194:5000" "renders the toml"
assert_eq "$(jq -r '.params.config.repo' <<<"$payload")" "" "clears repo"
assert_eq "$(jq -r '.params.config.branch' <<<"$payload")" "" "clears branch"
assert_eq "$(jq -r '.params.config.resource_path | length' <<<"$payload")" "0" "clears resource_path"
assert_eq "$(jq -r '.params.id' <<<"$payload")" "homelab" "targets the named sync"

printf 'links = ["http://${NOPE}:1"]\n' > "$tmp/bad.toml"
assert_fails "an unknown placeholder fails" komodo_sync_config homelab "$tmp/bad.toml"
rm -rf "$tmp"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: FAIL with `komodo_sync_config: command not found`.

- [ ] **Step 3: Implement `komodo_sync_config` in the library**

Append to `lib/komodo.sh`:

```bash
# komodo_sync_config <sync-name> <toml-file>
# Builds the UpdateResourceSync request for a contents-mode sync. repo, branch
# and resource_path are cleared every time: Komodo picks its source in that
# order and prefers a repo over stored contents, so leaving them set would
# silently ignore what we just pushed (bin/core/src/sync/remote.rs, v2.3.1).
komodo_sync_config() {
  local sync=$1 file=$2 rendered
  rendered=$(komodo_expand "$(cat "$file")") || return 1
  jq -n --arg s "$sync" --arg toml "$rendered" \
    '{type:"UpdateResourceSync",params:{id:$s,config:{
       file_contents:$toml, repo:"", branch:"", resource_path:[]}}}'
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.github/actions/komodo/tests/run_tests.sh
```

Expected: six `ok` lines under `run-sync payload`, exit 0.

- [ ] **Step 5: Write the action**

Create `.github/actions/komodo/run-sync/action.yml`:

```yaml
name: Komodo run sync
description: Render a ResourceSync's TOML, push it as the sync's contents, run the sync and wait.

inputs:
  komodo-url:
    description: Base URL of Komodo Core.
    required: true
  api-key:
    description: API key for a Komodo service user with Write on this sync.
    required: true
  api-secret:
    description: API secret for that key.
    required: true
  sync:
    description: Name of the ResourceSync.
    required: true
  contents-file:
    description: TOML file to push. ${NAME} is expanded from vars.env.
    required: true
  timeout:
    description: Seconds to wait for the sync to finish.
    required: false
    default: "300"

runs:
  using: composite
  steps:
    - shell: bash
      env:
        KOMODO_URL: ${{ inputs.komodo-url }}
        KOMODO_API_KEY: ${{ inputs.api-key }}
        KOMODO_API_SECRET: ${{ inputs.api-secret }}
        SYNC: ${{ inputs.sync }}
        CONTENTS_FILE: ${{ inputs.contents-file }}
        TIMEOUT: ${{ inputs.timeout }}
      run: |
        set -euo pipefail
        source "$GITHUB_ACTION_PATH/../lib/komodo.sh"

        komodo_call write "$(komodo_sync_config "$SYNC" "$CONTENTS_FILE")" > /dev/null

        resp=$(komodo_call execute "$(jq -n --arg s "$SYNC" \
          '{type:"RunSync",params:{sync:$s}}')")
        komodo_await "$(jq -r '._id."$oid"' <<<"$resp")" "$TIMEOUT"
```

- [ ] **Step 6: Verify the YAML parses**

```bash
python3 -c 'import yaml; d=yaml.safe_load(open(".github/actions/komodo/run-sync/action.yml")); print("inputs:", list(d["inputs"]))'
```

Expected: the six input names.

- [ ] **Step 7: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "add the run-sync action"
```

---

### Task 8: Run the action tests in CI

**Files:**
- Create: `.github/workflows/test-actions.yml`

**Interfaces:**
- Consumes: `tests/run_tests.sh` from Task 1.
- Produces: a workflow that runs the suite on any push touching the action.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/test-actions.yml`:

```yaml
name: Test actions

on:
  push:
    paths:
      - '.github/actions/komodo/**'
      - '.github/workflows/test-actions.yml'
  pull_request:
    paths:
      - '.github/actions/komodo/**'
  workflow_dispatch:

jobs:
  komodo-lib:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Shellcheck the library
        run: shellcheck -x .github/actions/komodo/lib/komodo.sh
      - name: Run the library tests against the stub server
        run: .github/actions/komodo/tests/run_tests.sh
```

- [ ] **Step 2: Verify it parses and shellcheck is clean locally**

```bash
python3 -c 'import yaml; yaml.safe_load(open(".github/workflows/test-actions.yml")); print("parses")'
shellcheck -x .github/actions/komodo/lib/komodo.sh && echo "shellcheck clean"
```

Expected: `parses`, then `shellcheck clean`. If shellcheck is not installed locally, note it and rely on CI.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/test-actions.yml
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "run the komodo action tests in ci"
```

---

### Task 9: Rewrite this repo's `deploy.yml` to use the actions

Replaces both the inline sync job added on this branch and the signed webhook that deploys `config`.

**Files:**
- Modify: `.github/workflows/deploy.yml`
- Delete: `komodo/vars.env` (moved to `.github/actions/komodo/vars.env` in Task 4)
- Modify: `scripts/bootstrap.sh` (vars path)

**Interfaces:**
- Consumes: `deploy-stack` and `run-sync` from Tasks 5 and 7.
- Produces: nothing later depends on.

- [ ] **Step 1: Replace the webhook trigger step**

In `.github/workflows/deploy.yml`, replace the whole `Tell Komodo to deploy config` step (the one building an HMAC with `openssl dgst`) with:

```yaml
      - name: Tell Komodo to deploy config
        if: github.event_name == 'workflow_dispatch' || steps.images.outputs.config_agent == 'true'
        uses: ./.github/actions/komodo/deploy-stack
        with:
          komodo-url: ${{ secrets.KOMODO_URL }}
          api-key: ${{ secrets.KOMODO_API_KEY }}
          api-secret: ${{ secrets.KOMODO_API_SECRET }}
          stack: config
```

- [ ] **Step 2: Replace the inline sync step**

Replace the whole `Render stacks.toml and push it into the sync` step with:

```yaml
      - name: Render stacks.toml and apply it
        if: github.event_name == 'workflow_dispatch' || steps.sync.outputs.stacks == 'true'
        uses: ./.github/actions/komodo/run-sync
        with:
          komodo-url: ${{ secrets.KOMODO_URL }}
          api-key: ${{ secrets.KOMODO_API_KEY }}
          api-secret: ${{ secrets.KOMODO_API_SECRET }}
          sync: homelab
          contents-file: komodo/stacks.toml
```

Also update that job's `paths-filter` to watch the new vars location:

```yaml
          filters: |
            stacks:
              - 'komodo/stacks.toml'
              - '.github/actions/komodo/vars.env'
```

- [ ] **Step 3: Move the vars file and point bootstrap at it**

```bash
git rm -q komodo/vars.env
```

In `scripts/bootstrap.sh`, inside `seed_resource_sync`, change the render line from `. komodo/vars.env` to:

```bash
  rendered=$(set -a; . .github/actions/komodo/vars.env; set +a; envsubst '${HOMELAB_LAN_IP}' < komodo/stacks.toml)
```

- [ ] **Step 4: Verify the workflow parses and nothing still references the old path**

```bash
python3 -c 'import yaml; d=yaml.safe_load(open(".github/workflows/deploy.yml")); print("jobs:", list(d["jobs"]))'
bash -n scripts/bootstrap.sh && echo "bootstrap syntax ok"
! grep -rn 'komodo/vars.env' --include='*.yml' --include='*.sh' . | grep -v '^./docs/' | grep . && echo "no stale vars path in code"
grep -c 'openssl dgst' .github/workflows/deploy.yml || echo "hmac signing gone"
```

Expected: two jobs, `bootstrap syntax ok`, `no stale vars path in code`, `hmac signing gone`. Prose under `docs/` keeps the old path on purpose (it describes the move); README and LEARNING are corrected in Task 12.

- [ ] **Step 5: Verify bootstrap's render still produces the applied config**

```bash
rendered=$(set -a; . .github/actions/komodo/vars.env; set +a; envsubst '${HOMELAB_LAN_IP}' < komodo/stacks.toml)
diff <(grep -v '^#' <<<"$rendered") <(git show origin/main:komodo/stacks.toml | grep -v '^#') \
  && echo "rendered output matches what is live"
```

Expected: `rendered output matches what is live`.

- [ ] **Step 6: Commit**

```bash
git add -A .github komodo scripts
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "use the komodo actions from the homelab workflow"
```

---

### Task 10: Grant the CI user Execute on `config`, and drop the webhook secret

The workflow now deploys `config` through the API, so the `homelab-ci` service user needs Execute on that stack. Without this the job fails at runtime, not at parse time.

**Files:**
- No repo files. This is live configuration plus a secret deletion.

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Grant Execute on the config stack**

```bash
cd /mnt/Data/work/homelab
set -a; . ./komodo/.env; set +a
K=https://komodo.sussman.win
jwt=$(curl -s -X POST $K/auth/login -H 'Content-Type: application/json' \
  -d "{\"type\":\"LoginLocalUser\",\"params\":{\"username\":\"$KOMODO_INIT_ADMIN_USERNAME\",\"password\":\"$KOMODO_INIT_ADMIN_PASSWORD\"}}" | jq -r .data.jwt)
uid=$(curl -s -X POST $K/read -H "Authorization: Bearer $jwt" -H 'Content-Type: application/json' \
  -d '{"type":"ListUsers","params":{}}' | jq -r '.[] | select(.username=="homelab-ci") | ._id."$oid"')
curl -s -X POST $K/write -H "Authorization: Bearer $jwt" -H 'Content-Type: application/json' \
  -d "{\"type\":\"UpdatePermissionOnTarget\",\"params\":{
    \"user_target\":{\"type\":\"User\",\"id\":\"$uid\"},
    \"resource_target\":{\"type\":\"Stack\",\"id\":\"config\"},
    \"permission\":{\"level\":\"Execute\"}}}" | jq -c '{err: .error}'
```

Expected: `{"err":null}`.

- [ ] **Step 2: Verify the grant with a throwaway key, then delete that key**

Mint a temporary key for `homelab-ci`, call `DeployStack` on `config`, and confirm the resulting Update reports `success: true` — remember a 2xx alone proves nothing. Then delete the temporary key with `DeleteApiKeyForServiceUser`. Full command shape is in the `komodo-api-access` and `komodo-execute-async-permission-failures` memories.

Expected: the Update for that deploy shows `success: true`, and the config containers keep running.

- [ ] **Step 3: Remove the now-unused CI secret**

```bash
gh secret delete KOMODO_WEBHOOK_SECRET -R lorainemg/homelab
gh secret list -R lorainemg/homelab | awk '{print $1}'
```

Expected: `KOMODO_WEBHOOK_SECRET` absent, `KOMODO_API_KEY` and `KOMODO_API_SECRET` present.

Note: this deletes a GitHub secret. The value still exists as `KOMODO_WEBHOOK_SECRET` in `komodo/.env` on the workstation and on the server, so it is recoverable; the per-stack GitHub webhooks that Komodo listens on are unaffected and keep using it.

---

### Task 11: Rewrite the bot repo's workflow to use the actions

**Files:**
- Modify: `/mnt/Data/study/traktv-tg-bot/.github/workflows/deploy-main.yml`

**Interfaces:**
- Consumes: `update-stack` and `deploy-stack`, referenced cross-repo.
- Produces: nothing later depends on.

- [ ] **Step 1: Branch from the bot repo's main**

```bash
cd /mnt/Data/study/traktv-tg-bot
git fetch -q origin
git checkout -b use-komodo-action origin/main
```

- [ ] **Step 2: Replace all three Komodo steps**

Delete the `Ensure the Komodo stack exists`, `Push the generated compose and env to the stack` and `Deploy the stack and wait for the result` steps. In their place:

```yaml
      # The dashboard's host port is read back from Aspire's own output, so it
      # is declared once, in apphost.cs. ${HOMELAB_LAN_IP} is expanded by the
      # action from the homelab repo's vars.env, which is checked out with it.
      - name: Read the dashboard's published port
        id: dash
        shell: bash
        run: |
          set -euo pipefail
          port=$(yq '.services[] | select(.container_name == "aspire") | .ports[0]' \
                   aspire-output/docker-compose.yaml | cut -d: -f1)
          [ -n "$port" ] || { echo "no published dashboard port in the compose output" >&2; exit 1; }
          echo "port=$port" >> "$GITHUB_OUTPUT"

      - name: Push the generated compose and env to the stack
        uses: lorainemg/homelab/.github/actions/komodo/update-stack@main
        with:
          komodo-url: ${{ env.KOMODO_URL }}
          api-key: ${{ env.KOMODO_API_KEY }}
          api-secret: ${{ env.KOMODO_API_SECRET }}
          stack: trakt-tg-bot
          create-if-missing: true
          compose-file: aspire-output/docker-compose.yaml
          env-file: aspire-output/.env.production
          links: http://${HOMELAB_LAN_IP}:${{ steps.dash.outputs.port }}/login?t=${{ secrets.ASPIRE_BROWSER_TOKEN }}

      - name: Deploy the stack and wait for the result
        uses: lorainemg/homelab/.github/actions/komodo/deploy-stack@main
        with:
          komodo-url: ${{ env.KOMODO_URL }}
          api-key: ${{ env.KOMODO_API_KEY }}
          api-secret: ${{ env.KOMODO_API_SECRET }}
          stack: trakt-tg-bot
```

- [ ] **Step 3: Verify the workflow parses and the boilerplate is gone**

```bash
python3 -c 'import yaml; yaml.safe_load(open(".github/workflows/deploy-main.yml")); print("parses")'
grep -c 'X-Api-Key' .github/workflows/deploy-main.yml || echo "no hand-rolled auth left"
grep -c 'GetUpdate' .github/workflows/deploy-main.yml || echo "no hand-rolled polling left"
wc -l .github/workflows/deploy-main.yml
```

Expected: `parses`, `no hand-rolled auth left`, `no hand-rolled polling left`, and a line count near 110 (down from 168).

- [ ] **Step 4: Commit and open the PR**

```bash
git add .github/workflows/deploy-main.yml
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "deploy through the shared komodo action instead of hand-rolled curl"
git push -u origin use-komodo-action
gh pr create --base main --head use-komodo-action \
  --title "deploy through the shared komodo action instead of hand-rolled curl" \
  --body "Replaces this repo's hand-rolled Komodo curl with the shared actions in the homelab repo. The LAN address comes from the action's own vars.env, so nothing is fetched at deploy time.

**Merge order:** lorainemg/homelab#6 must merge first. This references the action at \`@main\`, and it does not resolve until then. Nothing breaks in the meantime: this workflow only runs on push to main and workflow_dispatch, so PR checks here never touch it."
```

- [ ] **Step 5: Close the superseded PR**

```bash
gh pr close 12 --comment "Superseded: the LAN address now comes from the action's own vars.env, so there is nothing to fetch."
```

---

### Task 12: Update the prose, then verify live

**Files:**
- Modify: `README.md`
- Modify: `LEARNING.md`
- Modify: PR #6's description

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Update the README's CI/CD section**

Rewrite the `sync-komodo` paragraph so it describes the action rather than inline curl, and correct the secrets list: `KOMODO_URL`, `KOMODO_API_KEY` and `KOMODO_API_SECRET`, with `KOMODO_WEBHOOK_SECRET` no longer held by CI. State that `config` now deploys through the API like every other stack, and that the shared values live in `.github/actions/komodo/vars.env`. Also correct the rebuild step 4 secrets list and the `bootstrap.sh` paragraph's vars path.

- [ ] **Step 2: Add the LEARNING.md entry**

Add under `## Covered`, replacing nothing:

```markdown
- **Composite actions share code by sourcing a sibling file, not by nested
  `uses:`** — a relative `uses: ./...` inside a composite action resolves
  against the *caller's* workspace, so it breaks the moment another repo uses
  the action. Sourcing `$GITHUB_ACTION_PATH/../lib/komodo.sh` works because
  GitHub checks out the whole action repository, not just the action's own
  directory. That same fact is what lets the bot repo read this repo's
  `vars.env` without fetching anything: the file is physically on the runner
  beside the action. One shared library, one copy of the LAN address, and
  ~140 lines of hand-rolled curl deleted across two repos. (2026-09-04)
```

- [ ] **Step 3: Commit the prose**

```bash
cd /mnt/Data/work/homelab
git add README.md LEARNING.md
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "document the komodo action and drop the webhook secret from ci"
git push origin sync-from-ci
```

- [ ] **Step 4: Update PR #6's description**

Rewrite it to lead with the action rather than the inline job, note that `KOMODO_WEBHOOK_SECRET` is gone from CI, that `config` now deploys through the API, and that the bot repo's PR should merge after this one because it references `@main`.

- [ ] **Step 5: Live verification after merge**

Merge PR #6, then trigger `workflow_dispatch` on the homelab repo and confirm three things:

1. The run is green and the `run-sync` step printed `komodo update ... completed`.
2. The sync's pending diff is empty afterwards — read `.info.resource_updates` on `GetResourceSync`, **not** `.info.pending.data`, which does not exist and silently reads as zero.
3. `GetResourceSync` shows `config.repo` empty and a non-empty `file_contents`, proving the repo-to-contents switch happened.

Then confirm the `config` stack's containers are still running and that Caddy was not disturbed, since a `config` deploy severs the response path it reports on.
