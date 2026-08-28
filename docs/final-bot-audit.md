# Final bot audit

Audited 2026-08-28. Exact candidate SHA and CI evidence are recorded in the
release handoff, not hard-coded here.

## Automated evidence

- The complete user-facing surface is Telegram. No Mini App runtime, WebApp
  button, frontend source, or frontend build exclusion remains.
- Registration handlers cover idempotent `/start`, username synchronization,
  full-name validation, disabled accounts, and role-aware menus.
- Administration handlers cover the directory, pagination, cards, roles,
  activation, sector assignment/removal, active-sector validation, audit rows,
  forged/stale callbacks, unauthorized actors, and self-lockout prevention.
- Task and event participant pickers cover search, directory pagination,
  persistent selection, stale/inactive users, sector scoping, and finish paths.
- The group task wizard persists creator-led tasks end to end. Domain scenarios
  additionally cover participant-led groups, creator-as-leader, individual
  tasks, report rework, permanent accidental deletion, and retention.
- Task-card and checklist tests cover rendering, complete/uncomplete metadata,
  manager add/delete, membership authorization, and stale callback handling.
- Archive handler tests deliver valid PDF/ZIP documents, enforce event access,
  scope photos to the selected event, sanitize ZIP filenames, support an empty
  archive, reject purged archives, and fail safely on storage errors.
- A callback contract scans normal inline actions for reachability, the 64-byte
  Telegram limit, handler conflicts/shadowing, canonical UUIDs, and obsolete
  frontend references.
- The full integration run passes 76 tests with warnings treated as errors,
  PostgreSQL 16 enabled, and a real isolated MinIO instance enabled.
- PostgreSQL reaches one Alembic head and verifies BIGINT Telegram IDs,
  report lifecycle enum labels, task-member uniqueness, `pg_trgm`, scoped
  search, and `FOR UPDATE SKIP LOCKED`.
- API readiness, Redis, Celery worker/beat, bot fake-transport initialization,
  Python 3.12 image imports, Docker build, and both Compose overlays pass.
- API, bot, and Celery application logs use the shared JSON formatter. Tokens,
  invite links, and credential-like values are redacted; worker retry/failure
  records include safe operation, event, attempt, and error-type fields.

## External gates

- GitHub Actions must be green for the exact final candidate SHA. The release
  handoff must record the SHA, workflow run ID, and conclusion.
- Full group provisioning, bot addition, direct invitation, privacy fallback,
  Bot API delivery, join reconciliation, report-photo transport, and actual
  group cleanup require the isolated live Telegram staging procedure.

## Non-blocking technical debt

- `apps/bot/app/handlers/core.py` remains a compatibility aggregate. Further
  decomposition is deferred because imports, handler reachability, and runtime
  startup are verified and no correctness blocker requires another refactor.

See `docs/final-acceptance.md` for the release matrix. This audit does not make
a release claim while the revision-specific CI and live Telegram gates remain.
