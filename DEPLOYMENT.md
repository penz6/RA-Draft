# Pangolin passcode + Google SSO deployment

The intended production path is:

```text
Browser -> Pangolin PIN/passcode gate -> RA Draft -> Google OpenID Connect
```

Pangolin is the outer access gate. Its PIN/password mode does not identify an individual user, so RA Draft still signs each person in with Google and stores Google's stable `sub`, verified email, and display name. Application roles and building access remain enforced by RA Draft.

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
```

`PUBLIC_HOST` is the hostname only, without a scheme or path. `PROXY_HOPS` must match the number of trusted forwarded-host/proto values between Pangolin and Flask. Keep it at `1` unless the deployment has been intentionally tested with another value.

Generate a secret with, for example:

```bash
openssl rand -hex 32
```

## Google OpenID Connect

Create a Google OAuth 2.0 **Web application** and configure this exact authorized redirect URI:

```text
https://<PUBLIC_HOST>/auth/callback
```

RA Draft requests only `openid email profile`. It requires:

- Google's validated OpenID response
- a stable Google `sub`
- `email_verified` equal to boolean `true`
- an email ending in `@g.rwu.edu` or `@rwu.edu`
- a matching Google Workspace `hd` claim

The app never uses email as the primary identity key and does not store Google access or refresh tokens.

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
6. Confirm Pangolin preserves the public Host and HTTPS forwarding information. RA Draft rejects unexpected Host values and uses forwarded scheme/host data to generate Google's callback URL.

The compose file publishes no host port. Do not add a public `0.0.0.0:8000` binding. If Pangolin runs outside Docker, bind port 8000 only to a private interface or loopback and target that private address from Pangolin.

Start the application with:

```bash
docker compose -f docker-compose.pangolin.yml pull
docker compose -f docker-compose.pangolin.yml up -d
```

## First sign-in

The first new user whose email appears in `ADMIN_EMAILS` becomes an admin. Everyone else starts as an RA. Sign in with the admin account, create buildings, then assign roles and buildings to users after their first Google login.

## Security and operations

- Rotate the Pangolin passcode if it is shared beyond the intended group.
- Keep the application port private; only Pangolin should reach it.
- Keep HTTPS enabled for the public hostname.
- Do not cache authenticated HTML or `.ics` responses at the proxy.
- Back up the `ra-draft-data` volume off the VPS.
- SQLite uses WAL mode. Use SQLite's backup API or stop the app briefly before copying the database and WAL files.
- Review the Admin audit table for role, building, session, deferral, and assignment changes.
- Pull newly published images regularly so Dependabot security updates are incorporated.

## Update

```bash
git pull
docker compose -f docker-compose.pangolin.yml pull
docker compose -f docker-compose.pangolin.yml up -d
```
