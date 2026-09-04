#!/usr/bin/env python3
"""A small client for the Komodo API, used by the composite actions beside it.

Komodo Core 2.3.1. Everything here is standard library on purpose: this runs on
a GitHub runner and on a freshly installed homelab server, with nothing to pip
install in either place.
"""
import json
import os
import sys
import time
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
