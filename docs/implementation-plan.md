# Implementation plan

Updated: 2026-08-25

This is a greenfield implementation built from the target architecture. The first two commits establish the project; they do **not** complete the architecture.

## Coverage audit

| Phase | Status | Evidence / next work |
| --- | --- | --- |
| 0 Foundation | Partial | Docker, API, bot, worker, Mini App, Alembic, CI exist. Add production reverse proxy/observability later. |
| 1 Registration | Partial | `/start`, profile capture, and init-data HMAC exist. Add username readiness, session authorization, audit, and full `/me` flows now. |
| 2 Roles/sectors | In progress | Replace caller-supplied actor IDs with authenticated identity. Complete role, sector, activation/deactivation, and audit endpoints. |
| 3 Search | Partial | Normalization/search exists but lacks result sector shape, PostgreSQL trigram GIN index, and duplicate-name coverage. |
| 4 Events | Partial | Creation/listing exists. Add retrieval, edits, participant lifecycle, sector guards, archive metadata. |
| 5 Tasks | Partial | Creation, membership invariant, checklist, basic report transitions exist. Add task changes, cancellation, notifications, strict state machine. |
| 6–10 Outbox/chat/invites/reminders | In progress | Durable outbox, state API, targeted re-invites, admin promotion, reminders, and ten-minute membership reconciliation exist. Add pinned task messages, chat-member removal, and adapter mocks. |
| 11–14 Execution/notifications/cleanup | In progress | Presign/report/approval, report notifications, overdue/deadline jobs, cleanup lifecycle, server-side image inspection, and preview generation exist. Add storage integration tests and cleanup persistence safeguards. |
| 15–16 Archive/retention | In progress | Event archive API, PDF/ZIP downloads, one-year scheduling, 30-day alerts, extensions, and soft purge exist. Add generated-export persistence and storage integration tests. |
| 17 Hardening | Not started | Webhooks, reverse proxy, structured logging, metrics, backups/runbooks, production CI/CD. |

## Current implementation order

1. Secure Phase 1–3 flows and finish role/sector/search administration.
2. Complete events/tasks, notifications, and state transitions.
3. Make MTProto/chat automation durable and testable without real credentials.
4. Add archive/export/retention workers.
5. Complete the Mini App workflows and production hardening.

## External blockers

No external credential is needed for implementation or mocked verification. A new BotFather token, HTTPS host, S3 deployment credentials, and an organization-owned authorized MTProto account are required only for live integration testing and deployment.
