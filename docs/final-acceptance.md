# Final acceptance matrix

Audited 2026-08-28. `AUTOMATED_PASS` means the hardened candidate passed the
stated executable checks. `LIVE_TELEGRAM_REQUIRED` means isolated Telegram
staging remains mandatory. Revision-specific GitHub Actions evidence belongs in
the release handoff so this matrix never mistakes an older run for the final
candidate.

| Area | Evidence in the current tree | Status |
| --- | --- | --- |
| registration and username synchronization | Handler tests cover new/repeated `/start`, valid/invalid name capture, username change/removal, disabled users, and role menus | AUTOMATED_PASS |
| participant database and user cards | Handler tests cover open, pagination, card rendering, authorization, role and status mutations | AUTOMATED_PASS |
| sector assignment | Picker, assignment/removal contract, inactive/forged/missing/removed sector paths, audit serialization, and refreshed card are tested | AUTOMATED_PASS |
| participant and event pickers | Search, pagination, persistent selection, stale/inactive/sector guards, finish paths, and callback size are tested | AUTOMATED_PASS |
| task creation and membership invariants | Individual/group domain paths plus full creator-led group FSM; different leader, creator-as-leader, unique creator membership, TaskChat and outbox are tested | AUTOMATED_PASS |
| task cards and checklist | Real card rendering, completion/uncompletion metadata, manager add/delete, member/non-member authorization, and stale callbacks are tested | AUTOMATED_PASS |
| reports and rework | Individual completion and group return/edit/resubmit/approve paths, including wrong-member/leader rejection, are tested | AUTOMATED_PASS |
| photos | Telegram media validation, image inspection, preview/rollback behavior, and a real MinIO round trip are tested | AUTOMATED_PASS |
| archive PDF delivery | Correct event, valid non-empty PDF, Telegram document call, authorization, and purged archive rejection are tested | AUTOMATED_PASS |
| archive ZIP delivery | Correct-event photo scope, safe unique filenames, valid/empty ZIPs, Telegram document call, and storage failure are tested | AUTOMATED_PASS |
| callback reachability | Normal callback payloads are source-scanned for handler reachability, 64-byte limit, canonical UUIDs, obsolete UI references, duplicates, and shadowing | AUTOMATED_PASS |
| accidental permanent deletion | Admin authorization, notification/outbox/member cleanup, event retention refresh, audit, report guard, and real-chat guard are tested | AUTOMATED_PASS |
| PostgreSQL 16 | Empty upgrade reaches sole head `0012_fix_postgres_enum_labels`; schema contracts, `pg_trgm`, sector-scoped search, and `SKIP LOCKED` are tested | AUTOMATED_PASS |
| Redis, Celery worker, and beat | Redis ping, worker ready state, seven task imports, and exact seven-job cadence contract are verified | AUTOMATED_PASS |
| runtime startup | API `/healthz` and PostgreSQL-backed `/readyz`, bot fake-transport startup, router registration, and Python 3.12 image imports are verified | AUTOMATED_PASS |
| Docker and Compose | `ss-bot-release-candidate` builds; development and production overlays pass `config --quiet` | AUTOMATED_PASS |
| warnings | Full suite passes under `-W error`; Starlette TestClient uses declared `httpx2` dev dependency | AUTOMATED_PASS |
| secrets and logging | No tracked token/private-key/session literal; ignored credential files verified; JSON logs redact bot tokens, invite links, and secret values | AUTOMATED_PASS |
| local test total | 76 collected, 76 passed, 0 failed, 0 skipped with PostgreSQL and MinIO enabled | AUTOMATED_PASS |
| GitHub Actions | Must run successfully for the exact final candidate SHA; record the SHA, run ID, and conclusion in the release handoff | EXACT_SHA_REQUIRED |
| live Telegram staging | No existing local credential was assumed safe; dedicated bot, MTProto account, users, database, Redis, and storage are still required | LIVE_TELEGRAM_REQUIRED |

The candidate remains `NOT_RELEASE_READY` until GitHub Actions is green for its
exact SHA and the live Telegram staging matrix passes with explicitly approved
isolated credentials.
