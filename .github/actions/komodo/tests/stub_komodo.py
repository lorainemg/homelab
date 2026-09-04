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
