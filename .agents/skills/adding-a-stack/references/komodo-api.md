# Komodo API — the calls used when adding a stack

Base URL is `https://komodo.sussman.win` (also in the `KOMODO_URL` Actions
secret). `POST /read`, `/write` and `/execute` all take
`{"type": ..., "params": {...}}` with `Authorization: Bearer $JWT`.

`/execute` is asynchronous: it returns an Update with `status: "InProgress"`.
Confirm results by polling `/read`, never by the HTTP status.

## Get a JWT

Credentials live in `komodo/.env` on the server, not in git.

```bash
JWT=$(curl -s -X POST https://komodo.sussman.win/auth/login \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"type":"LoginLocalUser","params":{"username":"%s","password":"%s"}}' \
        "$(grep '^KOMODO_INIT_ADMIN_USERNAME=' komodo/.env | cut -d= -f2-)" \
        "$(grep '^KOMODO_INIT_ADMIN_PASSWORD=' komodo/.env | cut -d= -f2-)")" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["jwt"])')
```

The endpoint is `/auth/login`, not `/auth`.

## Create a git-mode Stack (methods 1 and 2)

`project_name` must equal the repo directory name, or the stack adopts fresh
empty volumes instead of the existing ones.

```bash
curl -s -X POST https://komodo.sussman.win/write \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' -d '{
  "type": "CreateStack",
  "params": {
    "name": "<stack>",
    "config": {
      "server_id": "Local",
      "project_name": "<stack>",
      "git_provider": "github.com",
      "repo": "lorainemg/homelab",
      "branch": "main",
      "file_paths": ["<stack>/docker-compose.yml"],
      "webhook_enabled": true,
      "auto_pull": true,
      "auto_update": false,
      "poll_for_updates": false
    }
  }
}'
```

Add these two only when the stack bind-mounts config out of the checkout:

```json
"webhook_force_deploy": true,
"post_deploy": {"path": "", "command": "docker restart <svc> <svc>", "shell_mode": false}
```

## Create a `file_contents` Stack (method 3)

Starts empty; the owning repo's CI fills it on every deploy.

```json
{"type": "CreateStack", "params": {"name": "<stack>", "config": {
  "server_id": "Local",
  "project_name": "<stack>",
  "file_contents": "services: {}"
}}}
```

Give that CI a **service user** with Write on this one Stack. Its CI pushes
compose and env together in one `UpdateStack`, then polls `GetUpdate`.

## Trigger a deploy without an API key

Komodo's GitHub listener accepts a hand-signed payload — the same HMAC-SHA256
scheme GitHub uses. This is what `deploy.yml` does, so CI holds nothing that can
do more than redeploy one stack.

```bash
body='{"ref":"refs/heads/main"}'
sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$KOMODO_WEBHOOK_SECRET" | cut -d' ' -f2)
curl -sf -X POST "$KOMODO_URL/listener/github/stack/<stack>/deploy" \
  -H 'Content-Type: application/json' \
  -H "X-Hub-Signature-256: sha256=$sig" \
  -H 'X-GitHub-Event: push' \
  -d "$body"
```

The `ref` is required. The listener filters on it against the Stack's branch;
a bare `{}` is ignored while still answering `200`.

## Check what is deployed

```bash
curl -s -X POST https://komodo.sussman.win/read -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"type":"ListStacks","params":{}}' \
  | python3 -c 'import sys,json;[print(s["name"], s["info"]["deployed_hash"], s["info"]["latest_hash"]) for s in json.load(sys.stdin)]'
```

`deployed_hash` is where the Stack is; `latest_hash` is the branch tip. They may
differ legitimately when `webhook_force_deploy` is false.

## Roll back

In the UI, set the Stack's **commit** field to the target hash, Save, **then**
Deploy — two separate actions. Do not put a hash in **branch**: it is
interpolated into `git clone -b <branch>`, which accepts only branch names and
tags. `commit` is interpolated into `git reset --hard`, which is what works.

**Clear the commit field afterwards.** A pin left there is invisible: the Stack
still reports `branch: main`, webhooks still fire, deploys still report success,
and the stack stays frozen forever.
