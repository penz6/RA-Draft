# Pangolin passcode + Google SSO deployment

The intended production path is:

```text
Browser -> Pangolin PIN/passcode gate -> RA Duty Picking -> Google OpenID Connect
```

Pangolin is the outer access gate. Its PIN/password mode does not identify an individual user, so RA Duty Picking still signs each person in with Google and stores Google's stable `sub`, verified email, and display name. Application roles and building access remain enforced by the application.

## Container image

Successful pushes to `main` publish the tested production image to:

```text
ghcr.io/penz6/ra-draft:latest
```

Each main build is also published with its immutable Git commit SHA as a tag. The Docker image contains an OCI source label linking it to this repository.

## Environment

Create a local `.env` file and never commit it:

```env
PUBLIC_HOST=duty.example.edu
SECRET_KEY=<random value at least 32 characters long>
GOOGLE_CLIENT_ID=<Google OAuth web client ID>
GOOGLE_CLIENT_SECRET=<Google OAuth client secret>
ADMIN_EMAILS=you@rwu.edu
PROXY_HOPS=1
PANGOLIN_NETWORK=pangolin
WEB_THREADS=64
AUDIT_LOG_MAX_ROWS=5000
```

`PUBLIC_HOST` is the hostname only, without a scheme or path. `PROXY_HOPS` must match the number of trusted forwarded-host/proto values between Pangolin and Flask. Keep it at `1` unless the deployment has been intentionally tested with another value.

`WEB_THREADS` controls how many concurrent ordinary requests and visible live pages Gunicorn can serve. Each visible dashboard or session uses one lightweight Server-Sent Events connection. Hidden tabs close their connection and reconnect when visible again. The image defaults to 64 threads.

`AUDIT_LOG_MAX_ROWS` bounds the persistent Admin audit trail. The default is 5,000 rows. Older audit rows are deleted automatically and SQLite reuses the freed pages for later records. This limit does not remove duty sessions or assignment history.

The event broker is held in the application process, so the production container intentionally runs exactly one Gunicorn worker. Do not add workers or app replicas unless the broker is replaced with shared pub/sub such as Redis.

Generate a secret with, for example:

```bash
openssl rand -hex 32
```

## Google OpenID Connect

Create a Google OAuth 2.0 **Web application** and configure this exact authorized redirect URI:

```text
https://<PUBLIC_HOST>/auth/callback
```

RA Duty Picking requests only `openid email profile`. It requires:

- Google's validated OpenID response
- a stable Google `sub`
- `email_verified` equal to boolean `true`
- an email ending in `@g.rwu.edu` or `@rwu.edu`
- a matching Google Workspace `hd` claim

The app never stores Google access or refresh tokens. Admins may pre-create an account by verified RWU email; the record is linked to the matching Google identity on that person's first sign-in.

## Pangolin resource

1. Create or identify the Docker network used by the Pangolin connector:

   ```bash
   docker network create pangolin
   ```

   Skip creation if the network already exists, and set `PANGOLIN_NETWORK` to its real name.

2. Ensure the Pangolin connector and `ra-draft` service share that network.
3. Create a Pangolin public resource for `https://<PUBLIC_HOST>`.
4. Point the resource target to:

   ```text
   http://ra-draft:8000
   ```

5. Enable Pangolin PIN/passcode authentication on the resource.
6. Confirm Pangolin preserves the public Host and HTTPS forwarding information. The app rejects unexpected Host values and uses forwarded scheme/host data to generate Google's callback URL.

The compose file publishes no host port. Do not add a public `0.0.0.0:8000` binding. If Pangolin runs outside Docker, bind port 8000 only to a private interface or loopback and target that private address from Pangolin.

Start the application with:

```bash
docker compose -f docker-compose.pangolin.yml pull
docker compose -f docker-compose.pangolin.yml up -d
```

## Live updates

Dashboards and active sessions use a same-origin Server-Sent Events connection at `/live-events`. This is ordinary streaming HTTPS, so no WebSocket upgrade, separate port, or additional container is required.

Changes are published only after SQLite executes a real commit. Notifications are routed to session pages, dashboards, or the signing-out user's pages; each connected client recalculates its complete authorized view and receives an `update` event only when that view actually changed.

The server sends a heartbeat every 15 seconds and closes each stream after five minutes. The browser reconnects automatically, which revalidates the current login cookie and authorization. Signing out in another tab sends an immediate reload signal to the user's other live pages.

Pangolin/Traefik must pass streaming responses without buffering them for completion. The application preserves these response properties:

```text
Content-Type: text/event-stream
Cache-Control: private, no-cache, no-store, no-transform
X-Accel-Buffering: no
Content-Encoding: identity
```

Browsers without EventSource use the scoped JSON state endpoint as a slower compatibility fallback. If a stream repeatedly errors, the browser also performs fallback checks while EventSource reconnects.

A quick authenticated browser check is to open Developer Tools, select **Network**, and confirm that `live-events` remains pending with content type `text/event-stream`. When another participant picks, the server emits an `update` event and the page refreshes immediately. If the user has unsaved form changes or an open confirmation dialog, the app displays an update notice and preserves the edits until the user reloads or returns the form to its original state.

## First sign-in

The first new user whose email appears in `ADMIN_EMAILS` becomes an admin. Everyone else starts as an RA unless an Admin pre-created their account with another role. Sign in with the admin account, create buildings, then assign or pre-create users and their building access.

## Security and operations

- Rotate the Pangolin passcode if it is shared beyond the intended group.
- Keep the application port private; only Pangolin should reach it.
- Keep HTTPS enabled for the public hostname.
- Do not cache authenticated HTML, `.ics`, or `text/event-stream` responses at the proxy.
- Back up the `ra-draft-data` volume off the VPS.
- SQLite uses WAL mode. Use SQLite's backup API or stop the app briefly before copying the database and WAL files.
- Review the Admin audit table for role, building, session, assignment, and access changes.
- Pull newly published images regularly so dependency security updates are incorporated.

## Storage limits

The production container intentionally does not write a Gunicorn access log. Gunicorn error output remains available through Docker logs.

The sample Compose file also caps the Docker `json-file` log at three 5 MB files (about 15 MB maximum for this container). The persistent audit table is bounded separately by `AUDIT_LOG_MAX_ROWS` and defaults to 5,000 rows.

Docker images can consume much more disk than application logs after many deployments. After confirming a new deployment is healthy, remove dangling images that are no longer referenced by a container:

```bash
docker image prune -f
```

Check overall Docker disk use at any time with:

```bash
docker system df
```

Do not use `docker system prune --volumes` for routine cleanup because the named database volume is persistent application data.

## Update

```bash
git pull
docker compose -f docker-compose.pangolin.yml pull
docker compose -f docker-compose.pangolin.yml up -d --force-recreate
docker image prune -f
```
