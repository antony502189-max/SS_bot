# Production operations runbook

Use this runbook for a staging deployment first, then repeat the same checks for production. Do not place any secret in this repository, a ticket, or a chat message.

## Before deployment

1. Rotate any BotFather token that has been exposed. Store the replacement token, the Telegram webhook secret, database password, S3 keys, and MTProto session in the deployment secret manager.
2. Provision PostgreSQL, Redis, and S3-compatible object storage with private network access. Enable encrypted backups for PostgreSQL and versioning or equivalent recovery protection for the object bucket.
3. Obtain a public DNS hostname, set `DOMAIN`, and set `TELEGRAM_WEBHOOK_URL` to its `https://` base URL. The Caddy edge terminates TLS and forwards the configured webhook path to the bot.
4. Authorize a dedicated organization-owned Telegram account for MTProto operations in a protected environment. Mount its session file at `TELEGRAM_SERVICE_SESSION_PATH`; never use a personal account.

## Deploy

1. Populate a deployment-only `.env` from `.env.example` using secrets from the manager.
2. Build and start the edge stack:

   ```powershell
   docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build
   ```

3. Confirm the API is ready: `https://<DOMAIN>/readyz` must return HTTP 200 and an `X-Request-ID` header.
4. Review the API, worker, bot, and Caddy logs for migrations, webhook setup, or MTProto authorization failures.

## Staging acceptance test

1. Send `/start` to the replacement bot and complete the full-name and username flow. Verify the correct role-aware reply keyboard appears.
2. Exercise the bot-only task workflow: open task filters, create an individual task, create a group task, edit task details, change checklist items, add/remove a member, and replace the group leader.
3. Create creator-led and participant-led group tasks with two participants. Verify the worker creates each chat once, posts and pins the task brief, adds the task creator, handles direct invitation or invite-link fallback, sends reminders at `INVITE_REMINDER_MINUTES` only while a user has not joined, and handles one member leaving and rejoining.
4. Build a report entirely through bot buttons. Add JPEG, PNG, and WebP photos, preview and remove a photo, submit the report, return it for rework, resubmit it, and approve it as the leader.
5. Create an event through the bot, verify participants and budget, open its archive, request the PDF and photo ZIP in Telegram, and test retention extension with an administrator account.
6. Test administrator controls from the bot by changing a test user role and toggling account activation.
7. On a disposable closed task, validate invitation retry, cleanup warning, invitation revocation, and chat deletion. Verify an active or merely overdue task is never deleted before closure and the calculated cleanup time.
8. To shorten only this staging cleanup test, set `APP_ENV=staging` and `STAGING_TASK_CLEANUP_MINUTES` from 5 to 60. The override is ignored outside staging; remove it immediately after acceptance.

## Backup, restore, and rollback

- Take automated PostgreSQL backups and protect object storage with versioning or backups. Retain both long enough to cover the event record retention policy.
- At least quarterly, restore a database backup and a sample original plus preview image into an isolated environment; exercise an archive export from the restored data.
- Before a release, take a database backup and record the current image/commit. The API runs `alembic upgrade head` at startup; review migrations before deploying.
- To roll back application code, deploy the prior image/commit. Do not roll back a database schema blindly: use a tested forward migration or a restore into a maintenance window.

## Monitoring and incident response

- Forward JSON application logs to centralized logging. Alert on `/readyz` failure, repeated worker outbox failures, webhook delivery errors, task-chat degradation, and storage errors. Verify redaction remains active for bot tokens, invite links, and credential-like values.
- Use `request_id` from an API response or log event to trace a request across application logs.
- If a bot token or MTProto session is exposed, immediately revoke/rotate it, remove the old deployment secret, restart affected services, and audit bot/group activity.
- If Telegram group automation degrades, leave the task and archive records intact; retry through the durable outbox after resolving permissions or rate limits.
