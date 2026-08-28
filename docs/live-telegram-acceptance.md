# Live Telegram staging acceptance

This procedure remains required because the locally available credentials were
not identified as an isolated, disposable staging environment and were not used.
Use only an isolated bot, at least three test user accounts,
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
4. As an administrator create two group tasks: one led by the creator and one
   led by a selected participant. Set a checklist and event, then verify the
   worker creates exactly one supergroup per task and pins its brief. Change
   leader, participant list, and checklist text before reporting.
5. Verify direct invitation; test the privacy fallback with an account that
   cannot be directly added. Confirm direct success records `JOINED`; confirm
   fallback records `INVITED`, sends a personal link, and schedules a reminder.
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
9. Exercise cleanup only with a disposable task. Set `APP_ENV=staging` and an
   explicit `STAGING_TASK_CLEANUP_MINUTES` value from 5 to 60; the override is
   ignored by every other environment. Verify warning, invite revocation, group
   deletion, and retention of database/archive/photo records. Remove the
   staging override after the test.
10. Record chat IDs, timestamps, logs, and database rows for each result.

Current result (2026-08-28): `LIVE_TELEGRAM_REQUIRED`. Mocked adapter and
handler tests pass; no real Telegram action is represented as a pass.
