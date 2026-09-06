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
from typing import ClassVar

_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]  # stub_komodo, then komodo.py

import komodo                       # noqa: E402
from stub_komodo import StubServerTestCase  # noqa: E402


class KomodoTestCase(StubServerTestCase):
    """Adds a client pointed at the stub the base class started."""

    client: ClassVar[komodo.Komodo]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.client = komodo.Komodo(
            url=cls._stub.url, api_key="test-key", api_secret="test-secret",
            poll_interval=0,
        )


class TestStubItself(KomodoTestCase):
    def test_stub_is_reachable(self) -> None:
        self.assertTrue(self._stub.url.startswith("http://127.0.0.1:"))


class TestCall(KomodoTestCase):
    def test_returns_the_parsed_response(self) -> None:
        out = self.client.call("read", "GetStack", {"stack": "immich"})
        self.assertEqual(out["name"], "immich")

    def test_non_2xx_raises_with_status_and_message(self) -> None:
        with self.assertRaises(komodo.KomodoError) as caught:
            self.client.call("write", "UpdateStack", {"id": "boom", "config": {}})
        message = str(caught.exception)
        self.assertIn("400", message)
        self.assertIn("bad request from stub", message)
        self.assertIn("UpdateStack", message)

    def test_failure_never_echoes_the_request_body(self) -> None:
        # Drives the *error* branch on purpose: the success path prints nothing,
        # so asserting against it would be a test that cannot fail.
        secret = "hunter2"
        with self.assertRaises(komodo.KomodoError) as caught:
            self.client.call("write", "UpdateStack", {
                "id": "boom", "config": {"environment": f"TOKEN={secret}"}})
        self.assertNotIn(secret, str(caught.exception))

    def test_routes_are_checked_by_the_stub(self) -> None:
        # Guards the harness itself: a read sent to /execute must not pass.
        with self.assertRaises(komodo.KomodoError):
            self.client.call("execute", "GetStack", {"stack": "immich"})

    def test_sends_a_user_agent_cloudflare_does_not_ban(self) -> None:
        # Komodo is behind Cloudflare, which answers urllib's default agent
        # with a plain-text 403 (`error code: 1010`) instead of passing it on.
        # The stub replays that, so losing the header fails here rather than
        # in a live deploy, as it did on 2026-09-06.
        self.assertFalse(komodo.USER_AGENT.startswith("Python-urllib"))
        with unittest.mock.patch.object(komodo, "USER_AGENT", "Python-urllib/3.12"):
            with self.assertRaises(komodo.KomodoError) as caught:
                self.client.call("read", "GetStack", {"stack": "immich"})
        self.assertIn("403", str(caught.exception))

    def test_a_non_json_error_body_reaches_the_message(self) -> None:
        # "response was not json" hid the one clue the 403 carried.
        with unittest.mock.patch.object(komodo, "USER_AGENT", "Python-urllib/3.12"):
            with self.assertRaises(komodo.KomodoError) as caught:
                self.client.call("read", "GetStack", {"stack": "immich"})
        self.assertIn("error code: 1010", str(caught.exception))


class TestCallTimeout(unittest.TestCase):
    def test_a_stalled_request_times_out_promptly(self) -> None:
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
    def test_returns_quietly_on_success(self) -> None:
        self.client.await_update("u-ok", timeout=30)  # must not raise

    def test_raises_and_prints_the_failing_stage(self) -> None:
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with self.assertRaises(komodo.KomodoError):
                self.client.await_update("u-fail", timeout=30)
        printed = captured.getvalue()
        self.assertIn("Deploy", printed)
        self.assertIn("denied: permission on Stack", printed)

    def test_timeout_raises_rather_than_passing(self) -> None:
        with self.assertRaises(komodo.KomodoError) as caught:
            self.client.await_update("u-never", timeout=1)
        self.assertIn("timed out", str(caught.exception))

    def test_really_polls_rather_than_reading_status_once(self) -> None:
        # The stub answers InProgress on the first poll for every id.
        self.client.await_update("u-ok-again", timeout=30)


