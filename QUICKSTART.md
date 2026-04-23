# Quickstart

From zero to a working JuliusCaesar instance in ~10 minutes.

## Prerequisites

- Linux host (Ubuntu 22.04+ tested). macOS likely works but untested.
- Python 3.11+, `bash`, `screen`, `curl`, `ffmpeg`, `git`.
- An always-on box if you want 24/7 availability (VPS, local always-on machine, etc.).
- A [Claude Code](https://www.anthropic.com/claude-code) subscription (Max recommended for scheduled tasks).
- A DashScope API key from Alibaba Cloud if you want voice (TTS/ASR).
- A Telegram bot token if you want chat notifications.

## Install the framework

```bash
git clone https://github.com/matsei-ruka/juliuscaesar.git ~/juliuscaesar
cd ~/juliuscaesar
./install.sh
```

This creates a venv at `~/.local/share/juliuscaesar/venv`, installs Python deps (pyyaml, python-dotenv, dashscope, requests), and writes shims into `~/.local/bin/` for `jc`, `jc-memory`, `jc-heartbeat`, `jc-voice`, `jc-watchdog`, `jc-init`.

Make sure `~/.local/bin` is on your `$PATH`. Verify:

```bash
jc help
```

## Scaffold your first instance

Pick a directory name for your assistant. We'll use `~/my-assistant`.

```bash
jc init ~/my-assistant
cd ~/my-assistant
```

What `jc init` creates:

```
my-assistant/
├── .jc               # marker so jc-* tools auto-discover this instance
├── .env              # credentials — fill this in next (mode 600)
├── .gitignore        # sensible defaults (.env, index.sqlite, state/, etc.)
├── memory/           # llm-wiki + FTS5 knowledge base
│   ├── L1/           # always-loaded (identity, user profile, rules, hot cache)
│   ├── L2/           # on-demand topical entries
│   ├── LOG.md        # operation audit trail
│   └── raw/          # immutable source archives
├── heartbeat/
│   ├── tasks.yaml    # scheduled task definitions (seeded with a 'hello' smoke task)
│   └── fetch/        # bash scripts that fetch data before synthesis
└── voice/
    ├── references/   # cloned voice metadata goes here after enroll
    └── tmp/          # scratch dir for generated audio
```

## Fill in .env

```bash
vim .env
```

Required depending on features you use:

```
# Voice (TTS + ASR via DashScope Qwen, Singapore/intl endpoint)
DASHSCOPE_API_KEY=sk-...

# Telegram notifications from heartbeat + watchdog
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=1234567890
```

To get a Telegram bot token, chat with [@BotFather](https://t.me/BotFather). To find your chat_id, message your bot and then `curl https://api.telegram.org/bot<TOKEN>/getUpdates`.

## Seed memory

Edit the four L1 files to describe who this assistant is, who they help, and what rules to follow:

- `memory/L1/IDENTITY.md` — personality, voice, boundaries
- `memory/L1/USER.md` — the user's profile, preferences, context
- `memory/L1/RULES.md` — standing corrections and feedback
- `memory/L1/HOT.md` — rolling 7-day hot cache (start empty)

Then index:

```bash
jc memory rebuild
```

Try a search:

```bash
jc memory search "identity"
```

## Enroll a voice (optional)

Record 10–20s of clean mono audio at ≥24kHz (`.mp3`, `.m4a`, or `.wav`), then:

```bash
jc voice enroll ~/voice-sample.mp3 --name myassistant
```

This returns a voice id and writes `voice/references/voice.json`. Test:

```bash
jc voice speak "This is my cloned voice."
```

The output OGG lands at `voice/tmp/out.ogg`. Use `--out <path>` to control.

## Test the heartbeat pipeline

```bash
jc heartbeat run hello --dry-run
```

The `hello` task is seeded in `tasks.yaml` as a smoke test. `--dry-run` skips Telegram send and prints the output.

Drop `--dry-run` to send it for real. You should see a message on Telegram within a few seconds.

## Install the watchdog (optional but recommended)

If you're running on an always-on box and want your `claude` session to auto-recover from crashes or auto-updates:

```bash
jc watchdog install
```

This writes two crontab entries (`@reboot` + every 2 minutes) that supervise the screen session and respawn claude on death. You get a Telegram ping when it restarts.

To pin a specific claude session id (so `--resume` keeps conversation memory across restarts), edit `ops/watchdog.conf`:

```
SESSION_ID=<uuid-from-~/.claude/sessions>
```

## You're done

```bash
jc doctor        # verify the whole setup
jc watchdog status
jc memory search "..."
jc heartbeat run <task>
jc voice speak "..."
```

Put the `my-assistant/` directory under private git if you want version control — `.env` and `index.sqlite` are already gitignored.

## Troubleshooting

- `jc` not found → `~/.local/bin` isn't on `$PATH`. Add it to your shell config.
- `DASHSCOPE_API_KEY not set` → you skipped the `.env` step, or forgot to `source` a shell. `.env` is auto-loaded by all `jc` commands.
- Heartbeat says "adapter not found" → the tool's CLI (`claude`, `gemini`, etc.) isn't installed. `jc doctor` tells you which.
- Telegram plugin dies silently → framework watchdog detects it and restarts claude. Your only visible signal is the "back alive" ping.

## More

- Architecture: [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)
- Roadmap: [ROADMAP.md](./ROADMAP.md)
- Issues: [GitHub Issues](https://github.com/matsei-ruka/juliuscaesar/issues)
