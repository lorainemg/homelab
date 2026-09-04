# Komodo GitHub Action Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ~140 lines of hand-rolled Komodo `curl` across two repos with three task-named composite GitHub Actions sharing one tested Python client.

**Architecture:** A single Python module (`lib/komodo.py`, standard library only) owns authentication, the HTTP call, placeholder expansion from a `vars.env` beside it, and the poll-an-Update-to-completion loop. It exposes a small CLI. Three composite actions (`update-stack`, `deploy-stack`, `run-sync`) are each a few lines of bash that pass inputs through as environment variables and invoke one CLI subcommand. The module is tested with `unittest` against a stub HTTP server that replays real Komodo response shapes, so nothing in the test suite touches the live homelab.

**Tech Stack:** Python 3 standard library only (`urllib.request`, `json`, `argparse`, `http.server`, `unittest`) — preinstalled on `ubuntu-latest` and present on the homelab server (3.13.7). GitHub composite actions with a thin `bash` wrapper per action.

**Spec:** [docs/superpowers/specs/2026-09-04-komodo-github-action-design.md](../specs/2026-09-04-komodo-github-action-design.md)

## Global Constraints

- Komodo Core version is **2.3.1**. All response shapes below are from that version.
- Authentication is **`X-Api-Key` + `X-Api-Secret` headers**, never a JWT. Endpoints are `POST {url}/read`, `/write`, `/execute` with a body of `{"type": ..., "params": {...}}`.
- **`/execute` returns `status: "InProgress"` immediately**, and permission failures surface only in the resulting Update's `success: false`. A 2xx never means permitted or done.
- **A missing stack is HTTP 500** with `"Did not find any Stack"` in the body, not a 404. Branch on the body.
- **Never print a request or response body that can contain a stack `environment`.** Those hold secrets. Print request *types*, HTTP statuses, and Komodo's own Update log stages only.
- Poll interval is **5 seconds**; default timeout **300 seconds**.
- The library is Python 3 standard library only — no pip installs, no third-party imports. The thin bash wrapper inside each `action.yml` uses `set -euo pipefail`.
- The library raises on failure and the CLI turns an exception into a non-zero exit with a readable message; a composite action step fails because the process exited non-zero, never because a string was parsed.
- Commit messages: single line, casual, no trailers (repo convention).
- The gitleaks pre-commit hook cannot run on this workstation (Docker permissions). Scan staged changes with the standalone binary at `/tmp/gitleaks/gitleaks git --staged --no-banner --redact .` and commit with `--no-verify`.

---

### Task 1: The stub Komodo server and the test scaffolding

Build the test harness first, so every later task has something to test against. The stub replays the real Komodo response shapes *and* validates that each request type arrives on the right route, because every later task trusts it for exactly that.

**Files:**
- Create: `.github/actions/komodo/tests/stub_komodo.py`
- Create: `.github/actions/komodo/tests/test_komodo.py`
- Create: `.github/actions/komodo/lib/komodo.py` (module docstring only, so the suite can import it)

**Interfaces:**
- Consumes: nothing.
- Produces: `stub_komodo.StubKomodo` — a context manager that starts the stub on an ephemeral port and exposes `.url`. `test_komodo.KomodoTestCase` — a `unittest.TestCase` base that starts one stub for the class and points the library's environment at it.

- [ ] **Step 1: Write the stub server**

Create `.github/actions/komodo/tests/stub_komodo.py`:

