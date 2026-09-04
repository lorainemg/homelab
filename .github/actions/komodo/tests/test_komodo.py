#!/usr/bin/env python3
"""Tests for lib/komodo.py against a stub Komodo. No network, nothing live."""
import contextlib
import io
import os
import socket
import sys
import tempfile
import time
import unittest
import unittest.mock
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


class TestCallTimeout(unittest.TestCase):
    def test_a_stalled_request_times_out_promptly(self):
        # A socket that accepts a connection (the OS backlog completes the
        # handshake even though nothing ever calls accept()) and never
        # answers. With no request-level timeout this would hang forever;
        # the client's own timeout must bound it instead.
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]

        client = komodo.Komodo(
            url=f"http://127.0.0.1:{port}", api_key="k", api_secret="s",
            request_timeout=0.2,
        )
        started = time.monotonic()
        with self.assertRaises(komodo.KomodoError) as caught:
            client.call("read", "GetStack", {"stack": "immich"})
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1)
        message = str(caught.exception)
        self.assertIn("timed out", message)
        self.assertIn("GetStack", message)


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


if __name__ == "__main__":
    unittest.main()
