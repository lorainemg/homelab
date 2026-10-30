"""Serve a Codex-native Foundry catalog, fetching only on demand."""
import copy
import json
import os
import secrets
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def translate(deployments, bundled):
    by_model = {model['slug']: model for model in bundled['models']}
    models, omitted = [], []
    for deployment in deployments:
        if deployment.get('type') != 'ModelDeployment' or deployment.get('capabilities', {}).get('chat_completion') not in (True, 'true'):
            continue
        template = by_model.get(deployment.get('modelName'))
        if template is None:
            omitted.append(deployment['name'])
            continue
        model = copy.deepcopy(template)
        model['slug'] = deployment['name']
        model['display_name'] = 'foundry-' + deployment['name']
        models.append(model)
    return {'models': models}, omitted


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch_inventory(project, token):
    endpoint = project.rstrip('/') + '/deployments?api-version=v1'
    trusted = urllib.parse.urlsplit(project)
    deployments = []
    opener = urllib.request.build_opener(NoRedirect())
    seen = set()
    while endpoint:
        target = urllib.parse.urlsplit(endpoint)
        if (target.scheme, target.netloc) != (trusted.scheme, trusted.netloc) or not target.path.startswith(trusted.path.rstrip('/') + '/'):
            raise ValueError('Refusing pagination outside the project endpoint')
        if endpoint in seen:
            raise ValueError('Repeated pagination endpoint')
        seen.add(endpoint)
        request = urllib.request.Request(endpoint, headers={'api-key': token})
        with opener.open(request, timeout=20) as response:
            page = json.load(response)
        deployments.extend(page['value'])
        endpoint = page.get('nextLink')
    return deployments


def main():
    project = os.environ['FOUNDRY_PROJECT_URL']
    token = Path(os.environ['CODEX_API_TOKEN_FILE']).read_text().strip()
    bundled = json.loads(Path('/opt/catalog/bundled-models.json').read_text())

    def fetch():
        catalog, omitted = translate(fetch_inventory(project, token), bundled)
        print(f'Catalog fetched: {len(catalog["models"])} supported; omitted without metadata: {omitted}', flush=True)
        return catalog

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle_catalog(self):
            if not secrets.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                self.send_error(401)
                return
            try:
                catalog = fetch()
            except Exception as error:
                print(f'Catalog fetch failed ({type(error).__name__})', flush=True)
                self.send_error(502, 'Deployment inventory unavailable')
                return
            body = json.dumps(catalog).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urllib.parse.urlsplit(self.path).path == '/models':
                self.handle_catalog()
            else:
                self.send_error(404)

    # No startup fetch, timer, polling thread, or inventory health probe.
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()


if __name__ == '__main__':
    main()
