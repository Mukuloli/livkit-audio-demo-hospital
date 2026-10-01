"""Import a downloaded Google OAuth web-client JSON into backend/.env."""
import argparse
import json
from pathlib import Path


EXPECTED_REDIRECT = 'http://127.0.0.1:8000/host/google/callback'
BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BACKEND_DIR / '.env'


def replace_env_value(content, key, value):
    lines = content.splitlines()
    replacement = f'{key}={value}'
    for index, line in enumerate(lines):
        if line.startswith(key + '='):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description='Import a Google OAuth web application client JSON.')
    parser.add_argument('client_json', type=Path)
    args = parser.parse_args()
    payload = json.loads(args.client_json.read_text(encoding='utf-8'))
    client = payload.get('web')
    if not client or not client.get('client_id') or not client.get('client_secret'):
        raise SystemExit('The selected file is not a Google OAuth Web application client JSON.')
    redirects = client.get('redirect_uris', [])
    if EXPECTED_REDIRECT not in redirects:
        raise SystemExit(f'Add this Authorized redirect URI in Google Cloud first: {EXPECTED_REDIRECT}')
    content = ENV_PATH.read_text(encoding='utf-8-sig') if ENV_PATH.exists() else ''
    content = replace_env_value(content, 'GOOGLE_OAUTH_CLIENT_ID', client['client_id'])
    content = replace_env_value(content, 'GOOGLE_OAUTH_CLIENT_SECRET', client['client_secret'])
    content = replace_env_value(content, 'GOOGLE_OAUTH_REDIRECT_URI', EXPECTED_REDIRECT)
    ENV_PATH.write_text(content, encoding='utf-8')
    print('Google OAuth web client imported into backend/.env. The client secret was not printed.')


if __name__ == '__main__':
    main()