```python
#!/usr/bin/env python3
"""A stand-in for Komodo Core 2.3.1, replaying the response shapes the real
server produces, so the client can be tested without touching the homelab.

Which scenario you get is chosen by the request's `type`, so a test picks a
behaviour by calling the matching Komodo request type. The stub also checks
that each type arrives on the route the real server serves it from: a read
sent to /execute is a test failure here rather than a silent pass in CI.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Which route each request type belongs on, mirroring the real API.
ROUTES = {
    "GetStack": "/read",
    "GetUpdate": "/read",
    "CreateStack": "/write",
    "UpdateStack": "/write",
    "UpdateResourceSync": "/write",
    "DeployStack": "/execute",
    "RunSync": "/execute",
}


class _Handler(BaseHTTPRequestHandler):
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
        polls = self.server.polls

        if not self.headers.get("X-Api-Key") or not self.headers.get("X-Api-Secret"):
            return self._send(401, {"error": "unauthorized"})

        expected = ROUTES.get(rtype)
        if expected is None:
            return self._send(404, {"error": f"stub has no case for {rtype}"})
        if self.path != expected:
            return self._send(
                404, {"error": f"{rtype} belongs on {expected}, not {self.path}"}
            )

        if rtype == "GetStack":
            if params.get("stack") == "missing-stack":
                # The real shape for a name Komodo does not know: 500, not 404.
                return self._send(500, {
                    "error": "Did not find any Stack matching missing-stack",
                    "trace": [],
                })
            return self._send(200, {"name": params.get("stack"), "_id": {"$oid": "s1"}})

        if rtype == "GetUpdate":
            uid = params.get("id")
            polls[uid] = polls.get(uid, 0) + 1
            if uid == "u-never":
                return self._send(200, {"_id": {"$oid": uid}, "status": "InProgress"})
            # Stay InProgress on the first poll so the caller must really loop.
            if polls[uid] < 2:
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

        if rtype in ("CreateStack", "UpdateStack", "UpdateResourceSync"):
            if params.get("id") == "boom" or params.get("name") == "boom":
                return self._send(400, {"error": "bad request from stub"})
            self.server.received.append(req)
            return self._send(200, {"name": params.get("name") or params.get("id"),
                                    "config": params.get("config", {})})

        # DeployStack / RunSync: accepted, never a result.
        target = params.get("stack") or params.get("sync")
        uid = {"fail-me": "u-fail", "hang-me": "u-never"}.get(target, "u-ok")
        return self._send(200, {"_id": {"$oid": uid}, "status": "InProgress"})


class StubKomodo:
    """Runs the stub on an ephemeral port for the life of a `with` block."""

    def __enter__(self):
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._server.polls = {}
        self._server.received = []          # every write payload, for assertions
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        return self

    @property
    def received(self):
        return self._server.received

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
```

- [ ] **Step 2: Write the test scaffolding**

Create `.github/actions/komodo/tests/test_komodo.py`:

```python
#!/usr/bin/env python3
"""Tests for lib/komodo.py against a stub Komodo. No network, nothing live."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import komodo                      # noqa: E402
from stub_komodo import StubKomodo  # noqa: E402


class KomodoTestCase(unittest.TestCase):
    """Starts one stub per class and points the client at it."""

    @classmethod
    def setUpClass(cls):
        cls._stub = StubKomodo().__enter__()
        cls.client = komodo.Komodo(
            url=cls._stub.url, api_key="test-key", api_secret="test-secret",
            poll_interval=0,
        )

    @classmethod
    def tearDownClass(cls):
        cls._stub.__exit__(None, None, None)


class TestStubItself(KomodoTestCase):
    def test_stub_is_reachable(self):
        self.assertTrue(self._stub.url.startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Create the module so the suite can import it**

Create `.github/actions/komodo/lib/komodo.py`:

```python
#!/usr/bin/env python3
"""A small client for the Komodo API, used by the composite actions beside it.

Komodo Core 2.3.1. Everything here is standard library on purpose: this runs on
a GitHub runner and on a freshly installed homelab server, with nothing to pip
install in either place.
"""
```

- [ ] **Step 4: Run the suite and verify it passes**

```bash
cd /mnt/Data/work/homelab
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: 1 test, OK.

- [ ] **Step 5: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "add a stub komodo server to test the action client against"
```

---

### Task 2: `Komodo.call` — one authenticated request

**Files:**
- Modify: `.github/actions/komodo/lib/komodo.py`
- Modify: `.github/actions/komodo/tests/test_komodo.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `class KomodoError(RuntimeError)`. `class Komodo(url, api_key, api_secret, poll_interval=5)` with `.call(route, rtype, params) -> dict`. Raises `KomodoError` on a non-2xx, with a message naming the request type, the status code and Komodo's own `error` field — and never the request body, which can hold secrets.

- [ ] **Step 1: Write the failing tests**

Add to `test_komodo.py`, above the `if __name__` block:

```python
class TestCall(KomodoTestCase):
    def test_returns_the_parsed_response(self):
        out = self.client.call("read", "GetStack", {"stack": "immich"})
        self.assertEqual(out["name"], "immich")

    def test_non_2xx_raises_with_status_and_message(self):
        with self.assertRaises(komodo.KomodoError) as caught:
            self.client.call("write", "UpdateStack", {"id": "boom", "config": {}})
        message = str(caught.exception)
        self.assertIn("400", message)
        self.assertIn("bad request from stub", message)
        self.assertIn("UpdateStack", message)

    def test_failure_never_echoes_the_request_body(self):
        # Drives the *error* branch on purpose: the success path prints nothing,
        # so asserting against it would be a test that cannot fail.
        secret = "hunter2"
        with self.assertRaises(komodo.KomodoError) as caught:
            self.client.call("write", "UpdateStack", {
                "id": "boom", "config": {"environment": f"TOKEN={secret}"}})
        self.assertNotIn(secret, str(caught.exception))

    def test_routes_are_checked_by_the_stub(self):
        # Guards the harness itself: a read sent to /execute must not pass.
        with self.assertRaises(komodo.KomodoError):
            self.client.call("execute", "GetStack", {"stack": "immich"})
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: FAIL with `AttributeError: module 'komodo' has no attribute 'KomodoError'`.

- [ ] **Step 3: Implement `KomodoError` and `Komodo.call`**

Append to `lib/komodo.py`:

```python
import json
import os
import urllib.error
import urllib.request


