# Final bot audit

Audited: 2026-08-26. Statuses reflect reachable Telegram flows, not merely API routes.

| Function | Implementation status | Test status | Known issue | Action required |
| --- | --- | --- | --- | --- |
| Bot startup and router registration | IMPLEMENTED_AND_TESTED | Bot contract/import tests pass | Live Bot API credentials not exercised | Run staging procedure |
| Telegram-only deployment surface | IMPLEMENTED_AND_TESTED | Compose config and source search pass | Local ignored `.env` may retain obsolete variables | Remove obsolete local variables when rotating secrets |
| Registration and username synchronization | IMPLEMENTED_AND_TESTED | Direct aiogram-adapter `/start` and full-name registration test | Live Bot API credentials not exercised | Run staging procedure |
| Role-aware home menu | IMPLEMENTED_AND_TESTED | `test_bot_contract.py` | None | Maintain menu contract test |
| User search in task wizard | IMPLEMENTED_NOT_TESTED | SQLite-only unit coverage | No PostgreSQL trigram test | Add PostgreSQL integration test |
| Administration through Telegram | IMPLEMENTED_NOT_TESTED | No Telegram update simulation | User cards, roles, activation, sector assignment, and recent audit-log navigation exist | Add handler integration tests |
| Sector administration through Telegram | IMPLEMENTED_NOT_TESTED | No Telegram update simulation | List/create/rename/description/status and user-count controls exist | Add handler integration tests |
| Events through Telegram | IMPLEMENTED_NOT_TESTED | Event validation and archive-union tests | Create/list/open/edit, participant add/remove, sector assignment, archive view, PDF/ZIP, and retention extension exist | Add handler integration tests |
| Task creation wizard | IMPLEMENTED_NOT_TESTED | Contract and domain tests pass | Event, description, checklist, separate leader, and participant selection are implemented | Add staged-wizard tests |
| Task card and task lists | IMPLEMENTED_AND_TESTED | Card-control test plus domain tests | Details, checklist, report, cancellation, chat status, members, leader, and checklist add/edit/remove exist | Add aiogram callback tests |
| Report draft/return/resubmit lifecycle | IMPLEMENTED_AND_TESTED | `test_reports.py` covers group rework and individual close | Bot FSM is implemented; live media/API check remains | Run staging procedure |
| Report photos | IMPLEMENTED_AND_TESTED | Image inspection and gated MinIO round-trip test | Telegram photo/document upload, preview, and draft deletion are implemented | Add mocked media-handler test; run staging procedure |
| MTProto group creation and ID interoperability | IMPLEMENTED_AND_TESTED | Bot API/Telethon ID round-trip regression test | Live group creation remains unverified | Run staging verification |
| Invitations, reminders, reconciliation | IMPLEMENTED_AND_TESTED | Membership-before-reminder regression test | Reminder checks membership; manager chat-status/retry/recovery controls exist | Run live privacy-fallback/reconciliation procedure |
| Transactional outbox | IMPLEMENTED_AND_TESTED | FloodWait scheduler regression test | Typed FloodWait and generic exponential retries are scheduled | Add an operator outbox view if operational volume demands it |
| Deadline processing | IMPLEMENTED_AND_TESTED | Worker overdue/notification regression test | Reminder and overdue processing use row locking | Run staging procedure |
| Group cleanup | IMPLEMENTED_AND_TESTED | Closed/open cleanup regressions pass | Telegram deletion requires live verification | Run staging cleanup procedure |
| Archive, PDF and photo ZIP through Telegram | IMPLEMENTED_NOT_TESTED | Archive-union and PDF-byte tests | Telegram archive summary, PDF, and ZIP delivery are implemented | Add bot export tests and run staging procedure |
| Retention and purge | IMPLEMENTED_AND_TESTED | Retention-date, warning, successful purge, and storage-failure regressions | Live storage retention remains to be exercised in staging | Run staging procedure |
| Audit logging | IMPLEMENTED_NOT_TESTED | Indirect domain coverage | Mutations are audited and admins can view recent entries | Add audit-navigation test |
| PostgreSQL migration from empty database | IMPLEMENTED_AND_TESTED | SQLite and PostgreSQL 16 upgrades passed | `pg_trgm` and report lifecycle columns verified | Keep PostgreSQL CI gate |
| Object-storage integration | IMPLEMENTED_AND_TESTED | Gated MinIO upload/retrieve/delete integration test | Local MinIO round trip passed; CI runs it | Run live storage staging check |

No Telegram Mini App or other user-facing web UI remains. The remaining release
gate is staged Telegram verification and a small set of handler/retention tests,
not a missing business flow.
