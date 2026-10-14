"""Read-only deployment probe; never logs credentials or private response bodies."""
import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, build_opener

from inventory_hub.settings import settings


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # A readiness probe must never forward its bearer token elsewhere.
        return None


_open = build_opener(NoRedirect).open


def check(base_url: str, timeout: int = 12) -> bool:
    token = settings.AI_CONTENT_ACCESS_TOKEN.get_secret_value()
    if len(token) < 24:
        print('Security readiness: operator access is not configured')
        return False
    deadline = time.monotonic() + timeout
    probes = (
        ('/api/health', False, 200),
        ('/api/stock/summary', False, 401),
        ('/api/shops/biketrek/config', False, 401),
        ('/data/shops/biketrek/config.json', False, 401),
        ('/api/ai-content/status', True, 200),
        ('/data/shops/biketrek/config.json', True, 404),
    )
    for path, authenticated, expected in probes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print('Security readiness: probe timed out')
            return False
        headers = {'Accept': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + token
        request = Request(base_url.rstrip('/') + path, headers=headers)
        healthy = True
        try:
            with _open(request, timeout=min(3, remaining)) as response:
                status = response.status
                if path == '/api/health':
                    result = json.loads(response.read(4096))
                    healthy = result.get('status') == 'ok' and (
                        not result.get('use_postgres') or result.get('database', {}).get('status') == 'healthy')
        except HTTPError as error:
            status = error.code
            error.close()
        except (URLError, TimeoutError, OSError, ValueError):
            print('Security readiness: request failed')
            return False
        print(f'Security readiness {path}: HTTP {status}, expected {expected}')
        if status != expected or not healthy:
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='https://hub.biketrek.sk')
    parser.add_argument('--timeout', type=int, default=12)
    args = parser.parse_args()
    raise SystemExit(0 if check(args.base_url, max(1, min(args.timeout, 12))) else 1)


if __name__ == '__main__':
    main()
