"""Print a fresh T3 pairing link using the client-facing address."""
import argparse
import re
import subprocess
import urllib.parse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='https://t3.sussman.win')
args = parser.parse_args()
address = urllib.parse.urlsplit(args.url)
if address.scheme not in ('http', 'https') or not address.netloc or address.path not in ('', '/') or address.query or address.fragment or address.username:
    parser.error('--url must be an HTTP(S) origin, without a path or credentials')
result = subprocess.run(
    ['docker', 'compose', '--project-directory', str(Path(__file__).resolve().parent),
     'exec', '-T', 't3-code', 't3', 'pair', '--label', 'Paired client'],
    capture_output=True, text=True, check=True,
)
match = re.search(r'^Pairing URL: (\S+)', result.stdout, re.MULTILINE)
if match is None:
    raise SystemExit('T3 did not print a pairing URL')
url = urllib.parse.urlsplit(match.group(1))
print(urllib.parse.urlunsplit(url._replace(scheme=address.scheme, netloc=address.netloc)))
