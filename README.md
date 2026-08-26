# SS Bot

SS Bot is a Telegram-first task and event management system. The user-facing product is the Telegram bot itself: registration, task management, events, checklists, reports, photos, leader approval, temporary working groups, archives, and administration are operated through bot messages and buttons.

The FastAPI service remains an internal/backend API and health surface. PostgreSQL and Object Storage are the source of truth. A dedicated MTProto service account automates temporary Telegram working groups for group tasks.

## Implemented bot workflows

- Telegram `/start` registration with mandatory full name, Telegram ID, and current `@username` synchronization.
- Role-aware bot menu for participants, sector heads, and administrators.
- Task lists with filters and detailed task cards.
- Individual and group task creation directly in the bot.
- Event selection during task creation.
- Full-name / `@username` assignee search with multi-select buttons.
- Separate group-leader selection.
- Task description, deadline, and checklist creation.
- Creator auto-membership.
- Checklist completion directly from task buttons.
- Manager task editing, member add/remove, leader replacement, checklist management, and cancellation.
- Report draft, comment, up to five JPEG/PNG/WebP photos, photo preview/delete, final submission, return for rework, resubmission, and leader approval.
- Telegram working-group status, invite retry, group recovery, and direct link to the working group.
- Event creation, participant selection, archive view, PDF export, photo ZIP export, and retention extension.
- Administrator user management for roles, activation state, and sector assignment.
- Bot-native sector creation, rename, description editing, activation/deactivation, and user-to-sector assignment.
- Automatic group creation, direct invite/fallback invite link, 30-minute reminders until join, membership reconciliation, cleanup warning, and group deletion.
- PostgreSQL transactional outbox, retry/backoff, audit logs, deadline reminders, overdue processing, retention, and archive purge.

The previous Telegram Mini App has been removed from the project. There is no Mini App runtime, source bundle, production route, Node build, or Mini App dependency in the acceptance path. New user-facing functionality belongs in the Telegram bot.

## Security

Never commit a bot token, Telethon session, database password, or S3 secret. Copy `.env.example` to `.env` and fill values locally. If a bot token or Telegram service session was ever exposed, revoke/replace it before production use.

## Run locally

1. Install Python 3.12+ and Docker.
2. Copy `.env.example` to `.env`.
3. Configure a fresh `TELEGRAM_BOT_TOKEN` and `BOOTSTRAP_ADMIN_TELEGRAM_IDS`.
4. For automatic working-group management, configure the dedicated MTProto account variables.
5. Start the stack:

```powershell
docker compose up --build
```

The API is available on `http://localhost:8000`; `/healthz` reports process health and `/readyz` verifies database access. The bot uses long polling locally unless `TELEGRAM_WEBHOOK_URL` is configured.

For a local Python-only iteration:

```powershell
python -m pip install -e .
$env:APP_ENV = 'development'
python -m apps.bot.app.runner
```

The development default can use SQLite; Docker uses PostgreSQL and runs `alembic upgrade head` before starting the API.

## Telegram onboarding

1. Create the bot with BotFather.
2. Put the new bot token in `TELEGRAM_BOT_TOKEN`.
3. Configure `BOOTSTRAP_ADMIN_TELEGRAM_IDS` with the initial administrator Telegram IDs.
4. In production set `TELEGRAM_WEBHOOK_URL` to the public HTTPS base URL and configure a long random `TELEGRAM_WEBHOOK_SECRET`.
5. Create a separate organization-owned Telegram user account for MTProto group operations.
6. Authorize its Telethon session in a protected environment and mount the resulting session where `TELEGRAM_SERVICE_SESSION_PATH` points.
7. Test group creation/invitation/deletion with non-production Telegram accounts before real use.

Read `docs/telegram-api-notes.md` before enabling live group automation.

## Tests and quality checks

```powershell
python -m pip install -e '.[dev]'
python -m ruff check .
python -m pytest
```

GitHub Actions validates the Python bot/backend, applies the complete Alembic migration chain on PostgreSQL 16, and validates the Docker Compose configuration.

## Production

Start production with:

```powershell
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
```

Set `DOMAIN` and `TELEGRAM_WEBHOOK_URL=https://your-domain`. Caddy terminates TLS and routes `/webhook` to the bot and `/api/*`, `/healthz`, and `/readyz` to the API. There is no public Mini App runtime.

Keep PostgreSQL, Redis, MinIO, workers, and the MTProto session private. Use a secret manager, managed backups where possible, and verify database/object-storage restore procedures before production rollout.

See `docs/operations-runbook.md` for staging acceptance, backup/restore, rollback, and incident-response procedures.
