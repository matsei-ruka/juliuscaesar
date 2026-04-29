# Gateway: Sender Approval Prompt

**Branch**: `feat/gateway-sender-approval`

**Goal**: When an unauthorized sender (DM or chat) attempts contact, don't silently drop the message. Instead, send operator an approval/deny prompt and block transcripts until approved.

## Current Behavior

1. Message arrives at gateway
2. `_is_authorized(chat_id)` checks static allowlist (`ops/gateway.yaml`), operator DM, or DB `auth_status`
3. If unauthorized → silently dropped (no enqueue, no transcript, no notification)

**Problem**: Operator unaware someone tried to reach them. Legitimate contacts silently rejected.

## Proposed Behavior

1. Unauthorized message arrives
2. Check DB `chats` table:
   - `auth_status = "denied"` → drop silently (user explicitly rejected)
   - `auth_status = "pending"` → drop silently (already sent approval prompt, waiting)
   - No record / no status → continue to step 3
3. Upsert chat to `chats` with `auth_status = "pending"`, `first_seen = now()`
4. Send approval prompt to operator DM (TELEGRAM_CHAT_ID) with:
   - Sender handle (`@username` or numeric `user_id`)
   - Chat type (`private` / `group` / `supergroup`)
   - Chat ID (numeric, e.g., `28547271` or negative for groups)
   - Message preview (first 100 chars)
   - Inline keyboard: `[✅ Allow] [❌ Deny]`
5. Message NOT enqueued → no transcript entry
6. Future messages from same sender:
   - If approved → processed normally + logged
   - If denied → dropped silently
   - If still pending → dropped + no new prompt (already notified)

**Outcome**:
- Unknown senders never silently disappear
- Operator controls access without blocking the channel
- Transcripts only record approved conversations
- No retroactive processing of pending messages (they're lost if rejected)

## Implementation

### Files Changed

**`lib/gateway/channels/telegram.py`**:
1. Modify `run()` method (line ~550): In the `if not self._is_authorized(chat_id):` block
   - Add DB lookup: `status = self.db.get_chat_status(chat_id)` or query `chats` table
   - If `status in ("denied", "pending")` → continue (skip message)
   - If status is None/unknown → upsert chat with `pending`, call `_send_auth_prompt(chat_id, meta, content_preview)`
2. Extend `_send_auth_prompt()` (line ~267) to accept optional `content_preview` and `is_dm=True` flag
   - Refactor to handle both group-add context and new-sender-dm context
   - When `is_dm=True`: format as "New contact" instead of "Bot added to group"

**`lib/gateway/queue.py`**:
- No schema changes (DB `chats` table already has `auth_status` column)
- May add helper: `chat_pending(chat_id)` and `chat_deny(chat_id)` for readability (optional)

### Database

No migration needed. `chats` table already has:
```sql
auth_status TEXT DEFAULT NULL  -- NULL | 'pending' | 'allowed' | 'denied'
```

### Callback Handler

Existing `_handle_callback_query()` (line ~331) handles `chat_auth:<allow|deny>` prefixes and updates `auth_status`. No changes needed — seamlessly works for both group-add and new-sender flows.

## Testing

Unit test in `tests/gateway/test_telegram_sender_approval.py`:
1. **Test pending state**: Unauthorized message → upsert pending, send prompt, no enqueue
2. **Test idempotent pending**: Second message from same sender → drop, no new prompt
3. **Test approve flow**: Callback "allow" → update DB, future messages process
4. **Test deny flow**: Callback "deny" → update DB, future messages drop
5. **Test existing auth**: Messages from pre-approved chat_ids → process normally (no change)

Integration test in `tests/heartbeat/` (if needed): Simulate full gateway cycle.

## Timeline

- Spec approval: ✅ (this doc)
- Implementation: ~1-2h (mostly in telegram.py, ~40 lines net)
- Testing: ~1h
- Review & ship: ~30m

## Rollback

Remove `_send_auth_prompt()` call in `run()` method. DB state persists (safe — just marks chats as pending). Restart gateway.

## Alternatives Considered

1. **Opt-in per instance**: Some operators may want auto-accept or auto-deny. Could add `gateway.yaml` flags like `auto_approve: false`, `auto_deny: false`. Rejected for now — operator DM approval is clearer intent.
2. **Rate-limit prompts**: If a sender spams, send only 1 prompt per hour. Rejected — prompts are best-effort; rate-limit via Telegram itself or callback handler.
3. **Retroactive processing on approval**: When operator approves, replay pending messages. Rejected — could overwhelm operator; pending messages are discarded by design.
