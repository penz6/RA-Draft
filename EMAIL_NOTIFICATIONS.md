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

The visible sender name is always **RA Draft**. Gmail still sends from the address configured in `MAIL_USERNAME`.

Set `MAIL_ENABLED=0` to disable delivery. When email delivery is enabled, `MAIL_USERNAME` and `MAIL_APP_PASSWORD` are required and the application will fail at startup if either is missing.

## Notifications

RA Draft sends email for these committed state changes:

- A duty session changes from `OPEN` to `CLOSED`: every active participant receives their finalized duty dates and an authenticated link to download their personal iCal calendar.
- A new duty swap is requested: the target RA receives the proposed date pair(s) and a link to review the request.
- The target RA approves a swap: active HRA accounts for that building receive the request for final approval. If the building has no active HRA, active Admin accounts are used as a fallback.
- The HRA/Admin gives final approval: both RAs and the approving HRA/Admin receive confirmation. The two RAs also receive a link to download their updated personal iCal calendar.

SMTP delivery happens only after the underlying database change has committed. SMTP connection or authentication failures are logged and do not roll back an already-completed session or swap action.
