# Telegram integration notes

Verified on 2026-08-25 against the official Telegram documentation:

- The Bot API cannot create a supergroup. The isolated MTProto service-account adapter uses `channels.createChannel` with `megagroup=true`; direct group invitations use `channels.inviteToChannel`. [MTProto methods](https://core.telegram.org/methods), [group API overview](https://core.telegram.org/api/channel)
- Direct invitations can fail because of a user’s privacy/contact restrictions. The adapter classifies those failures as `privacy_restricted`, then creates a single-use invite using the MTProto invite API. [Invite links and direct invites](https://core.telegram.org/api/invites)
- A bot administrator with invitation rights can create and revoke its own invite links through `createChatInviteLink` and `revokeChatInviteLink`; it cannot manage invite links made by other administrators. [Bot API](https://core.telegram.org/bots/api)
- The bot receives other members’ `chat_member` updates only when it is an administrator and `chat_member` is explicitly enabled in `allowed_updates`. The bot records a join and immediately clears `next_reminder_at`. [Bot API updates](https://core.telegram.org/bots/api)
- The service account promotes the bot through `channels.editAdmin` with `ChatAdminRights(other=true)`: Telegram recognises the bot as an admin but no operational moderation or invite rights are granted. The worker also checks unresolved invitations with `channels.getParticipant` and records the result. [Admin changes](https://core.telegram.org/method/channels.editAdmin), [admin-right fields](https://core.telegram.org/constructor/chatAdminRights), [participant lookup](https://core.telegram.org/method/channels.getParticipant)
- Task access removal is processed through `channels.editBanned`; the task-chat record changes to `removed` only after Telegram confirms the operation. The group brief is posted and pinned by the service account once per persisted task chat. [Member removal](https://core.telegram.org/method/channels.editBanned)
- Telegram can throttle MTProto actions through `FLOOD_WAIT`. The adapter returns a typed `flood_wait` result with the server-specified retry delay instead of treating it as a successful operation.

Operational decisions:

1. Use a dedicated, human-owned Telegram service account only. Store its Telethon session outside Git, preferably as a mounted secret.
2. Add the bot after group creation and promote it with the minimum administrator flag needed for membership updates. Smoke-test that permission path in a non-production group before enabling production chat creation.
3. The cleanup worker revokes tracked links before it asks Telegram to delete the group. PostgreSQL and object storage are never deleted by this job.
4. Live Telegram operations were not executed by this repository build.
