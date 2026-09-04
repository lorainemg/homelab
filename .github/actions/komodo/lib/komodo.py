#!/usr/bin/env python3
"""A small client for the Komodo API, used by the composite actions beside it.

Komodo Core 2.3.1. Everything here is standard library on purpose: this runs on
a GitHub runner and on a freshly installed homelab server, with nothing to pip
install in either place.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


class KomodoError(RuntimeError):
    """Anything Komodo refused, or any response we could not use."""


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


class Komodo:
    def __init__(self, url, api_key, api_secret, poll_interval=5, request_timeout=30):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.poll_interval = poll_interval
        self.request_timeout = request_timeout

    def call(self, route, rtype, params):
        """POST one request to /read, /write or /execute and return the body.

        The error message deliberately carries the request *type* and Komodo's
        own error text, never the request body: a stack's `environment` is in
        there, and this text ends up in a public CI log.

        `request_timeout` bounds a single stalled request (a hung peer that
        accepts a connection and never answers). It is deliberately separate
        from `await_update`'s overall `timeout`: one stuck request must fail
        fast so the polling loop can decide whether its own budget is spent,
        rather than the whole job hanging until GitHub's job-level limit.
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
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                return json.loads(response.read() or "{}")
        except urllib.error.HTTPError as error:
            detail = self._error_text(error.read())
            raise KomodoError(
                f"komodo {rtype} failed (HTTP {error.code}): {detail}"
            ) from None
        except TimeoutError:
            raise KomodoError(
                f"komodo {rtype} failed: request timed out after {self.request_timeout}s"
            ) from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise KomodoError(
                    f"komodo {rtype} failed: request timed out after {self.request_timeout}s"
                ) from None
            raise KomodoError(
                f"komodo {rtype} failed: could not reach {self.url} ({error.reason})"
            ) from None

    @staticmethod
    def _error_text(raw):
        try:
            return json.loads(raw).get("error", "no error field")
        except (ValueError, AttributeError):
            return "response was not json"

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
