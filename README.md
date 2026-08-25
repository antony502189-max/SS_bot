# SS Bot

SS Bot is a Telegram-first task and event system: a bot registers people, a mobile Mini App manages work, PostgreSQL/Object Storage retain the business record, and a dedicated MTProto service account automates temporary working groups.

## Included in this first implementation

- Telegram `/start` registration with mandatory full name and username refresh.
- Async FastAPI API with users, sectors, events, tasks, checklist state, reports, audit logs, and a PostgreSQL transactional outbox.
- Creator auto-membership, group-leader requirement, idempotent task creation, sector-scoped authorization, and name/username search.
- MTProto adapter with categorized Telegram errors, direct invitations, invite-link fallback, 30-minute reminder scheduling, join handling, cleanup warning/revocation/deletion workflow.
- Mobile-first Telegram Mini App shell for authenticated users, task queue, and administrative task creation.
- Docker Compose services for PostgreSQL, Redis, MinIO, API, worker, bot, and the Mini App.

## Security first

Never put a bot token, Telethon session, database password, or S3 secret in Git. Copy `.env.example` to `.env` and fill values locally. If a bot token was ever posted in a chat, screenshot, terminal, or commit, revoke it in BotFather and generate a replacement before using the bot.

## Run locally

1. Install Python 3.12+ and Node 22+.
2. Copy the example: `Copy-Item .env.example .env`.
3. Set a fresh `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, and `BOOTSTRAP_ADMIN_TELEGRAM_IDS` in `.env`. For the group automation, also configure the dedicated MTProto account variables.
4. Start the stack: `docker compose up --build`.
5. The API is at `http://localhost:8000`; `/healthz` reports process health and `/readyz` verifies database access. The packaged Mini App is at `http://localhost:8080`.

For a local API-only iteration, install the project and run:

```powershell
python -m pip install -e .
$env:APP_ENV = 'development'
uvicorn apps.api.app.main:app --reload
```

The development default uses SQLite; Docker uses PostgreSQL and runs `alembic upgrade head` before starting the API.

## Telegram onboarding

1. Create the bot with BotFather and configure the Mini App URL with HTTPS in production.
2. In production set `TELEGRAM_WEBHOOK_URL` to the public HTTPS base URL and keep a long random `TELEGRAM_WEBHOOK_SECRET`; the bot serves the configured `TELEGRAM_WEBHOOK_PATH` (default `/webhook`). Local development uses polling.
3. Create a separate Telegram user account for MTProto group operations. Authorize its Telethon session interactively in a protected environment, then mount the resulting session file where `TELEGRAM_SERVICE_SESSION_PATH` points.
4. Add the bot to a non-production test group first and verify its limited admin permissions and `chat_member` updates.

Read [the Telegram integration notes](docs/telegram-api-notes.md) before enabling live automation.

## Tests and quality checks

```powershell
python -m pytest
python -m ruff check .
Set-Location apps/miniapp; npm install; npm run build
```

## Production notes

Use the production edge configuration with `docker compose -f docker-compose.yml -f docker-compose.production.yml up -d`. Set `DOMAIN` and configure `TELEGRAM_WEBHOOK_URL=https://your-domain`; Caddy provisions TLS and routes `/webhook` to the bot, `/api/*` plus health checks to the API, and all other paths to the Mini App. Keep database, Redis, MinIO, and worker ports private.

Move all credentials to a secret manager, use managed PostgreSQL/S3 backups, run migrations as a release step, and configure object-storage lifecycle policies only after verifying the one-year retention process. The first release should run the full Telegram workflow in a dedicated test environment before it is used for real work.

JSON logs include `request_id`, method, path, status, and duration. Forward container logs to your observability platform, alert on `/readyz` failures, and test database restore plus object-storage restore on a regular schedule.

See the [production operations runbook](docs/operations-runbook.md) for deployment, staging acceptance, backup/restore, rollback, and incident-response procedures.
