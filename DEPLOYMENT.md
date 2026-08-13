# Secure Cloudflare Tunnel deployment

Use Cloudflare -> Cloudflare Tunnel -> private Docker network -> Gunicorn. `docker-compose.cloudflare.yml` intentionally publishes no application port on the host.

## Environment

Create a local `.env` with `PUBLIC_HOST`, `SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ADMIN_EMAILS`, and `CLOUDFLARE_TUNNEL_TOKEN`. Never commit `.env`.

`PUBLIC_HOST` is the hostname only. `SECRET_KEY` must be a random value at least 32 characters long.

## Google OAuth

Set the authorized redirect URI to `https://<PUBLIC_HOST>/auth/callback`.

The app requires a verified `@g.rwu.edu` or `@rwu.edu` email plus Google's signed `hd` Workspace claim of `g.rwu.edu` or `rwu.edu`. Users are keyed by Google's stable `sub` claim, not by email alone. Test one student and one staff/faculty account before launch; if Google returns a different canonical RWU hosted-domain value, update `ALLOWED_HOSTED_DOMAINS` rather than weakening the check.

## Cloudflare Tunnel

Create a remotely managed Tunnel and put its connector token in the deployment environment. Add a published application route whose hostname equals `PUBLIC_HOST` and whose service is `http://ra-draft:8000`.

Start with `docker compose -f docker-compose.cloudflare.yml up -d --build`.

Do not add a host `ports:` mapping for RA Draft.

## Edge controls

Keep browser-facing HTTPS enabled. Do not create a Cache Everything rule. Add a rate limit for `/login`, initially around 20 requests per minute per source IP, using Managed Challenge or a temporary block. Avoid aggressive per-IP limits on normal draft POST routes because RWU users may share a NAT address.

## Operations

The application port does not need to be opened on the host firewall. Back up the `ra-draft-data` volume regularly. SQLite uses WAL mode, so use SQLite's backup mechanism or stop the app briefly before a raw filesystem copy.

Startup fails if `SECRET_KEY` or `PUBLIC_HOST` is unsafe/missing. Flask rejects untrusted Host headers, trusts one proxy hop, uses Secure/HttpOnly/SameSite cookies, limits sessions to 12 hours, emits security headers/no-store caching, records privileged mutations in `audit_log`, and serializes capacity-sensitive SQLite writes.
