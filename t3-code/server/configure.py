"""Prepare Codex configuration; load the token from a runtime-only file."""
import json
import os
from pathlib import Path

os.environ['CODEX_API_TOKEN'] = Path(os.environ['CODEX_API_TOKEN_FILE']).read_text().strip()
if not os.environ['CODEX_API_TOKEN']:
    raise SystemExit('Foundry token file is empty')
home = Path(os.environ['CODEX_HOME'])
home.mkdir(parents=True, exist_ok=True, mode=0o700)
config = f'''model = {json.dumps(os.environ['CODEX_MODEL'])}
model_provider = "foundry"

[features]
api_key_model_discovery = true

[model_providers.foundry]
name = "Foundry"
base_url = {json.dumps(os.environ['CODEX_BASE_URL'].rstrip('/'))}
env_key = "CODEX_API_TOKEN"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
model_catalog_url = "http://catalog:8080/models"
'''
path = home / 'config.toml'
path.write_text(config)
path.chmod(0o600)
command = os.sys.argv[1:] or ['t3', 'serve', '--host', '0.0.0.0', '--port', '3773', '/workspace']
os.execvp(command[0], command)
