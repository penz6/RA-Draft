# GroupMe deadline reminders

The owner registers **one GroupMe application**. HRAs use **Connect GroupMe**
on the Deadlines page to authorize that application, choose their building's
chat, and confirm adding **RA Deadlines**. The owner does not need to join those
chats. GroupMe still creates one bot registration per chat, using the authorizing
HRA's account. Owning the application does not make the owner the creator of
every HRA-authorized bot registration.

## Owner deployment setup

1. Register an application at [GroupMe Developers](https://dev.groupme.com/applications).
2. Set its callback URL to `https://<PUBLIC_HOST>/groupme/callback` (HTTPS required).
   This is the OAuth callback, separate from a bot's optional message callback.
3. Set `GROUPME_CLIENT_ID` to that application's client ID in the deployment
   environment. No GroupMe client secret or personal token is needed in `.env`.
4. Deploy the updated web and `deadline-reminders` worker images and Compose
   configuration together. They share the SQLite volume and stable `SECRET_KEY`.
5. Allow outbound HTTPS to `api.groupme.com:443` from web and worker. HRA browsers
   must reach `oauth.groupme.com` to sign in and authorize the application.

Without a configured client ID, the site explains that connection is not yet
available. Registering the real application is an owner action outside the PR.

## HRA connection

1. On **Deadlines**, choose **Connect GroupMe** and sign in to GroupMe to authorize
   the owner's application. The HRA must already belong to the intended chat.
2. Check the displayed GroupMe account, select the building's chat, and confirm
   **Add RA Deadlines to this chat**. No bot is created before this confirmation.
3. The app creates an active bot with a consistent name and enables scheduled
   reminders. Use **Send test message** to verify the destination. That explicit
   action sends a connection test to the group, at most once per minute.

HRAs can manage only their assigned building. Admins have a building picker and
can manage all connections. RAs and Prostaff have no connection controls.
Uncheck and save to pause reminders. **Disconnect GroupMe** stops app posts and
removes the saved Bot ID; removing the bot registration itself is managed by its
creator on GroupMe's developer page. Reconnecting creates a new registration;
remove an unused old registration there if changing chats.

Admins can alternatively use **Manual setup** to connect a bot they registered
on [GroupMe's bot page](https://dev.groupme.com/bots), including bots they own.
That manual method requires the registering account to belong to the chat.
Create an active bot named **RA Deadlines**, leave its message callback URL blank,
and enter the private Bot ID. Saving never sends a test message automatically.

## Schedule and messages

GroupMe posts at 9 a.m. America/New_York **seven calendar days before and seven
calendar days after** each deadline. Email sends seven days before and **on** the
due date. No day-of GroupMe posts. One short post per matching deadline:

```text
📅 Due in 1 week
Bulletin Boards
Due Oct 26, 2026 · All day
```

The later post starts with `📅 Was due 1 week ago`. Posts contain only the title
and due date/time from the shared JSON feed, with no descriptions or links.
They do not indicate personal completion status. No duty assignments, swaps,
or group conversations are sent. Past deadlines must remain in the feed at
least seven days for the later reminder; old dates omitted from the original
PDF import are not added back.

## Authorization and credentials

Implementation references:
[authentication tutorial](https://dev.groupme.com/tutorials/oauth),
[bot tutorial](https://dev.groupme.com/tutorials/bots), and
[API reference](https://dev.groupme.com/docs/v3#bots_post).

GroupMe documents implicit OAuth returning an access token in the HTTPS callback
query. A minimal first-party callback page removes query/fragment from browser
history and submits it in a CSRF-protected POST. Callback responses use
`no-store` and `no-referrer`. Keep query strings out of reverse-proxy/access logs
for `/groupme/callback`; the production Gunicorn configuration already disables
access logging. A proxy sees the initial provider callback before the browser
can remove the token. Do not enable callback URL analytics or request tracing
that records credentials.

A connection attempt is tied to the signed-in user, initiating browser session,
and building, expires after ten minutes, and can only be completed once. The
app sends OAuth `state` and rejects a mismatched returned value. The official
tutorial does not promise a state echo, so the documented callback without it
is supported; explicit account identity and group confirmation are required
before creating or connecting any bot. Never confirm an unfamiliar account.

Setup requests use `X-Access-Token` headers for `/users/me`, paginated `/groups`,
and `/bots`; account tokens are never placed in API URLs. Registration includes
`name`, `group_id`, and `active: true`; no message callback is registered, and
no group messages are read. The account token is encrypted in a temporary
server-side setup record, never stored in Flask's cookie, and deleted on
completion/cancellation/error or expiration cleanup (worker scans every five
minutes). Only the encrypted Bot ID is retained for ongoing posting. Rotating
`SECRET_KEY` requires reconnecting saved bots.

Bot posts use JSON `bot_id` and `text` at `POST /v3/bots/post`; only `201 Created`
is success. Requests have timeouts, do not follow redirects, and show generic
errors without logging credentials or provider response bodies. Plain Unicode
📅 needs no PowerUp attachment. Messages stay below the documented 1,000-character
group-message limit. GroupMe's developer/API terms apply to the application.

## Delivery and verification

The worker checks both channels independently every five minutes; SMTP settings
do not control GroupMe. Successful reminders have persistent SQLite receipts,
preventing ordinary duplicates across scans, restarts, reconnecting the same
Bot ID, and overlapping workers. A fifteen-minute claim lease recovers crashes.
Failed posts retry later on the same reminder date; a building's batch stops
after a provider failure to avoid bursts. There is no catch-up for missed days.
Feed errors pause posts. Disabled configuring accounts, revoked HRA/Admin roles,
and HRAs moved away from the building pause their connection. Roles and consent
are rechecked before posts and bot connection writes.

A timeout/crash after GroupMe accepts a post but before its receipt is saved can
cause a duplicate; the bot endpoint has no documented idempotency key. If a bot
creation request fails ambiguously, check the GroupMe bot page before retrying;
creation is not automatically retried.

Tests mock GroupMe authorization, groups, bot registration, and delivery. A real
registered client ID, GroupMe account, and permitted outbound network access are
required to test the full authorization redirect and actual posts after deploy.
