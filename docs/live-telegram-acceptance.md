# Live Telegram staging acceptance

This procedure is required because no non-production Telegram credentials are
available in this workspace. Use only an isolated bot, two test user accounts,
and a dedicated MTProto service account; never run cleanup tests against a
production group.

## Required configuration

Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`,
`BOOTSTRAP_ADMIN_TELEGRAM_IDS`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and a
mounted `TELEGRAM_SERVICE_SESSION_PATH`. Configure PostgreSQL, Redis, and an
S3-compatible bucket. For webhook mode set `TELEGRAM_WEBHOOK_URL` and a random
`TELEGRAM_WEBHOOK_SECRET`.

```powershell
docker compose up --build
docker compose logs -f api worker bot
```

## Procedure

1. Send `/start` from test users A, B, and C; verify full-name capture,
   username refresh, and the role-specific keyboards.
2. As an administrator create a sector, assign A to it, grant A Sector Head,
   deactivate/reactivate the sector, and inspect the resulting entries in the
   bot's action log. Confirm a Sector Head cannot access another sector.
3. Create an event with dates, budget, description, sector, and participants.
   From its card add and remove a participant, edit all fields, change sector
   as administrator, view its archive summary, and extend its retention date.
4. As an administrator create a group task, select a participant and a separate
   leader, set a checklist and event, then verify the worker creates exactly
   one supergroup and pins its brief. Change leader, participant list, and
   checklist text from the task card before reporting.
5. Verify direct invitation; test the privacy fallback with an account that
   cannot be directly added. Confirm the invite link is personal.
6. Wait for a reminder window, join immediately before it is due, and confirm
   no reminder is sent. Repeat with a non-joining account and confirm a single
   reminder is sent. Use the manager status, retry, and recovery controls if a
   deliberate transient failure is injected.
7. Complete checklist items, save a report draft, upload JPEG, PNG, and WebP
   photos, preview and delete a draft photo, then submit a group report. Return
   it as leader with a reason, edit/resubmit it, and approve it. Also submit an
   individual-task report and confirm it closes immediately.
8. Generate the event PDF and photo ZIP in Telegram and verify task members,
   statuses, reports, photo counts, deadline, and budget appear in the archive.
9. Exercise cleanup only with a disposable task and a safe staging-only shortened
   deadline. Verify warning, invite revocation, group deletion, and retention of
   database/archive/photo records.
10. Record chat IDs, timestamps, logs, and database rows for each result.

Current result: `LIVE_VERIFICATION_REQUIRED`.
