#!/usr/bin/env python3
"""Migrate one Portainer stack to a git-backed stack.

Usage: migrate-stack.py <old_stack_id> <name> <compose_path> [env_file] [KEY=VALUE ...]
Reads the API token from ~/.portainer_token. Never prints secret values.
"""
import json
import pathlib
import ssl
import sys
import time
import urllib.request

BASE = "https://localhost:9443/api"
ENDPOINT_ID = 3
REPO_URL = "https://github.com/lorainemg/homelab"

token = pathlib.Path("~/.portainer_token").expanduser().read_text().strip()
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def api(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=600) as r:
            data = r.read()
            return r.status, json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


old_id, name, compose_path = sys.argv[1], sys.argv[2], sys.argv[3]
env = []
for arg in sys.argv[4:]:
    if "=" in arg and not arg.endswith((".env",)):
        k, v = arg.split("=", 1)
        env.append({"name": k, "value": v})
    else:
        for line in pathlib.Path(arg).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.append({"name": k, "value": v})

print(f"env vars: {[e['name'] for e in env]}")

status, resp = api("DELETE", f"/stacks/{old_id}?endpointId={ENDPOINT_ID}")
print(f"delete stack {old_id}: HTTP {status}")
if status not in (200, 204, 404):
    print(json.dumps(resp)[:500])
    sys.exit(1)
time.sleep(2)

body = {
    "name": name,
    "repositoryURL": REPO_URL,
    "repositoryReferenceName": "refs/heads/main",
    "composeFile": compose_path,
    "repositoryAuthentication": False,
    "tlsskipVerify": False,
    "env": env,
    "autoUpdate": {"interval": "5m", "forceUpdate": False, "forcePullImage": False},
}
status, resp = api("POST", f"/stacks/create/standalone/repository?endpointId={ENDPOINT_ID}", body)
if status != 200:
    print(f"create failed: HTTP {status}")
    print(json.dumps(resp)[:800])
    sys.exit(1)
print(f"created stack '{resp['Name']}' id={resp['Id']} git-backed at {resp['GitConfig']['URL']} ({resp['GitConfig']['ConfigFilePath']})")
