# Spec: voice ASR/TTS use `env_value()` instead of `os.environ` directly

**Branch:** `feat/voice-env-value`
**Author:** Rachel
**Status:** draft
**Date:** 2026-05-07

## Problem

`lib/voice/asr.py:transcribe()` and `lib/voice/synth.py:_synthesize_pcm()` read `DASHSCOPE_API_KEY` directly from `os.environ`. When the gateway is started with a clean process environment — the normal case for cron-spawned watchdogs and any `env -i` invocation — the key is absent and the call raises `RuntimeError("Missing DASHSCOPE_API_KEY in env")`.

The error is caught by `VoiceChannel._asr()` / `_synthesize()` and swallowed into a log line (`voice asr error: ...` / `voice tts skipped: ...`). Inbound voice messages are silently dropped: no transcription, no reply, no escalation. The user sees nothing.

This is the second time the same gap has bitten production. The previous mitigation (commit `edaae8f`, "load .env in supervisor") was reverted (`a07219b`) because the merge strategy clobbered process-level env vars and broke the gateway subprocess. The proper fix lives in the voice layer, not the supervisor.

## Why the rest of the gateway is fine

Every other channel resolves secrets via `lib/gateway/config.py:env_value(instance_dir, name)`:

```python
def env_value(instance_dir: Path, name: str) -> str:
    return os.environ.get(name) or env_values(instance_dir).get(name, "")
```

It checks `os.environ` first, then falls back to the parsed `<instance>/.env`. That means a clean-env gateway still finds its secrets — the `.env` file is the source of truth.

Voice is the only subsystem that bypasses this helper.

## Goal

Voice ASR and TTS resolve `DASHSCOPE_API_KEY` via `env_value(instance_dir, "DASHSCOPE_API_KEY")`, identical to how the Telegram channel resolves `TELEGRAM_BOT_TOKEN`. Process env still wins when set (no behavior change for shells that export the key), but `.env` works as a fallback when it isn't.

After this change, voice works in:
- cron-spawned gateway (no exported user env)
- gateway started via `env -i HOME=… PATH=…`
- gateway started from any shell, with or without the key exported

## Non-goals

- Changing how the supervisor spawns children. Supervisor stays as-is.
- Reworking `env_value()` ordering. The cross-instance leak documented in `memory/L1/HOT.md` (CRITICAL env-leak: cross-instance bot impersonation) is a separate, higher-priority spec.
- Caching/refresh strategy for `.env`. `env_values()` already caches by mtime.

## Design

### Public surface change

Both top-level entry points gain an `instance_dir: Path` keyword argument. Existing callers update to pass it.

```python
# lib/voice/asr.py
def transcribe(
    audio_path: Path,
    *,
    instance_dir: Path,
    model: str = DEFAULT_MODEL,
    prompt: str = DEFAULT_PROMPT,
    url: str = URL_INTL,
    timeout_s: float = 120.0,
) -> str:
    ...
    api_key = env_value(instance_dir, "DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DASHSCOPE_API_KEY in env or instance .env")
    ...
```

```python
# lib/voice/synth.py
def synthesize(
    text: str,
    out_path: Path,
    *,
    instance_dir: Path,
    voice_id: str,
    target_model: str,
    ws_url: str = WS_URL_INTL,
) -> Path:
    ...

def _synthesize_pcm(
    text: str,
    *,
    instance_dir: Path,
    voice_id: str,
    target_model: str,
    ws_url: str,
    pcm_path: Path,
    timeout_s: float = 120.0,
) -> None:
    ...
    api_key = env_value(instance_dir, "DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DASHSCOPE_API_KEY in env or instance .env")
    dashscope.api_key = api_key
    ...
```

### Import wiring

`voice/asr.py` and `voice/synth.py` currently sit under `lib/voice/` and the gateway imports them via `import_module("voice.asr")` / `import_module("voice.synth")`. They will import `env_value` from `gateway.config`:

```python
from gateway.config import env_value
```

Both `voice` and `gateway` already coexist on the install's `sys.path` (see `lib/gateway/channels/voice.py:83` and `:122`), so no packaging change needed.

### Caller updates

Two call sites in `lib/gateway/channels/voice.py`:

```python
# _asr (line 86)
return str(mod.transcribe(audio_path, instance_dir=self.instance_dir))

# _synthesize (line 123)
result = synth.synthesize(
    text,
    out_path,
    instance_dir=self.instance_dir,
    voice_id=str(voice_id),
    target_model=str(target_model),
)
```

One call site in `lib/gateway/channels/telegram_media.py:59`:

```python
mod = import_module("voice.asr")
return str(mod.transcribe(audio_path, instance_dir=instance_dir))
```

(`telegram_media`'s caller already has `instance_dir` available; threading it through is mechanical.)

## Files touched

| File | Change |
|---|---|
| `lib/voice/asr.py` | add `instance_dir` kw arg to `transcribe`; replace `os.environ.get(...)` with `env_value(instance_dir, ...)`; import `env_value` |
| `lib/voice/synth.py` | add `instance_dir` kw arg to `synthesize` + `_synthesize_pcm`; replace `os.environ.get(...)` with `env_value(...)`; import `env_value` |
| `lib/gateway/channels/voice.py` | pass `self.instance_dir` to `transcribe` and `synthesize` |
| `lib/gateway/channels/telegram_media.py` | pass `instance_dir` to `transcribe` |
| `tests/voice/test_asr.py` (if exists) | update fixtures |
| `tests/voice/test_synth.py` (if exists) | update fixtures |

## Backwards compatibility

`instance_dir` is a required kw arg. Any external caller (none known in-tree) breaks at call site with a clear `TypeError: transcribe() missing 1 required keyword-only argument: 'instance_dir'`. Acceptable — voice is internal.

No `.env` schema change. No config file change. No migration script.

## Test plan

1. **Unit**
   - `transcribe()` with `instance_dir` pointing at a tmpdir containing `.env` with `DASHSCOPE_API_KEY=test` → key found.
   - Same with `os.environ["DASHSCOPE_API_KEY"]` set to a different value → process env wins.
   - Neither set → `RuntimeError("Missing DASHSCOPE_API_KEY ...")`.
   - Mirror for `synthesize()`.

2. **Integration (manual smoke)**
   - Start Marco's gateway with `env -i HOME=… PATH=…` (no `DASHSCOPE_API_KEY` exported).
   - Send a Telegram voice note.
   - Verify `state/gateway/gateway.log` shows ASR success, not `voice asr error: Missing DASHSCOPE_API_KEY`.
   - Verify reply comes back as voice.

3. **Regression**
   - Start a gateway from a shell that exports `DASHSCOPE_API_KEY=…`.
   - Voice flow still works (process env path unchanged).

## Rollout

1. Land on `feat/voice-env-value`.
2. PR against `main`.
3. After merge, fleet upgrade: `git pull && ./install.sh` per instance, watchdog restarts gateway naturally on next tick.
4. Update `memory/L1/HOT.md` "Known nuisances" — strike the voice/DASHSCOPE workaround entry.

## Open questions

- Is there any external (out-of-tree) caller of `voice.asr.transcribe` or `voice.synth.synthesize`? (Grep on rachel_zane returns no in-tree callers other than the four enumerated above. Worth a final ripgrep across all instance dirs before merging.)
