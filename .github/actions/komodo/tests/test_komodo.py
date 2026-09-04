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


if __name__ == "__main__":
    unittest.main()
