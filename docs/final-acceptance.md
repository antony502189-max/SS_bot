# Final acceptance matrix

Audited 2026-08-26. `BLOCKED` means the required Telegram workflow is not yet
implemented end to end; it is deliberately not reported as a pass.

| Feature | Code | Automated test | PostgreSQL test | Live Telegram test | Status |
| --- | --- | --- | --- | --- | --- |
| Telegram-only product surface | Yes | Yes | N/A | Not required | PASS |
| Registration and role menu | Yes | Yes | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| Administration and sectors from Telegram | Yes | Partial | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| Events from Telegram | Yes | Partial | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| Complete task wizard and task card | Yes | Partial | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| Report draft/return/resubmit lifecycle | Yes | Yes | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| Telegram photo report workflow | Yes | Partial + MinIO gated test | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| Group creation, invitation, recovery | Yes | Partial | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| Cleanup safety | Yes | Yes | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| Archive PDF/ZIP delivery from Telegram | Yes | Partial | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| Retention and purge | Yes | Yes | Pending CI | Required | LIVE_VERIFICATION_REQUIRED |
| PostgreSQL migration | Yes | Yes | PostgreSQL 16 empty-database upgrade passed | N/A | PASS |
| Object storage | Yes | MinIO integration test | N/A | Required | LIVE_VERIFICATION_REQUIRED |

No production release claim is made until the `LIVE_VERIFICATION_REQUIRED` staging
procedure is recorded against isolated Telegram credentials.
