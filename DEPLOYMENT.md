# Cloudflare Access + Tunnel deployment

Use Cloudflare Access -> Cloudflare Tunnel -> private Docker network -> Gunicorn. `docker-compose.cloudflare.yml` intentionally publishes no application port on the host.

## Environment

Create a local `.env` with:

```env
PUBLIC_HOST=duty.example.edu
SECRET_KEY=<long-random-secret-at-least-32-characters>
ADMIN_EMAILS=you@rwu.edu
CF_ACCESS_TEAM_DOMAIN=https://your-team.cloudflareaccess.com
CF_ACCESS_AUD=<Access Application Audience AUD tag>
CLOUDFLARE_TUNNEL_TOKEN=<tunnel connector token>
```

Never commit `.env`. `PUBLIC_HOST` is the hostname only. `CF_ACCESS_TEAM_DOMAIN` must be the Cloudflare Access team URL, not the application hostname.

The RA Draft container no longer needs `GOOGLE_CLIENT_ID` or `GOOGLE_CLIENT_SECRET`. Google authentication is configured in Cloudflare Access instead.

## Cloudflare Access

1. In Cloudflare Zero Trust, add Google as an identity provider.
2. Create a self-hosted Access application for `https://<PUBLIC_HOST>`.
3. Add an Allow policy limited to `@g.rwu.edu` and `@rwu.edu` identities.
4. Copy the application's Audience (AUD) tag into `CF_ACCESS_AUD`.
5. Put your Zero Trust team domain (for example `https://your-team.cloudflareaccess.com`) into `CF_ACCESS_TEAM_DOMAIN`.

Cloudflare sends a signed JWT to the origin in `Cf-Access-Jwt-Assertion`. RA Draft validates that JWT using Cloudflare's rotating JWKS, requires RS256, validates issuer/AUD/expiry/not-before, requires an identity-based `type=app` token, and independently restricts the email domain to RWU.

The app never trusts `Cf-Access-Authenticated-User-Email` by itself.

## Cloudflare Tunnel

Create a remotely managed Tunnel and put its connector token in the deployment environment. Add a published application route whose hostname equals `PUBLIC_HOST` and whose service is `http://ra-draft:8000`.

Start with:

```bash
docker compose -f docker-compose.cloudflare.yml up -d --build
```

Do not add a host `ports:` mapping for RA Draft.

## Identity migration

The schema migrates the old `google_sub` column to `access_sub` and marks existing direct-Google identities as legacy. The bootstrap admin named in `ADMIN_EMAILS` can be rebound automatically on the first verified Cloudflare Access login. Other existing users with a changed identity are held as pending claims and must be explicitly approved by an admin in the Admin page. This prevents a recycled email address from silently inheriting HRA/Admin permissions.

If the existing database contains only development/test data, starting with a fresh database is simpler.

## Logout

Use the Sign out link in RA Draft. It points to `/cdn-cgi/access/logout`, which Cloudflare handles to end the Access session.

## Edge controls

Keep browser-facing HTTPS enabled. Do not create a Cache Everything rule for this hostname. Access itself blocks unauthenticated requests before they reach the Tunnel. If you add rate limiting, keep it modest and avoid aggressive per-IP limits on normal draft POST routes because RWU users may share a NAT address.

## Operations

The application port does not need to be opened on the host firewall. Back up the `ra-draft-data` volume regularly. SQLite uses WAL mode, so use SQLite's backup mechanism or stop the app briefly before a raw filesystem copy.

Startup fails if `SECRET_KEY`, `PUBLIC_HOST`, `CF_ACCESS_TEAM_DOMAIN`, or `CF_ACCESS_AUD` is unsafe or missing. Flask rejects untrusted Host headers, trusts one proxy hop, uses Secure/HttpOnly/SameSite cookies, emits restrictive response headers/no-store caching, records privileged mutations in `audit_log`, and serializes capacity-sensitive SQLite writes.