class TestExpand(unittest.TestCase):
    VARS = {"HOMELAB_LAN_IP": "172.20.3.194"}

    def test_expands_a_known_placeholder(self) -> None:
        self.assertEqual(
            komodo.expand("http://${HOMELAB_LAN_IP}:5000", self.VARS),
            "http://172.20.3.194:5000")

    def test_leaves_plain_text_alone(self) -> None:
        self.assertEqual(komodo.expand("no placeholders", self.VARS), "no placeholders")

    def test_leaves_a_bare_dollar_alone(self) -> None:
        # A compose file or a password may legitimately contain one.
        text = "cost is $5 and 100% real"
        self.assertEqual(komodo.expand(text, self.VARS), text)

    def test_an_unknown_placeholder_is_an_error(self) -> None:
        with self.assertRaises(komodo.KomodoError) as caught:
            komodo.expand("http://${NOPE}:1", self.VARS)
        self.assertIn("NOPE", str(caught.exception))

    def test_load_vars_reads_the_pairs_the_caller_passed(self) -> None:
        # The workflow states its own values; nothing on disk holds them.
        with unittest.mock.patch.dict(
                os.environ, {"KOMODO_VARS": "HOMELAB_LAN_IP=172.20.3.194\n"}):
            self.assertEqual(komodo.load_vars()["HOMELAB_LAN_IP"], "172.20.3.194")

    def test_load_vars_is_empty_when_the_caller_passed_none(self) -> None:
        # An action with no `vars` input is fine as long as nothing needs one;
        # a file that then hits ${NAME} fails in expand(), with the name in it.
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(komodo.load_vars(), {})


class TestStackExists(KomodoTestCase):
    def test_true_for_an_existing_stack(self) -> None:
        self.assertTrue(self.client.stack_exists("immich"))

    def test_false_on_the_500_not_found_body(self) -> None:
        # Komodo answers a missing stack with 500, not 404.
        self.assertFalse(self.client.stack_exists("missing-stack"))

    def test_other_failures_still_raise(self) -> None:
        broken = komodo.Komodo(url="http://127.0.0.1:1", api_key="k", api_secret="s")
        with self.assertRaises(komodo.KomodoError):
            broken.stack_exists("immich")


class TestPayloads(KomodoTestCase):
    VARS = {"HOMELAB_LAN_IP": "172.20.3.194"}

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.compose = Path(self.dir.name) / "compose.yaml"
        self.compose.write_text("services: {}\n")
        self.env_file = Path(self.dir.name) / ".env"
        self.env_file.write_text("A=1\n")
        self.addCleanup(self.dir.cleanup)

    def test_carries_every_supplied_field(self) -> None:
        config = self.client.stack_config(
            str(self.compose), str(self.env_file),
            "http://${HOMELAB_LAN_IP}:28888/login", self.VARS)
        self.assertEqual(config["file_contents"], "services: {}\n")
        self.assertEqual(config["environment"], "A=1\n")
        self.assertEqual(config["links"], ["http://172.20.3.194:28888/login"])

    def test_omits_fields_with_no_input(self) -> None:
        # An omitted input must never clear what is already on the stack.
        config = self.client.stack_config(str(self.compose), None, None, self.VARS)
        self.assertNotIn("environment", config)
        self.assertNotIn("links", config)

    def test_splits_multiple_links(self) -> None:
        config = self.client.stack_config(
            None, None, "http://${HOMELAB_LAN_IP}:1\nhttp://${HOMELAB_LAN_IP}:2",
            self.VARS)
        self.assertEqual(len(config["links"]), 2)

    def test_carries_the_registry_login_fields(self) -> None:
        config = self.client.stack_config(
            None, None, None, self.VARS,
            registry_provider="ghcr.io", registry_account="lorainemg")
        self.assertEqual(config["registry_provider"], "ghcr.io")
        self.assertEqual(config["registry_account"], "lorainemg")

    def test_refuses_half_a_registry_login(self) -> None:
        # Komodo looks the stored account up by provider *and* username, so
        # one without the other can never match.
        with self.assertRaises(komodo.KomodoError) as caught:
            self.client.stack_config(
                None, None, None, self.VARS, registry_provider="ghcr.io")
        self.assertIn("registry-account", str(caught.exception))

    def test_sync_config_clears_the_other_sources(self) -> None:
        # Komodo prefers a repo over stored contents, so leaving repo set would
        # silently ignore what we just pushed.
        toml = Path(self.dir.name) / "stacks.toml"
        toml.write_text('links = ["http://${HOMELAB_LAN_IP}:5000"]\n')
        config = self.client.sync_config(str(toml), self.VARS)
        self.assertIn("172.20.3.194", config["file_contents"])
        self.assertEqual(config["repo"], "")
        self.assertEqual(config["branch"], "")
        self.assertEqual(config["resource_path"], [])


if __name__ == "__main__":
    unittest.main()
