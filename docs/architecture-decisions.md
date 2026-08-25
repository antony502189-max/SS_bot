# Architecture decisions

## ADR-001: PostgreSQL and S3 are authoritative

Task, report, audit, and photo metadata are stored before Telegram side effects. The Telegram group is temporary integration state represented by `task_chats` and `task_chat_members`.

## ADR-002: Durable side effects use an outbox

Task creation writes `TASK_CREATED` in the same transaction as task members and checklist items. The worker processes it under a database lock and records attempts/errors. Reprocessing does not create another task chat because `task_chats.task_id` is unique.

## ADR-003: Bot token is not a service-account credential

The standard Bot API bot handles registration, direct messages, Mini App validation, and membership updates. A distinct MTProto service account handles supergroup lifecycle work. Their credentials are separate environment secrets.

## ADR-004: Photo metadata follows storage verification

The API issues a short-lived private S3 PUT URL, then accepts a photo record only after a `head_object` verification. A report is limited to five JPEG, PNG, or WebP files of at most 10 MiB each.

