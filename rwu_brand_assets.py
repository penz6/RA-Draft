"""Allow the official RWU brand image hosts used by the portal shell."""

from core import app, security_headers as _base_security_headers


def _security_headers_with_rwu_assets(response):
    response = _base_security_headers(response)
    csp = response.headers.get("Content-Security-Policy", "")
    old = "img-src 'self' data: https://*.googleusercontent.com https://lh3.googleusercontent.com;"
    new = (
        "img-src 'self' data: https://*.googleusercontent.com "
        "https://lh3.googleusercontent.com https://www.rwu.edu https://rwuhawks.com;"
    )
    if old in csp:
        response.headers["Content-Security-Policy"] = csp.replace(old, new)
    return response


# Keep the existing security policy intact and only extend its image allow-list.
handlers = app.after_request_funcs.get(None, [])
for index, handler in enumerate(handlers):
    if handler is _base_security_headers:
        handlers[index] = _security_headers_with_rwu_assets
        break
