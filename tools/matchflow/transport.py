"""Single host-only HTTPS transport; no secret/error-body logging or mutation retries."""
import json
import urllib.error
import urllib.parse
import urllib.request

from config import key_file

SITE = 'https://f3decomp.com'
USER_AGENT = 'f3-matchflow/3 (OpenCode; local contributor tooling)'


class ServiceError(ValueError):
    def __init__(self, code, retry_after='unspecified', kind=None, response_body=None):
        self.code = code
        self.retry_after = retry_after
        # Private host evidence only. Never include this in exception text/tool output.
        self.response_body = response_body
        self.kind = kind or ({401: 'authentication', 403: 'access_denied', 429: 'rate_limit',
                              503: 'capacity'}.get(code, 'http_error'))
        super().__init__('service HTTP %s (%s); Retry-After=%s; no automatic retry' %
                         (code, self.kind, retry_after))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward contributor credentials to a redirect target.
        return None


def fetch(url, body=None, limit=64 * 1024 * 1024, opener=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'f3decomp.com' or parsed.fragment:
        raise ValueError('only the official HTTPS service origin is allowed')
    key = key_file().read_text(encoding='utf-8').strip()
    if not key:
        raise ValueError('credential file is empty')
    headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json', 'User-Agent': USER_AGENT}
    data = None if body is None else json.dumps(body).encode('utf-8')
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method='GET' if data is None else 'POST')
    try:
        with (opener or urllib.request.build_opener(NoRedirect())).open(req, timeout=90) as response:
            result = response.read(limit + 1)
            if len(result) > limit:
                raise ValueError('service response exceeds configured size limit')
            return result
    except urllib.error.HTTPError as exc:
        # Inspect only for a classification, never emit bodies (they can contain source).
        try:
            sample = exc.read(8193)
        finally:
            exc.close()
        edge = exc.code == 403 and (b'cloudflare' in sample.lower() or b'error 1010' in sample.lower())
        raise ServiceError(exc.code, exc.headers.get('Retry-After', 'unspecified'),
                           'edge_rejection' if edge else None,
                           response_body=sample if len(sample) <= 8192 else None) from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError('service transport failed; outcome ambiguous, reconcile without retry') from None


def request(tool, body):
    if not tool.replace('_', '').isalnum():
        raise ValueError('invalid service operation')
    try:
        result = json.loads(fetch(SITE + '/api/v1/' + tool, body))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('service returned invalid JSON; reconcile mutations without retry') from None
    if not isinstance(result, dict):
        raise ValueError('service response must be an object')
    return result
