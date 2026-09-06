#!/usr/bin/env python3
"""Tests for cli.py against a stub Komodo: dispatch, env handling, exit codes."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]  # stub_komodo, then cli.py

import cli                                  # noqa: E402
from stub_komodo import StubServerTestCase  # noqa: E402

LAN_IP = "172.20.3.194"
VARS = f"HOMELAB_LAN_IP={LAN_IP}"


class TestDeployStackCommand(StubServerTestCase):
    def test_returns_zero_on_success(self):
        with unittest.mock.patch.dict(os.environ, self.env(KOMODO_POLL_INTERVAL="0")):
            self.assertEqual(cli.main(["deploy-stack", "--stack", "immich"]), 0)

    def test_returns_one_when_komodo_refuses(self):
        captured = io.StringIO()
        with unittest.mock.patch.dict(os.environ, self.env(KOMODO_POLL_INTERVAL="0")):
            with contextlib.redirect_stderr(captured):
                code = cli.main(["deploy-stack", "--stack", "fail-me"])
        self.assertEqual(code, 1)
        self.assertIn("denied: permission on Stack", captured.getvalue())

    def test_missing_credentials_is_a_clear_error(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                code = cli.main(["deploy-stack", "--stack", "immich"])
        self.assertEqual(code, 1)
        self.assertIn("KOMODO_URL", captured.getvalue())


class TestUpdateStackCommand(StubServerTestCase):
    def test_creates_the_stack_when_missing_then_updates_it(self):
        before = len(self._stub.received)
        with unittest.mock.patch.dict(os.environ, self.env(KOMODO_VARS=VARS)):
            code = cli.main([
                "update-stack", "--stack", "missing-stack", "--create-if-missing"])
        self.assertEqual(code, 0)
        sent = [r["type"] for r in self._stub.received[before:]]
        self.assertEqual(sent, ["CreateStack", "UpdateStack"])

    def test_does_not_create_when_the_stack_is_there(self):
        before = len(self._stub.received)
        with unittest.mock.patch.dict(os.environ, self.env(KOMODO_VARS=VARS)):
            code = cli.main([
                "update-stack", "--stack", "immich", "--create-if-missing"])
        self.assertEqual(code, 0)
        sent = [r["type"] for r in self._stub.received[before:]]
        self.assertEqual(sent, ["UpdateStack"])

    def test_links_reach_komodo_expanded(self):
        before = len(self._stub.received)
        with unittest.mock.patch.dict(os.environ, self.env(KOMODO_VARS=VARS)):
            cli.main([
                "update-stack", "--stack", "immich",
                "--links", "http://${HOMELAB_LAN_IP}:2283"])
        config = self._stub.received[before]["params"]["config"]
        self.assertEqual(config["links"], [f"http://{LAN_IP}:2283"])


class TestRunSyncCommand(StubServerTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.toml = Path(self.dir.name) / "stacks.toml"
        self.toml.write_text('links = ["http://${HOMELAB_LAN_IP}:5000"]\n')
        self.addCleanup(self.dir.cleanup)

    def test_pushes_rendered_contents_and_clears_the_repo_source(self):
        before = len(self._stub.received)
        env = self.env(KOMODO_POLL_INTERVAL="0", KOMODO_VARS=VARS)
        with unittest.mock.patch.dict(os.environ, env):
            code = cli.main([
                "run-sync", "--sync", "homelab", "--contents-file", str(self.toml)])
        self.assertEqual(code, 0)
        pushed = self._stub.received[before]
        self.assertEqual(pushed["type"], "UpdateResourceSync")
        config = pushed["params"]["config"]
        self.assertIn(LAN_IP, config["file_contents"])
        self.assertEqual(config["repo"], "")

    def test_render_prints_the_expanded_file_and_contacts_nothing(self):
        captured = io.StringIO()
        with unittest.mock.patch.dict(
                os.environ, {"KOMODO_VARS": VARS}, clear=True):
            with contextlib.redirect_stdout(captured):
                code = cli.main(["render", str(self.toml)])
        self.assertEqual(code, 0)
        self.assertIn(LAN_IP, captured.getvalue())


if __name__ == "__main__":
    unittest.main()
