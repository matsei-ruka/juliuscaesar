# Quickstart

From zero to a running JuliusCaesar instance in ~15 minutes on Linux.

---

## 1. Prerequisites on the target machine

```bash
sudo apt update
sudo apt install -y python3 python3-venv git curl screen ffmpeg
```

Verify:

```bash
python3 --version     # must be ≥ 3.10
which git curl screen ffmpeg
```

Install [Claude Code](https://www.anthropic.com/claude-code) and log in:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude /login        # interactive; completes in your browser
```

---

## 2. Register a Telegram bot (one-time)

Skip this section if your instance won't use Telegram.

1. On your phone, open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts. Pick a display name and a unique `@handle` ending in `_bot`.
3. Save the token BotFather returns (looks like `123456789:ABC…`).
4. Message your new bot any text (e.g. `hi`). This creates the first update so the API has something to return.
5. On the target machine, fetch your chat id:

   ```bash
   TOKEN=123456789:ABC...
   curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates" | python3 -m json.tool
   ```

   Look for `"chat": { "id": <NUMBER> }` in the JSON. That number is your `TELEGRAM_CHAT_ID`.

6. Install the Claude Code telegram plugin (required for inbound/outbound when the live session is running):

   ```bash
   claude
   > /plugins install claude-plugins-official/telegram
   > /exit
   ```

---

## 3. Install the framework

```bash
git clone https://github.com/matsei-ruka/juliuscaesar.git ~/juliuscaesar
cd ~/juliuscaesar
./install.sh
```

Add `~/.local/bin` to your shell PATH if it isn't already:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Verify:

```bash
jc help
```

You should see the router listing `memory`, `heartbeat`, `voice`, `watchdog`, `init`, `doctor`.

---

## 4. Configure an instance

Pick a directory for your assistant. Anywhere you have write access works. Common choices:

- `~/my-assistant/` — your home dir, no special setup.
- `/opt/my-assistant/` — shared system location; needs a quick chown so the running user can write to it.

```bash
# Option A — home dir
jc setup ~/my-assistant
cd ~/my-assistant

# Option B — /opt (requires sudo for the mkdir)
sudo mkdir -p /opt/my-assistant
sudo chown $(id -u):$(id -g) /opt/my-assistant
jc setup /opt/my-assistant
cd /opt/my-assistant
```

`jc setup` asks for assistant name, user profile, timezone, communication style,
optional Telegram credentials, optional DashScope key, and whether to start the
live runtime or install watchdog. It uses `jc init` underneath when the target is
not already a JC instance.

For automation or tests, use safe defaults:

```bash
jc setup ~/my-assistant --defaults
```

The configured instance contains:

```
my-assistant/
├── .jc                # marker — other jc-* tools auto-discover this instance
├── .env               # secrets (mode 600) — fill in next step
├── .gitignore
├── memory/
│   ├── L1/{IDENTITY,USER,RULES,HOT}.md    # seeded by setup
│   ├── L2/{people,business,projects,learnings,reference}/
│   ├── LOG.md
│   └── raw/
├── heartbeat/
│   ├── tasks.yaml     # 'hello' smoke task pre-seeded
│   └── fetch/
├── voice/
│   ├── references/
│   └── tmp/
└── ops/
    └── watchdog.conf  # SESSION_ID + SCREEN_NAME overrides
```

---

## 5. Review credentials and identity

`jc setup` writes `.env` and L1 memory from your answers. You can edit them
afterward:

```bash
vim .env
vim memory/L1/IDENTITY.md
vim memory/L1/USER.md
vim memory/L1/RULES.md
```

`.env` should be mode 600 (auto-set by `jc setup`; re-apply with `chmod 600 .env` if you copied it).

---

## 6. Rebuild and verify

```bash
jc memory rebuild
jc doctor
```

All critical checks should be green. Telegram credentials are validated with a live `getMe` ping.

---

## 7. Enroll a voice (optional)

Need a 10–20s clean mono audio sample (mp3/m4a/wav at ≥24kHz):

```bash
jc voice enroll /path/to/sample.mp3 --name myassistant
jc voice speak "Testing, one two three." --out /tmp/test.ogg
```

The returned voice id is written to `voice/references/voice.json`.

---

## 7.5. Route reports to multiple chats (optional — destinations)

By default, every heartbeat task posts to `$TELEGRAM_CHAT_ID` from `.env` (your DM with the bot). If you want tasks to route to different chats — e.g. routine reports to a team group and critical alerts to the owner's DM — declare named destinations in `heartbeat/tasks.yaml`:

```yaml
destinations:
  owner-dm:
    channel: telegram
    chat_id: 123456789             # your personal DM
  team-group:
    channel: telegram
    chat_id: -1001234567890        # group chat (negative chat id)

tasks:
  daily_report:
    destination: team-group        # single
    prompt: |
      ...

  critical_alert:
    destination: owner-dm
    prompt: |
      ...

  announcement:
    destination: [team-group, owner-dm]   # multi-fanout, one message to each
    prompt: |
      ...
```

To post in a group, add the bot to it, then send any message there and grep `curl https://api.telegram.org/bot<TOKEN>/getUpdates` for the `chat.id` (negative number). For a group bot to see non-mention messages, set its privacy to "Disabled" in @BotFather (`/setprivacy`).

`jc doctor` validates the `destinations:` block — warns on broken references and unused entries. Backward-compat: if `destinations:` is absent, tasks continue to use `TELEGRAM_CHAT_ID` from `.env`.

---

## 8. Test the heartbeat

```bash
jc heartbeat run hello --dry-run     # synthesis only; no Telegram send
jc heartbeat run hello               # sends to Telegram for real
```

You should see a message on your phone within a few seconds.

---

## 9. Start the live runtime

```bash
INSTANCE_DIR="$(pwd)"
screen -dmS myassistant bash -c 'cd "$1" && exec claude --dangerously-skip-permissions --chrome --channels plugin:telegram@claude-plugins-official' _ "$INSTANCE_DIR"
```

Wait ~10s, then:

```bash
jc doctor
```

`live claude process running` should now be green, and `telegram plugin alive` should report a pid.

Find the session id so the watchdog can restore conversation memory on restart:

```bash
ls -lat ~/.claude/projects/*$(basename $(pwd))*/*.jsonl | head -1
```

Copy the UUID portion of the filename (strip `.jsonl`), then:

```bash
vim ops/watchdog.conf
```

Uncomment and fill in:

```
SESSION_ID=<uuid-from-above>
SCREEN_NAME=myassistant
```

---

## 10. Install the watchdog

```bash
jc watchdog install
jc watchdog status
```

Two cron entries get installed (`@reboot` + every 2 minutes). If claude dies (crash, auto-update, or Telegram plugin death), the watchdog respawns it with `--resume`, and you get a Telegram ping when it's back.

---

## 11. Workers (on-demand background agents, optional)

`jc heartbeat` handles *scheduled* tasks. `jc workers` handles *on-demand* tasks — dev work the user triggers interactively, that shouldn't block the chat session.

```bash
# Spawn a detached worker. Stdin is the full prompt.
echo "Refactor the Telegram adapter for async I/O" | \
  jc workers spawn --topic "telegram async refactor" \
                   --brain claude --model claude-opus-4-7

# Watch it progress. The worker is fully detached from this shell.
jc workers list
jc workers tail <id>       # tail -f the log
jc workers show <id>       # full state + result preview

# Kill one
jc workers cancel <id>

# Maintenance
jc workers reconcile       # mark stale 'running' rows as failed
jc workers gc --days 7     # purge old rows (add --prune-files to remove logs)
```

The worker writes its prompt, log, and result under `<instance>/state/workers/<id>/`. When it reaches a terminal state (done/failed/cancelled/need_input), the runner sends a Telegram summary to `$TELEGRAM_CHAT_ID` (or a per-worker `--notify <chat_id>`).

**When to spawn vs. do inline:** quick answers and single-file edits → inline; multi-file refactors, research, scaffolding, anything iterative → spawn. See `docs/specs/workers.md` for the full design.

---

## Done

Normal day-to-day:

```bash
jc memory search "query"
jc memory read <slug>
jc heartbeat run <task>
jc voice speak "text"
jc workers list
jc watchdog status
jc doctor
```

DM the Telegram bot and the live session will respond.

---

## Troubleshooting

**`jc` not found** → `~/.local/bin` isn't on `$PATH`. See step 3.

**`python3 required` or Python version error on install** → `install.sh` needs Python 3.10+. Upgrade (pyenv, deadsnakes, or `apt install python3.10`).

**`DASHSCOPE_API_KEY not set`** → your `.env` is missing a value or isn't being sourced. Every `jc-*` command auto-sources `.env` at the instance root.

**`jc doctor` says `telegram plugin dead`** → restart the live claude session (`exit` inside the screen and let the watchdog respawn it, or `screen -S <name> -X quit && screen -dmS <name> ...`).

**`tasks.yaml` adapter error** → the CLI for that adapter (`claude`, `gemini`, etc.) isn't installed or isn't authenticated. `jc doctor` flags which are missing.

**`jc init` refuses to scaffold** → the target dir already has files (expected for a JC instance to be scaffolded into an empty directory) or a `.jc` marker (already an instance).

**Shim collision** (`install.sh` refuses to overwrite) → another juliuscaesar clone is already installed. Either use that clone, or rerun `./install.sh --force` to repoint the shims to this clone.

---

## What's next

- [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) — how the pieces fit together
- [ROADMAP.md](./ROADMAP.md) — shipped + planned features
- Issues: open one at https://github.com/matsei-ruka/juliuscaesar/issues
