# Implementation plan

Updated: 2026-08-28

This document is superseded by `docs/final-bot-audit.md` and `docs/final-acceptance.md`. The product is Telegram-only; FastAPI remains internal infrastructure.

## Coverage audit

| Phase | Status | Evidence / next work |
| --- | --- | --- |
| 0 Foundation | Automated pass, exact-SHA CI required | Local lint, PostgreSQL 16, 76 tests, MinIO, runtime startup, both Compose overlays, and the Python 3.12 image build pass. The release handoff records final GitHub CI evidence. |
| 1 Registration | Automated pass | `/start`, profile capture, username refresh/removal, disabled users, and role menus have handler coverage; live Bot API remains. |
| 2 Roles/sectors | Automated pass | Directory/cards, role/status changes, sector assignment/removal, audit, stale callbacks, active-sector validation, and self-lockout have handler/domain coverage. |
| 3 Search | Implemented | Normalized person search and PostgreSQL trigram index migration are included. |
| 4 Events | Implemented | Event and participant lifecycle, sector guards, archive metadata, exports, and retention controls exist. |
| 5 Tasks | Automated pass | Creator membership/leadership, participant pickers, group wizard persistence, real card rendering, checklist metadata/management, deletion, reports, deadlines, and authorization guards are tested. |
| 6–10 Outbox/chat/invites/reminders | Implemented, automated adapters | PostgreSQL row claiming, retries, fallback links, configured reminders, reconciliation and inactive-user stopping are tested; live Telegram remains. |
| 11–14 Execution/notifications/cleanup | Implemented | Presigned reports, server-side image inspection and previews, durable notifications, overdue/deadline jobs, cleanup lifecycle, and archive verification before Telegram deletion exist. |
| 15–16 Archive/retention | Implemented | Event archive API, PDF/ZIP exports, one-year scheduling, 30-day alerts, extensions, and soft purge exist. |
| 17 Hardening | Local gates pass | Webhook mode, reverse proxy, redacted JSON logs, readiness, CI definition, runbook, modular bootstrap, secret-ignore checks, and callback reachability tests exist. GitHub CI and live staging remain external. |

## Current implementation order

1. Provision production PostgreSQL, Redis, S3-compatible storage, HTTPS domain, and an organization-owned MTProto account.
2. Rotate any credential ever exposed outside the secret manager, configure an isolated replacement bot, and run the webhook smoke test.
3. Run the real Telegram and retention staging matrix using only isolated credentials and the guarded staging cleanup override.
4. Configure infrastructure backup, metrics, alerting, and credential-rotation ownership.

## External blockers

No external credential is needed for implementation or mocked verification. An isolated BotFather token, HTTPS host, deployment S3 credentials, and an organization-owned authorized MTProto account are required for live integration testing and deployment. The final candidate also requires a successful GitHub Actions run for its exact SHA.
