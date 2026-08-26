# Production operations runbook

Use this runbook for a staging deployment first, then repeat the same checks for production. Do not place any secret in this repository, a ticket, or a chat message.

## Before deployment

1. Rotate any BotFather token that has been exposed. Store the replacement token, Telegram webhook secret, database password, S3 keys, and MTProto session in the deployment secret manager.
2. Provision PostgreSQL, Redis, and S3-compatible object storage with private network access. Enable encrypted backups for PostgreSQL and versioning or equivalent recovery protection for the object bucket.
3. Obtain a public DNS hostname, set `DOMAIN`, and set `TELEGRAM_WEBHOOK_URL` to its `https://` base URL. Caddy terminates TLS and forwards the configured webhook path to the bot.
4. Authorize a dedicated organization-owned Telegram account for MTProto operations in a protected environment. Mount its session file at `TELEGRAM_SERVICE_SESSION_PATH`; never use a personal account.

## Deploy

1. Populate a deployment-only `.env` from `.env.example` using secrets from the manager.
2. Build and start the stack:

   ```powershell
   docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build
   ```

3. Confirm the API is ready: `https://<DOMAIN>/readyz` must return HTTP 200 and an `X-Request-ID` header.
4. Review API, worker, bot, and Caddy logs for migrations, webhook setup, storage errors, or MTProto authorization failures.

## Staging acceptance test

1. Send `/start` to the replacement bot and complete the full-name and username flow.
2. Verify the role-aware reply keyboard appears and that all required operations are available directly in the bot. No Mini App is required.
3. As an administrator or sector head, create an event from bot buttons, including participants, dates, description, and budget.
4. Create an individual task from the bot. Verify assignee search by full name/`@username`, checklist creation, deadline, task card, checklist completion, report comment, photo attachment, and final completion.
5. Create a group task with a leader and at least two participants. Verify creator auto-membership, worker-created supergroup, pinned task brief, direct invitation/fallback link, and 30-minute reminders for a user who has not joined.
6. Verify the group-status bot screen shows membership state and allows a manager to retry an invitation or recover a degraded group.
7. Submit a group-task report from bot buttons with photos. Verify the leader can approve it or return it with a reason, and that the participant can edit and resubmit the report.
8. Test task editing, adding/removing a group member, changing the leader, checklist changes, and cancellation from bot buttons.
9. Open an event archive from the bot and verify PDF export and photo ZIP export are delivered as Telegram documents. As an administrator, verify retention extension from the bot.
10. On a disposable completed task, validate cleanup scheduling, 24-hour warning, invitation revocation, and Telegram-group deletion. Do not shorten production retention merely to test it.
11. Verify task/report/photo/archive records remain intact after temporary Telegram-group deletion.

## Backup, restore, and rollback

- Take automated PostgreSQL backups and protect object storage with versioning or backups. Retain both long enough to cover the event-record retention policy.
- At least quarterly, restore a database backup and a sample original plus preview image into an isolated environment; exercise an archive export from restored data.
- Before a release, take a database backup and record the current image/commit. The API runs `alembic upgrade head` at startup; review migrations before deploying.
- To roll back application code, deploy the prior image/commit. Do not roll back a database schema blindly: use a tested forward migration or restore into a maintenance window.

## Monitoring and incident response

- Forward JSON application logs to centralized logging. Alert on `/readyz` failure, repeated worker outbox failures, webhook delivery errors, task-chat degradation, and storage errors.
- If a bot token or MTProto session is exposed, immediately revoke/rotate it, remove the old deployment secret, restart affected services, and audit bot/group activity.
- If Telegram group automation degrades, leave task and archive records intact; retry through the durable outbox after resolving permissions or rate limits.
- If object storage is unavailable, do not complete destructive cleanup until report/photo persistence has been verified.
