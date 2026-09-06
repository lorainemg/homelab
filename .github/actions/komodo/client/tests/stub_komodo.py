#!/usr/bin/env python3
"""A stand-in for Komodo Core 2.3.1, replaying the response shapes the real
server produces, so the client can be tested without touching the homelab.

Which scenario you get is chosen by the request's `type`, so a test picks a
behaviour by calling the matching Komodo request type. The stub also checks
that each type arrives on the route the real server serves it from: a read
sent to /execute is a test failure here rather than a silent pass in CI.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

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


class _StubServer(HTTPServer):
    """HTTPServer plus the two things the handler records across requests.

    Declared here rather than bolted onto a plain HTTPServer after the fact,
    so the handler's `self.server.polls` is a field the checker knows about.
    """

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _Handler)
        self.polls: dict[str, int] = {}
        self.received: list[dict[str, Any]] = []  # every write payload, for assertions


class _Handler(BaseHTTPRequestHandler):
    server: _StubServer

    def log_message(self, *args: Any) -> None:
        pass  # keep test output clean

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or "{}")
        rtype = req.get("type", "")
        params = req.get("params", {})
        polls = self.server.polls

        # Komodo sits behind Cloudflare, which rejects urllib's default agent
        # with a plain-text 403 before the request ever arrives. Replayed here
        # so the client cannot lose its User-Agent without a test noticing.
        if self.headers.get("User-Agent", "").startswith("Python-urllib"):
            body = b"error code: 1010"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

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

    _server: _StubServer
    _thread: threading.Thread
    url: str

    def __enter__(self) -> StubKomodo:
        self._server = _StubServer(("127.0.0.1", 0))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        return self

    @property
    def received(self) -> list[dict[str, Any]]:
        return self._server.received

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


class StubServerTestCase(unittest.TestCase):
    """Base for any test that needs a live stub: one server per class."""

    _stub: ClassVar[StubKomodo]

    @classmethod
    def setUpClass(cls) -> None:
        cls._stub = StubKomodo().__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stub.__exit__(None, None, None)

    def env(self, **extra: str) -> dict[str, str]:
        """The environment cli.main() reads, pointed at this class's stub.

        Keyword arguments are merged in, so a test names only the settings it
        actually cares about (a zero poll interval, a KOMODO_VARS line).
        """
        return {
            "KOMODO_URL": self._stub.url,
            "KOMODO_API_KEY": "test-key",
            "KOMODO_API_SECRET": "test-secret",
            **extra,
        }
