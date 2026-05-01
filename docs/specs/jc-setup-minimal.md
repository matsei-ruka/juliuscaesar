# jc-setup minimal mode + .env preservation

## Goal

Reduce `jc-setup` to only ask determinant questions. Stop generating generic L1 boilerplate. Preserve existing `.env` values instead of overwriting.

## Scope

`bin/jc-setup` — one file. Tests in `tests/cli/test_setup_*.py` updated/added.

## Current Behavior (Broken)

- 7 prompts: assistant_name, user_name, timezone, mission, style, external_policy, default_brain
- 5 secret prompts: dashscope, telegram_token, telegram_chat, slack_app_token, slack_bot_token
- Generates IDENTITY.md, USER.md, RULES.md, HOT.md from boilerplate templates with answers interpolated
- Always overwrites `.env` with answered values (or empty strings if not answered)
- `WRITE_CONFIG=yes` hardcoded at line 24 → bypasses the existing-instance "ask before rewriting" check

## New Behavior

### Prompts kept (determinant)

- `assistant_name` → `IDENTITY.md` (minimal scaffold), `gateway.yaml` instance label
- `user_name` → `USER.md` (minimal scaffold)
- `timezone` → `USER.md`
- `default_brain` → `gateway.yaml`

### Prompts dropped (generic boilerplate)

- ❌ `mission` — operator hand-writes IDENTITY.md after setup
- ❌ `style` — operator hand-writes IDENTITY.md / USER.md
- ❌ `external_policy` — operator hand-writes RULES.md

### Secret prompts (preserve existing)

For each secret (dashscope, telegram_token, telegram_chat, slack_app_token, slack_bot_token):
- Read existing value from `.env` if file exists
- Show prompt with `[keep existing]` indicator if value present
- If user hits Enter → keep existing value
- If user types value → overwrite
- If file doesn't exist → empty default, prompt as before

### File generation logic

| File | Existing? | Behavior |
|---|---|---|
| `IDENTITY.md` | exists | skip (don't overwrite) |
| `IDENTITY.md` | missing | write minimal scaffold (name + placeholder mission) |
| `USER.md` | exists | skip |
| `USER.md` | missing | write minimal scaffold (name + timezone) |
| `RULES.md` | exists | skip |
| `RULES.md` | missing | write minimal scaffold (placeholder rules) |
| `HOT.md` | exists | skip |
| `HOT.md` | missing | write empty hot file with frontmatter |
| `gateway.yaml` | always rewrite | (current behavior; non-secret config) |
| `.env` | always rewrite | (with existing-value preservation per above) |
| `watchdog.yaml` | exists | skip |
| `watchdog.yaml` | missing | write default |

### `--force` flag

Add `--force` flag: opt-in to rewrite existing L1 files / watchdog config (current `WRITE_CONFIG=yes` behavior). Default off.

### Removed

- Line 24: `WRITE_CONFIG=yes` (now derived from `--force` or interactive prompt)
- Lines 421-423: `WRITE_CONFIG="$(yesno "...")"` (now `--force`-driven, not asked)

## Minimal Scaffold Templates

### IDENTITY.md (when missing)

```yaml
---
slug: IDENTITY
title: Identity
layer: L1
type: identity
state: draft
created: <today>
updated: <today>
last_verified: "<today>"
tags: [identity]
links: [USER, RULES]
---

# Who this assistant is

<assistant_name> is a JuliusCaesar assistant.

## Mission

TODO: Hand-write what this assistant exists to do.

## Voice and style

TODO: Hand-write voice and style preferences.
```

### USER.md (when missing)

```yaml
---
slug: USER
title: User profile
layer: L1
type: user
state: draft
...
---

# Who the assistant is helping

- Name: <user_name>
- Timezone: <timezone>

TODO: Hand-write user details, preferences, standing rules.
```

### RULES.md (when missing)

```yaml
---
slug: RULES
...
---

# Standing rules

TODO: Hand-write rules.
```

### HOT.md (when missing)

```yaml
---
slug: HOT
...
---

# Hot cache

(empty)
```

## .env Preservation Logic

Replace current `write_env()` with:

```bash
write_env() {
    local key value existing_value tmp old_umask
    local -A new_values=(
        [DASHSCOPE_API_KEY]="$1"
        [TELEGRAM_BOT_TOKEN]="$2"
        [TELEGRAM_CHAT_ID]="$3"
        [SLACK_APP_TOKEN]="$4"
        [SLACK_BOT_TOKEN]="$5"
    )

    old_umask=$(umask)
    umask 077
    tmp=$(mktemp "$TARGET/.env.tmp.XXXXXX")
    {
        printf '%s\n' "# JuliusCaesar instance secrets. Do not commit."
        for key in DASHSCOPE_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID SLACK_APP_TOKEN SLACK_BOT_TOKEN; do
            value="${new_values[$key]}"
            # If user passed empty AND existing .env has value, preserve existing
            if [[ -z "$value" && -f "$TARGET/.env" ]]; then
                existing_value=$(grep "^${key}=" "$TARGET/.env" 2>/dev/null | head -1 | cut -d= -f2-)
                value="${existing_value//\'/}"
            fi
            printf '%s=%s\n' "$key" "$(shell_quote "$value")"
        done
    } > "$tmp"
    umask "$old_umask"
    mv "$tmp" "$TARGET/.env"
    chmod 600 "$TARGET/.env"
}
```

## Tests

`tests/cli/test_setup_minimal.py` (new):

- `test_setup_skips_existing_l1` — pre-create IDENTITY.md, run setup, confirm not overwritten
- `test_setup_writes_minimal_l1_when_missing` — fresh dir, run setup, confirm scaffold written
- `test_setup_preserves_env_secrets` — pre-create .env with TELEGRAM_BOT_TOKEN=abc, run setup with empty input, confirm token preserved
- `test_setup_force_overwrites_l1` — `--force` flag, pre-existing L1 → overwritten
- `test_setup_no_mission_prompt` — confirm setup doesn't prompt for mission/style/external_policy

## Migration

None needed. Existing instances unaffected — they already have L1 files, so the new "skip if exists" behavior is a no-op for them. Operators can re-run `jc-setup` safely without losing customization.

## Success Criteria

- ✅ Setup prompts reduced from 12 to 6 (4 names + brain + 5 optional secrets)
- ✅ Existing L1 files never overwritten (without `--force`)
- ✅ Existing `.env` secrets preserved when user hits Enter
- ✅ `WRITE_CONFIG=yes` hardcoded line removed
- ✅ All new tests pass
- ✅ Re-running `jc-setup` on existing instance is idempotent
