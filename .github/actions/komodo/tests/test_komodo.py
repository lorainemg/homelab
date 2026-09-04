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