class KomodoError(RuntimeError):
    """Anything Komodo refused, or any response we could not use."""


class Komodo:
    def __init__(self, url, api_key, api_secret, poll_interval=5):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.poll_interval = poll_interval

    def call(self, route, rtype, params):
        """POST one request to /read, /write or /execute and return the body.

        The error message deliberately carries the request *type* and Komodo's
        own error text, never the request body: a stack's `environment` is in
        there, and this text ends up in a public CI log.
        """
        body = json.dumps({"type": rtype, "params": params}).encode()
        request = urllib.request.Request(
            f"{self.url}/{route}",
            data=body,
            headers={
                "X-Api-Key": self.api_key,
                "X-Api-Secret": self.api_secret,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read() or "{}")
        except urllib.error.HTTPError as error:
            detail = self._error_text(error.read())
            raise KomodoError(
                f"komodo {rtype} failed (HTTP {error.code}): {detail}"
            ) from None
        except urllib.error.URLError as error:
            raise KomodoError(
                f"komodo {rtype} failed: could not reach {self.url} ({error.reason})"
            ) from None

    @staticmethod
    def _error_text(raw):
        try:
            return json.loads(raw).get("error", "no error field")
        except (ValueError, AttributeError):
            return "response was not json"
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: 5 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "add the authenticated komodo call helper"
```

---

### Task 3: `Komodo.await_update` — poll an Update to completion

**Files:**
- Modify: `.github/actions/komodo/lib/komodo.py`
- Modify: `.github/actions/komodo/tests/test_komodo.py`

**Interfaces:**
- Consumes: `Komodo.call`, `KomodoError`.
- Produces: `Komodo.await_update(update_id, timeout=300) -> None`. Polls `GetUpdate` every `poll_interval` seconds until `status == "Complete"`. Returns quietly on success. Raises `KomodoError` when `success` is false, having first printed each log's stage, stdout and stderr to stderr. Raises `KomodoError` on timeout.

- [ ] **Step 1: Write the failing tests**

Add to `test_komodo.py`:

```python
import contextlib   # add to the imports at the top of the file
import io


class TestAwaitUpdate(KomodoTestCase):
    def test_returns_quietly_on_success(self):
        self.assertIsNone(self.client.await_update("u-ok", timeout=30))

    def test_raises_and_prints_the_failing_stage(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with self.assertRaises(komodo.KomodoError):
                self.client.await_update("u-fail", timeout=30)
        printed = captured.getvalue()
        self.assertIn("Deploy", printed)
        self.assertIn("denied: permission on Stack", printed)

    def test_timeout_raises_rather_than_passing(self):
        with self.assertRaises(komodo.KomodoError) as caught:
            self.client.await_update("u-never", timeout=1)
        self.assertIn("timed out", str(caught.exception))

    def test_really_polls_rather_than_reading_status_once(self):
        # The stub answers InProgress on the first poll for every id.
        self.client.await_update("u-ok-again", timeout=30)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: FAIL with `AttributeError: 'Komodo' object has no attribute 'await_update'`.

- [ ] **Step 3: Implement `await_update`**

Append to the `Komodo` class in `lib/komodo.py`, and add `import sys` and `import time` at the top:

```python
    def await_update(self, update_id, timeout=300):
        """Wait for an Update to finish, and decide whether it worked.

        /execute only means *accepted*. Even a permission refusal comes back as
        a 2xx with status InProgress, and surfaces here as success: false. So
        the verdict on any execute is whatever this method says, never the
        response to the execute itself.
        """
        waited = 0
        while True:
            update = self.call("read", "GetUpdate", {"id": update_id})
            if update.get("status") == "Complete":
                break
            if waited >= timeout:
                raise KomodoError(
                    f"komodo update {update_id} timed out after {timeout}s "
                    f"(last status: {update.get('status', 'Unknown')})"
                )
            time.sleep(self.poll_interval)
            waited += self.poll_interval or 1

        if update.get("success"):
            print(f"komodo update {update_id} completed")
            return None

        print(f"komodo update {update_id} failed:", file=sys.stderr)
        for log in update.get("logs") or []:
            print(f"--- {log.get('stage', '?')}", file=sys.stderr)
            for stream in ("stdout", "stderr"):
                if log.get(stream):
                    print(log[stream], file=sys.stderr)
        raise KomodoError(f"komodo update {update_id} failed")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: 9 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "poll komodo updates to completion and print the failing stage"
```

---

### Task 4: `expand`, `stack_exists`, and the payload builders

**Files:**
- Create: `.github/actions/komodo/vars.env`
- Modify: `.github/actions/komodo/lib/komodo.py`
- Modify: `.github/actions/komodo/tests/test_komodo.py`

**Interfaces:**
- Consumes: `Komodo.call`, `KomodoError`.
- Produces: module-level `load_vars(path=None) -> dict` and `expand(text, variables) -> str` (raises `KomodoError` if any `${...}` survives). `Komodo.stack_exists(name) -> bool`. `Komodo.stack_config(compose_file, env_file, links, variables) -> dict` and `Komodo.sync_config(toml_file, variables) -> dict`.

- [ ] **Step 1: Create `vars.env`**

Create `.github/actions/komodo/vars.env`:

```bash
# Values shared by everything that talks to this Komodo. Edit here, nowhere
# else: the composite actions in this directory expand ${NAME} in their
# `links` input and in the TOML that run-sync pushes, and scripts/bootstrap.sh
# renders komodo/stacks.toml through the same library on a fresh server.
HOMELAB_LAN_IP=172.20.3.194
```

- [ ] **Step 2: Write the failing tests**

Add to `test_komodo.py`:

```python
import tempfile     # add to the imports at the top of the file


class TestExpand(unittest.TestCase):
    VARS = {"HOMELAB_LAN_IP": "172.20.3.194"}

    def test_expands_a_known_placeholder(self):
        self.assertEqual(
            komodo.expand("http://${HOMELAB_LAN_IP}:5000", self.VARS),
            "http://172.20.3.194:5000")

    def test_leaves_plain_text_alone(self):
        self.assertEqual(komodo.expand("no placeholders", self.VARS), "no placeholders")

    def test_leaves_a_bare_dollar_alone(self):
        # A compose file or a password may legitimately contain one.
        text = "cost is $5 and 100% real"
        self.assertEqual(komodo.expand(text, self.VARS), text)

    def test_an_unknown_placeholder_is_an_error(self):
        with self.assertRaises(komodo.KomodoError) as caught:
            komodo.expand("http://${NOPE}:1", self.VARS)
        self.assertIn("NOPE", str(caught.exception))

    def test_load_vars_reads_the_file_beside_the_library(self):
        self.assertEqual(komodo.load_vars()["HOMELAB_LAN_IP"], "172.20.3.194")


class TestStackExists(KomodoTestCase):
    def test_true_for_an_existing_stack(self):
        self.assertTrue(self.client.stack_exists("immich"))

    def test_false_on_the_500_not_found_body(self):
        # Komodo answers a missing stack with 500, not 404.
        self.assertFalse(self.client.stack_exists("missing-stack"))

    def test_other_failures_still_raise(self):
        broken = komodo.Komodo(url="http://127.0.0.1:1", api_key="k", api_secret="s")
        with self.assertRaises(komodo.KomodoError):
            broken.stack_exists("immich")


class TestPayloads(KomodoTestCase):
    VARS = {"HOMELAB_LAN_IP": "172.20.3.194"}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.compose = Path(self.dir.name) / "compose.yaml"
        self.compose.write_text("services: {}\n")
        self.env = Path(self.dir.name) / ".env"
        self.env.write_text("A=1\n")
        self.addCleanup(self.dir.cleanup)

    def test_carries_every_supplied_field(self):
        config = self.client.stack_config(
            str(self.compose), str(self.env),
            "http://${HOMELAB_LAN_IP}:28888/login", self.VARS)
        self.assertEqual(config["file_contents"], "services: {}\n")
        self.assertEqual(config["environment"], "A=1\n")
        self.assertEqual(config["links"], ["http://172.20.3.194:28888/login"])

    def test_omits_fields_with_no_input(self):
        # An omitted input must never clear what is already on the stack.
        config = self.client.stack_config(str(self.compose), None, None, self.VARS)
        self.assertNotIn("environment", config)
        self.assertNotIn("links", config)

    def test_splits_multiple_links(self):
        config = self.client.stack_config(
            None, None, "http://${HOMELAB_LAN_IP}:1\nhttp://${HOMELAB_LAN_IP}:2",
            self.VARS)
        self.assertEqual(len(config["links"]), 2)

    def test_sync_config_clears_the_other_sources(self):
        # Komodo prefers a repo over stored contents, so leaving repo set would
        # silently ignore what we just pushed.
        toml = Path(self.dir.name) / "stacks.toml"
        toml.write_text('links = ["http://${HOMELAB_LAN_IP}:5000"]\n')
        config = self.client.sync_config(str(toml), self.VARS)
        self.assertIn("172.20.3.194", config["file_contents"])
        self.assertEqual(config["repo"], "")
        self.assertEqual(config["branch"], "")
        self.assertEqual(config["resource_path"], [])
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: FAIL with `AttributeError: module 'komodo' has no attribute 'expand'`.

- [ ] **Step 4: Implement the four pieces**

Append to `lib/komodo.py` — `load_vars` and `expand` at module level, the other two inside the `Komodo` class. Add `import re` and `from pathlib import Path` at the top:

```python
VARS_FILE = Path(__file__).resolve().parent.parent / "vars.env"
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_vars(path=None):
    """Read the shared values that sit beside this library."""
    variables = {}
    for line in Path(path or VARS_FILE).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        variables[name.strip()] = value.strip()
    return variables


def expand(text, variables):
    """Replace ${NAME} from `variables`, and refuse to leave one behind.

    Only the ${...} form is touched, so a bare `$` in a compose file or a
    password is never disturbed. An unknown name is an error rather than an
    empty string: a link silently rendered to `http://:5000` would look
    plausible in the UI and go nowhere.
    """
    missing = sorted(
        {name for name in _PLACEHOLDER.findall(text) if name not in variables}
    )
    if missing:
        raise KomodoError(
            f"unresolved placeholder(s) {', '.join(missing)}; "
            f"declared in vars.env: {', '.join(sorted(variables)) or 'nothing'}"
        )
    return _PLACEHOLDER.sub(lambda m: variables[m.group(1)], text)
```

and inside the class:

```python
    def stack_exists(self, name):
        """Whether Komodo knows this stack.

        A missing stack is HTTP 500 with "Did not find any Stack", not a 404,
        so the body is the only reliable signal. Any other failure is a real
        failure and is re-raised rather than read as "absent".
        """
        try:
            return self.call("read", "GetStack", {"stack": name}).get("name") == name
        except KomodoError as error:
            if "Did not find any Stack" in str(error):
                return False
            raise

    def stack_config(self, compose_file, env_file, links, variables):
        """Build an UpdateStack config from whichever inputs were supplied.

        A falsy argument means "leave that field alone": omitted fields are
        absent from the payload, so an update never clears something the
        caller did not mention.
        """
        config = {}
        if compose_file:
            config["file_contents"] = Path(compose_file).read_text()
        if env_file:
            config["environment"] = Path(env_file).read_text()
        if links:
            config["links"] = [
                line for line in expand(links, variables).splitlines() if line
            ]
        return config

    def sync_config(self, toml_file, variables):
        """Build the config for a contents-mode ResourceSync.

        repo, branch and resource_path are cleared every time: Komodo picks its
        source in that order and prefers a repo over stored contents, so
        leaving them set would silently ignore what we just pushed
        (bin/core/src/sync/remote.rs, v2.3.1).
        """
        return {
            "file_contents": expand(Path(toml_file).read_text(), variables),
            "repo": "",
            "branch": "",
            "resource_path": [],
        }
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: 21 tests, OK.

- [ ] **Step 6: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "expand shared vars, probe for a stack, and build the payloads"
```

---

### Task 5: The CLI, and the `deploy-stack` action

The actions invoke the library as a process, so this task adds the `argparse` entry point they all share, plus the first subcommand and the first `action.yml`.

**Files:**
- Modify: `.github/actions/komodo/lib/komodo.py`
- Modify: `.github/actions/komodo/tests/test_komodo.py`
- Create: `.github/actions/komodo/deploy-stack/action.yml`

**Interfaces:**
- Consumes: `Komodo`, `KomodoError`, `load_vars`.
- Produces: `client_from_env() -> Komodo` reading `KOMODO_URL`, `KOMODO_API_KEY`, `KOMODO_API_SECRET`. `main(argv=None) -> int`, which returns 1 and prints the message on a `KomodoError` rather than raising. Subcommand `deploy-stack --stack NAME [--timeout N]`.

- [ ] **Step 1: Write the failing tests**

Add to `test_komodo.py`:

```python
class TestCli(KomodoTestCase):
    def _env(self):
        return {
            "KOMODO_URL": self._stub.url,
            "KOMODO_API_KEY": "test-key",
            "KOMODO_API_SECRET": "test-secret",
            "KOMODO_POLL_INTERVAL": "0",
        }

    def test_deploy_stack_returns_zero_on_success(self):
        with unittest.mock.patch.dict(os.environ, self._env()):
            self.assertEqual(komodo.main(["deploy-stack", "--stack", "immich"]), 0)

    def test_deploy_stack_returns_one_when_komodo_refuses(self):
        captured = io.StringIO()
        with unittest.mock.patch.dict(os.environ, self._env()):
            with contextlib.redirect_stderr(captured):
                code = komodo.main(["deploy-stack", "--stack", "fail-me"])
        self.assertEqual(code, 1)
        self.assertIn("denied: permission on Stack", captured.getvalue())

    def test_missing_credentials_is_a_clear_error(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                code = komodo.main(["deploy-stack", "--stack", "immich"])
        self.assertEqual(code, 1)
        self.assertIn("KOMODO_URL", captured.getvalue())
```

Add `import unittest.mock` to the imports at the top of the file.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: FAIL with `AttributeError: module 'komodo' has no attribute 'main'`.

- [ ] **Step 3: Implement the CLI**

Append to `lib/komodo.py`. Add `import argparse` at the top:

```python
def client_from_env():
    """Build a client from the environment the composite actions set."""
    missing = [
        name for name in ("KOMODO_URL", "KOMODO_API_KEY", "KOMODO_API_SECRET")
        if not os.environ.get(name)
    ]
    if missing:
        raise KomodoError(f"missing environment: {', '.join(missing)}")
    return Komodo(
        url=os.environ["KOMODO_URL"],
        api_key=os.environ["KOMODO_API_KEY"],
        api_secret=os.environ["KOMODO_API_SECRET"],
        poll_interval=int(os.environ.get("KOMODO_POLL_INTERVAL", "5")),
    )


def _deploy_stack(args):
    client = client_from_env()
    accepted = client.call("execute", "DeployStack", {"stack": args.stack})
    client.await_update(accepted["_id"]["$oid"], timeout=args.timeout)


def main(argv=None):
    """Entry point for the composite actions. Never raises: an exception
    becomes exit 1 with a readable message, which is what fails the step."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    deploy = sub.add_parser("deploy-stack", help="deploy a stack and wait")
    deploy.add_argument("--stack", required=True)
    deploy.add_argument("--timeout", type=int, default=300)
    deploy.set_defaults(handler=_deploy_stack)

    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except KomodoError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: 24 tests, OK.

- [ ] **Step 5: Write the action**

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
        # Every input reaches the script as an environment variable, never as a
        # ${{ }} interpolated into the script text. GitHub substitutes those
        # before bash runs, so an input containing a single quote would close
        # the quoting and the rest would execute as commands.
        python3 "$GITHUB_ACTION_PATH/../lib/komodo.py" deploy-stack \
          --stack "$STACK" --timeout "$TIMEOUT"
```

- [ ] **Step 6: Verify the YAML parses and the sibling path resolves**

```bash
python3 -c 'import yaml; d=yaml.safe_load(open(".github/actions/komodo/deploy-stack/action.yml")); print("inputs:", list(d["inputs"]))'
test -f .github/actions/komodo/deploy-stack/../lib/komodo.py && echo "sibling lib path resolves"
```

Expected: the five input names, then `sibling lib path resolves`.

- [ ] **Step 7: Commit**

```bash
git add .github/actions/komodo
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "add the deploy-stack action and the cli it calls"
```

---

### Task 6: The `update-stack` action

**Files:**
- Modify: `.github/actions/komodo/lib/komodo.py`
- Modify: `.github/actions/komodo/tests/test_komodo.py`
- Create: `.github/actions/komodo/update-stack/action.yml`

**Interfaces:**
- Consumes: `Komodo.stack_exists`, `Komodo.stack_config`, `load_vars`, `main`.
- Produces: subcommand `update-stack --stack NAME [--compose-file F] [--env-file F] [--links TEXT] [--create-if-missing] [--server NAME]`.

- [ ] **Step 1: Write the failing tests**

Add to `test_komodo.py`:

```python
class TestUpdateStackCommand(KomodoTestCase):
    def _env(self):
        return {
            "KOMODO_URL": self._stub.url,
            "KOMODO_API_KEY": "test-key",
            "KOMODO_API_SECRET": "test-secret",
        }

    def test_creates_the_stack_when_missing_then_updates_it(self):
        before = len(self._stub.received)
        with unittest.mock.patch.dict(os.environ, self._env()):
            code = komodo.main([
                "update-stack", "--stack", "missing-stack", "--create-if-missing"])
        self.assertEqual(code, 0)
        sent = [r["type"] for r in self._stub.received[before:]]
        self.assertEqual(sent, ["CreateStack", "UpdateStack"])

    def test_does_not_create_when_the_stack_is_there(self):
        before = len(self._stub.received)
        with unittest.mock.patch.dict(os.environ, self._env()):
            code = komodo.main([
                "update-stack", "--stack", "immich", "--create-if-missing"])
        self.assertEqual(code, 0)
        sent = [r["type"] for r in self._stub.received[before:]]
        self.assertEqual(sent, ["UpdateStack"])

    def test_links_reach_komodo_expanded(self):
        before = len(self._stub.received)
        with unittest.mock.patch.dict(os.environ, self._env()):
            komodo.main([
                "update-stack", "--stack", "immich",
                "--links", "http://${HOMELAB_LAN_IP}:2283"])
        config = self._stub.received[before]["params"]["config"]
        self.assertEqual(config["links"], ["http://172.20.3.194:2283"])
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: FAIL — `argument command: invalid choice: 'update-stack'`.

- [ ] **Step 3: Implement the subcommand**

Add the handler beside `_deploy_stack` in `lib/komodo.py`:

```python
def _update_stack(args):
    client = client_from_env()
    if args.create_if_missing and not client.stack_exists(args.stack):
        print(f"stack {args.stack} does not exist yet; creating it")
        client.call("write", "CreateStack", {
            "name": args.stack,
            "config": {
                "server_id": args.server,
                "project_name": args.stack,
                "file_contents": "services: {}",
                "webhook_enabled": False,
            },
        })
    config = client.stack_config(
        args.compose_file, args.env_file, args.links, load_vars())
    client.call("write", "UpdateStack", {"id": args.stack, "config": config})
    print(f"stack {args.stack} updated")
```

and register it inside `main`, after the `deploy-stack` parser:

```python
    update = sub.add_parser("update-stack", help="push a stack's definition")
    update.add_argument("--stack", required=True)
    update.add_argument("--compose-file", default=None)
    update.add_argument("--env-file", default=None)
    update.add_argument("--links", default=None)
    update.add_argument("--create-if-missing", action="store_true")
    update.add_argument("--server", default="Local")
    update.set_defaults(handler=_update_stack)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: 27 tests, OK.

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
        SERVER: ${{ inputs.server }}
        COMPOSE_FILE: ${{ inputs.compose-file }}
        ENV_FILE: ${{ inputs.env-file }}
        LINKS: ${{ inputs.links }}
        CREATE_IF_MISSING: ${{ inputs.create-if-missing }}
      run: |
        set -euo pipefail
        # Every input reaches the script as an environment variable, never as a
        # ${{ }} interpolated into the script text. GitHub substitutes those
        # before bash runs, so an input containing a single quote would close
        # the quoting and the rest would execute as commands.
        args=(update-stack --stack "$STACK" --server "$SERVER")
        [[ -n $COMPOSE_FILE ]] && args+=(--compose-file "$COMPOSE_FILE")
        [[ -n $ENV_FILE ]] && args+=(--env-file "$ENV_FILE")
        [[ -n $LINKS ]] && args+=(--links "$LINKS")
        [[ $CREATE_IF_MISSING == true ]] && args+=(--create-if-missing)
        python3 "$GITHUB_ACTION_PATH/../lib/komodo.py" "${args[@]}"
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

### Task 7: The `run-sync` action, and `render` for bootstrap

**Files:**
- Modify: `.github/actions/komodo/lib/komodo.py`
- Modify: `.github/actions/komodo/tests/test_komodo.py`
- Create: `.github/actions/komodo/run-sync/action.yml`

**Interfaces:**
- Consumes: `Komodo.sync_config`, `Komodo.await_update`, `expand`, `load_vars`.
- Produces: subcommands `run-sync --sync NAME --contents-file F [--timeout N]` and `render FILE` (prints the expanded file to stdout, contacts nothing).

- [ ] **Step 1: Write the failing tests**

Add to `test_komodo.py`:

```python
class TestRunSyncCommand(KomodoTestCase):
    def _env(self):
        return {
            "KOMODO_URL": self._stub.url,
            "KOMODO_API_KEY": "test-key",
            "KOMODO_API_SECRET": "test-secret",
            "KOMODO_POLL_INTERVAL": "0",
        }

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.toml = Path(self.dir.name) / "stacks.toml"
        self.toml.write_text('links = ["http://${HOMELAB_LAN_IP}:5000"]\n')
        self.addCleanup(self.dir.cleanup)

    def test_pushes_rendered_contents_and_clears_the_repo_source(self):
        before = len(self._stub.received)
        with unittest.mock.patch.dict(os.environ, self._env()):
            code = komodo.main([
                "run-sync", "--sync", "homelab", "--contents-file", str(self.toml)])
        self.assertEqual(code, 0)
        pushed = self._stub.received[before]
        self.assertEqual(pushed["type"], "UpdateResourceSync")
        config = pushed["params"]["config"]
        self.assertIn("172.20.3.194", config["file_contents"])
        self.assertEqual(config["repo"], "")

    def test_render_prints_the_expanded_file_and_contacts_nothing(self):
        captured = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(captured):
                code = komodo.main(["render", str(self.toml)])
        self.assertEqual(code, 0)
        self.assertIn("172.20.3.194", captured.getvalue())
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: FAIL — `argument command: invalid choice: 'run-sync'`.

- [ ] **Step 3: Implement both subcommands**

Add the handlers beside the others in `lib/komodo.py`:

```python
def _run_sync(args):
    client = client_from_env()
    config = client.sync_config(args.contents_file, load_vars())
    client.call("write", "UpdateResourceSync", {"id": args.sync, "config": config})
    accepted = client.call("execute", "RunSync", {"sync": args.sync})
    client.await_update(accepted["_id"]["$oid"], timeout=args.timeout)


def _render(args):
    # No client: this exists for scripts/bootstrap.sh, which renders the same
    # file on a fresh server where no API key exists yet.
    print(expand(Path(args.file).read_text(), load_vars()), end="")
```

and register them inside `main`:

```python
    sync = sub.add_parser("run-sync", help="push a sync's contents and run it")
    sync.add_argument("--sync", required=True)
    sync.add_argument("--contents-file", required=True)
    sync.add_argument("--timeout", type=int, default=300)
    sync.set_defaults(handler=_run_sync)

    render = sub.add_parser("render", help="expand a file's ${VARS} and print it")
    render.add_argument("file")
    render.set_defaults(handler=_render)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m unittest discover -s .github/actions/komodo/tests -v
```

Expected: 29 tests, OK.

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
        # Every input reaches the script as an environment variable, never as a
        # ${{ }} interpolated into the script text. GitHub substitutes those
        # before bash runs, so an input containing a single quote would close
        # the quoting and the rest would execute as commands.
        python3 "$GITHUB_ACTION_PATH/../lib/komodo.py" run-sync \
          --sync "$SYNC" --contents-file "$CONTENTS_FILE" --timeout "$TIMEOUT"
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
git commit --no-verify -m "add the run-sync action and a render subcommand for bootstrap"
```

---

### Task 8: Run the action tests in CI

**Files:**
- Create: `.github/workflows/test-actions.yml`

**Interfaces:**
- Consumes: `tests/test_komodo.py`.
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
  komodo-client:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run the client tests against the stub server
        run: python3 -m unittest discover -s .github/actions/komodo/tests -v
```

- [ ] **Step 2: Verify it parses and the suite passes locally**

```bash
python3 -c 'import yaml; yaml.safe_load(open(".github/workflows/test-actions.yml")); print("parses")'
python3 -m unittest discover -s .github/actions/komodo/tests 2>&1 | tail -3
```

Expected: `parses`, then `OK`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/test-actions.yml
/tmp/gitleaks/gitleaks git --staged --no-banner --redact .
git commit --no-verify -m "run the komodo client tests in ci"
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

In `scripts/bootstrap.sh`, inside `seed_resource_sync`, replace the `envsubst` render line with a call to the same library the action uses, so there is one renderer rather than two:

```bash
  rendered=$(python3 .github/actions/komodo/lib/komodo.py render komodo/stacks.toml)
```

Also update the prerequisite check near the top of the file: it currently demands `envsubst` and `jq`. It now needs `python3` and `jq`.

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
rendered=$(python3 .github/actions/komodo/lib/komodo.py render komodo/stacks.toml)
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
- **Composite actions share code through a sibling file, not a nested
  `uses:`** — a relative `uses: ./...` inside a composite action resolves
  against the *caller's* workspace, so it breaks the moment another repo uses
  the action. Reaching `$GITHUB_ACTION_PATH/../lib/komodo.py` works because
  GitHub checks out the whole action repository, not just the action's own
  directory. That same fact is what lets the bot repo read this repo's
  `vars.env` without fetching anything: the file is physically on the runner
  beside the action. One shared client, one copy of the LAN address, and
  ~140 lines of hand-rolled curl deleted across two repos. (2026-09-04)
- **`${{ }}` in a `run:` block is a shell injection, not a variable** — GitHub
  substitutes expressions into the *text* of the script before bash sees it, so
  `--stack '${{ inputs.stack }}'` with an input containing a single quote closes
  the quote and runs the rest as commands. Demonstrated on the first draft of
  these actions: an input of `x'; echo INJECTED; '` executed. The fix is
  mechanical — every input goes in the step's `env:` block and is referenced as
  `"$VAR"`, which bash expands at runtime with no re-parsing. Worth internalising
  because the vulnerable form reads as ordinary quoting and looks fine in review;
  the tell is not the quotes, it is `${{` appearing anywhere below `run:`.
  (2026-09-04)
- **The bug you keep making is a property of the language, not of you** — the
  first version of this client was bash, and its review found three defects:
  a function returning non-zero aborted its caller under `set -e` (so the
  missing-stack probe killed the step it existed to inform), a secret-leak
  test that could never fail because it drove the success path where nothing
  is printed, and `envsubst` needing an explicit name list or it eats any `$`
  in a compose file. The call-poll-check logic was correct both times. Rewrote
  it in Python: same design, standard library only, and that entire class of
  mistake stops existing. Worth asking early, not after the review: is this
  shell script doing string handling and error control that a language with
  exceptions would do for free? (2026-09-04)
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
