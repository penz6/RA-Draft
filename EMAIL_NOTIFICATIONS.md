# RA Draft email notifications

RA Draft can send branded email notifications through an authenticated Gmail account using SMTP with STARTTLS.

## Gmail setup

1. Use the Gmail account that should send RA Draft notifications.
2. Enable 2-Step Verification on that Google account.
3. Create a Google App Password for RA Draft.
4. Put the Gmail address and App Password in the deployment `.env` file. Do not commit the App Password to GitHub.

Example:

```env
MAIL_ENABLED=1
MAIL_HOST=smtp.gmail.com
MAIL_PORT=587
MAIL_USERNAME=ra.draft.notifications@gmail.com
MAIL_APP_PASSWORD=<google app password>
```

The visible sender name is always **Duty Shifts**. Gmail still sends from the address configured in `MAIL_USERNAME`.

Set `MAIL_ENABLED=0` to disable delivery. When email delivery is enabled, `MAIL_USERNAME` and `MAIL_APP_PASSWORD` are required and the application will fail at startup if either is missing.

## Notifications

RA Draft sends email for these committed state changes:

- A duty session changes from `OPEN` to `CLOSED`: every active participant receives their finalized duty dates, an authenticated link to download their personal iCal calendar, and a **Duty Swaps** button that opens that session's swap page.
- A new duty swap is requested: the target RA receives the proposed date pair(s) and a link to review the request.
- The target RA approves a swap: active HRA accounts for that building receive the request for final approval. If the building has no active HRA, active Admin accounts are used as a fallback.
- The HRA/Admin gives final approval: both RAs and the approving HRA/Admin receive confirmation. The two RAs also receive a link to download their updated personal iCal calendar.

SMTP delivery happens only after the underlying database change has committed. SMTP connection or authentication failures are logged and do not roll back an already-completed session or swap action.

## Opt-in deadline reminders

Staff can turn on the checkbox on the Deadlines page or use the announcement
banner’s **Opt in to reminders** button. Consent defaults to off and is stored on
the signed-in account. Both controls update the same preference; unchecking the
checkbox opts out. The banner’s × button permanently dismisses it for that account
across devices without subscribing. Opting in also dismisses the announcement.

Reminders go to the account’s existing email address at 9 a.m. America/New_York
three calendar days before and on the due date. Multiple matching deadlines are
grouped into one email per user per scan. Dates use the school timezone across
EST/EDT changes. There are no retroactive reminders for prior days or timed
deadlines that have already passed. Opting in after 9 a.m. can receive that day’s
reminders on the next scan. The email includes a link back to the preference so
staff can opt out.

The `deadline-reminders` Compose service runs `python -m deadline_reminders`,
checks every five minutes, and uses the same image, SQLite volume, feed URL, and
SMTP settings as the web app. It waits for the web app to be healthy before
starting. Apply the updated Compose file when deploying this feature:

```bash
docker compose -f docker-compose.pangolin.yml pull
docker compose -f docker-compose.pangolin.yml up -d
```

For a deployment without Compose, run `python -m deadline_reminders` as a
supervised background process with the same environment and database path as the
web app. `MAIL_ENABLED=1` and the existing SMTP credentials are required for
sending; when mail is disabled the worker sends nothing. It does not rely on
visiting the site or keeping a browser tab open.

Delivery receipts persist in SQLite, so normal restarts and overlapping workers
do not resend successful reminders. Failed SMTP sends can retry on subsequent
scans. A claim lease of 15 minutes recovers interrupted work. As with SMTP in
general, a process crash after the server accepts mail but before its receipt is
saved can cause a retry; exactly-once delivery cannot be guaranteed. Unavailable
or invalid feeds pause delivery rather than send stale deadlines. Disabled
accounts and users who opt out are skipped, with consent checked again before
SMTP delivery. The worker never sends an opt-in confirmation email.
