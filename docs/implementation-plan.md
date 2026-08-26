# Implementation plan

Updated: 2026-08-25

This document is superseded by `docs/final-bot-audit.md` and `docs/final-acceptance.md`. The product is Telegram-only; FastAPI remains internal infrastructure.

## Coverage audit

| Phase | Status | Evidence / next work |
| --- | --- | --- |
| 0 Foundation | In progress | Docker, API, bot, worker, Alembic, CI, production Caddy edge configuration, readiness checks, and structured request logs exist. |
| 1 Registration | In progress | `/start`, profile capture, username refresh, and audit records exist. |
| 2 Roles/sectors | In progress | Role/sector guards and administration services exist; Telegram administration flow requires completion. |
| 3 Search | Implemented | Normalized person search and PostgreSQL trigram index migration are included. |
| 4 Events | Implemented | Event and participant lifecycle, sector guards, archive metadata, exports, and retention controls exist. |
| 5 Tasks | Implemented | Task creation/change/cancellation, member invariants, checklist, reports, and strict lifecycle transitions exist. |
| 6–10 Outbox/chat/invites/reminders | Implemented | Durable outbox, targeted re-invites, bot admin promotion, pinned task briefs, member removal, reminders, and ten-minute membership reconciliation exist. |
| 11–14 Execution/notifications/cleanup | Implemented | Presigned reports, server-side image inspection and previews, durable notifications, overdue/deadline jobs, cleanup lifecycle, and archive verification before Telegram deletion exist. |
| 15–16 Archive/retention | Implemented | Event archive API, PDF/ZIP exports, one-year scheduling, 30-day alerts, extensions, and soft purge exist. |
| 17 Hardening | Implemented in code | Webhook mode, reverse proxy, JSON request logs, health/readiness endpoints, CI, and deployment runbook are included. Metrics, backups, credential rotation, and live-alert integrations remain deployment operations. |

## Current implementation order

1. Provision production PostgreSQL, Redis, S3-compatible storage, HTTPS domain, and an organization-owned MTProto account.
2. Rotate the exposed BotFather token, configure the replacement token, and run the webhook smoke test.
3. Run the real Telegram, storage, and retention staging test matrix before launch.
4. Configure infrastructure backup, metrics, alerting, and credential-rotation ownership.

## External blockers

No external credential is needed for implementation or mocked verification. A new BotFather token, HTTPS host, S3 deployment credentials, and an organization-owned authorized MTProto account are required for live integration testing and deployment. The originally shared bot token must be treated as compromised and rotated before use.
