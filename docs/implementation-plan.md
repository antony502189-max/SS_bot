# Implementation plan

Updated: 2026-08-26

The product is bot-only from the user's point of view. The FastAPI service remains the business/backend layer, but users do not need a Telegram Mini App. All required day-to-day workflows are exposed through bot messages, reply keyboards, and inline buttons.

## Coverage audit

| Area | Status | Evidence / remaining proof |
| --- | --- | --- |
| Foundation | Implemented | Docker, API, bot, worker, Alembic, PostgreSQL/Redis/S3 configuration, CI, Caddy, readiness checks, and structured logs exist. |
| Registration | Implemented | `/start`, full-name capture, Telegram ID, username synchronization/readiness, role-aware bot menu. |
| Roles/users | Implemented in bot | Administrator user list/card, role changes, activation/deactivation. Sector assignment/sector CRUD remain backend-supported and are the next administration enhancement if operational sector management is required entirely through the bot. |
| Search | Implemented | Full-name and `@username` search is used directly by bot task/event workflows. |
| Events | Implemented in bot | Event creation, participant selection, event cards, archive view, PDF/ZIP delivery, and retention extension. |
| Tasks | Implemented in bot | Task filters/cards, individual/group creation, event link, assignee selection, leader selection, deadline, description, checklist, editing, member changes, leader replacement, checklist management, and cancellation. |
| Reports/photos | Implemented in bot | Draft comment, up to five photos, preview/delete, submit, leader approval/return, and resubmission. |
| Working groups | Implemented | Durable task-chat creation, MTProto service account, bot promotion, direct invite, personal-link fallback, 30-minute reminders, join reconciliation, retry/recovery, member removal, and cleanup. Bot exposes group state and retry actions. |
| Notifications/deadlines | Implemented | Assignment/update/completion notifications, 24-hour deadline reminders, overdue transition, durable retries. |
| Archive/retention | Implemented | Event archive, PDF/ZIP, one-year retention, 30-day warning, extension, purge workflow. |
| Mini App | Removed | Source, Docker runtime, production route, Node build, and CI job have been removed. User-facing work belongs in the bot. |
| Production hardening | Implemented in code / live proof required | Webhook mode, TLS edge, health/readiness, retry logic, runbook, PostgreSQL migration validation, and CI exist. Real Telegram/S3 staging acceptance, backups, monitoring, and alert ownership remain deployment operations. |

## Current order to finish rollout

1. Keep CI green on the bot-only branch.
2. Provision staging PostgreSQL, Redis, S3-compatible storage, HTTPS domain, and an organization-owned MTProto account.
3. Rotate/configure the BotFather token and Telegram webhook secret.
4. Run the complete bot-only staging acceptance matrix from `docs/operations-runbook.md`.
5. Fix any live Telegram permission/rate-limit differences found in staging.
6. Configure production backups, metrics, alerting, and credential-rotation ownership.
7. Merge the verified remediation branch into `main` only after staging acceptance.

## External blockers

No external credential is required for code-level CI verification. Real integration proof requires a valid BotFather token, HTTPS host, object-storage deployment credentials, and an authorized organization-owned MTProto account. Any previously exposed bot token or MTProto session must be treated as compromised and replaced before use.
