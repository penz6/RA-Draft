# Security model

RA Draft assumes the public application is behind a trusted reverse proxy such as Pangolin and that the Gunicorn port is not directly exposed to the Internet.

## Identity and authorization

- Pangolin PIN/password protection is an outer access gate, not an individual identity source.
- Google OpenID Connect provides the user's stable `sub`, verified email, hosted-domain claim, and display name.
- Only verified `@g.rwu.edu` and `@rwu.edu` Google Workspace identities are accepted.
- Database accounts are keyed by Google `sub`; an email collision with a different `sub` fails closed.
- Every HRA/Admin mutation performs server-side role and building authorization.
- The last admin cannot be demoted.

## Injection defenses

- SQL statements use bound parameters. User input is never interpolated into SQL.
- Jinja HTML autoescaping remains enabled; templates do not use the `safe` filter.
- Admin-created building and session names are length-limited and reject Unicode control characters.
- Shift times and dates are parsed into strict formats.
- iCalendar values normalize and escape CR, LF, backslash, comma, and semicolon characters to prevent calendar-property injection.
- All state-changing forms require a constant-time CSRF token check.

## Browser and proxy controls

- Secure, HttpOnly, SameSite=Lax session cookies
- 12-hour non-refreshing application sessions
- Trusted Host enforcement
- Explicit trusted proxy-hop count
- Content Security Policy, anti-framing, no-sniff, referrer, permissions, HSTS, no-index, and no-store headers
- Request size, form memory, and form-part limits

## Data and operations

- SQLite foreign keys, WAL mode, busy timeout, and serialized capacity-sensitive writes
- Audit records for authentication and privileged changes
- Non-root container user with dropped Linux capabilities in Compose
- Persistent database volume that must be backed up off-host

Please avoid including real OAuth credentials, session cookies, Pangolin passcodes, or production database contents in bug reports.
