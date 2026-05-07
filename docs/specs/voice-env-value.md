# Spec: instance-local env resolution for voice and runtime subprocesses

**Branch:** `feat/voice-env-value`
**Author:** Codex
**Status:** implemented
**Date:** 2026-05-07

## Problem

Voice ASR/TTS read `DASHSCOPE_API_KEY` directly from `os.environ`. That fails under clean cron/watchdog launches where the process env does not export the key, even though the target instance has the key in `<instance>/.env`.

The earlier watchdog-level mitigation was unsafe because it loaded `.env` into broad runtime processes. On multi-instance hosts that allows ambient user env, runtime-control names, or another instance's values to influence the wrong gateway.

## Invariant

Secrets are instance-local:

- `<instance>/.env` wins when a safe secret/provider key is present there.
- Process env is only a fallback when the key is absent from the instance `.env`.
- Two instances running under the same Linux user must resolve their own `.env` values, not whichever token happened to be exported by the launching shell.
- Runtime-control variables from `.env` are ignored when building subprocess envs. Examples include `PATH`, `HOME`, `PYTHONPATH`, `RUNTIME_MODE`, `SCREEN_NAME`, `SESSION_ID`, `JC_*`, `CODEX_*`, and `WORKER_*`.

## Design

`lib/gateway/config.py` owns the boundary:

- `env_value(instance_dir, name)` reads the instance `.env` first for allowed keys, then falls back to `os.environ`.
- `safe_instance_env_values(instance_dir)` filters `.env` to keys that are allowed to cross into subprocesses.
- `merge_instance_env(instance_dir, base=None)` copies the base/process env and overlays only safe instance `.env` keys.
- `apply_instance_env(instance_dir)` mutates the current process with only safe instance `.env` keys for CLI paths that still call legacy helpers.

Voice ASR/TTS entry points now take `instance_dir` as a required keyword argument:

```python
transcribe(audio_path, *, instance_dir=instance_dir)
synthesize(text, out_path, *, instance_dir=instance_dir, voice_id=..., target_model=...)
```

Gateway voice and Telegram-media callers pass the active instance through to those functions.

Runtime subprocess launchers use `merge_instance_env()` before setting their explicit runtime-control variables:

- gateway brain adapters
- heartbeat adapters, pre-fetch scripts, and Telegram delivery
- background worker adapters
- Python watchdog v2 child daemons and alert delivery

The legacy bash watchdog keeps its allowlisted `.env` parser. It may import secret/provider keys such as `TELEGRAM_BOT_TOKEN` and `DASHSCOPE_API_KEY`, but it ignores runtime-control keys like `PATH`, `RUNTIME_MODE`, and `SCREEN_NAME`.

## Files touched

| File | Change |
|---|---|
| `lib/gateway/config.py` | instance-first `env_value()`, safe env filter, merge/apply helpers |
| `lib/voice/asr.py` | require `instance_dir`; resolve `DASHSCOPE_API_KEY` through `env_value()` |
| `lib/voice/synth.py` | require `instance_dir`; resolve `DASHSCOPE_API_KEY` through `env_value()` |
| `lib/gateway/channels/voice.py` | pass channel instance to ASR/TTS |
| `lib/gateway/channels/telegram.py` | pass channel instance through Telegram audio transcription |
| `lib/gateway/channels/telegram_media.py` | require `instance_dir` for voice-note ASR |
| `bin/jc-voice` | apply safe instance env for CLI operations |
| `lib/gateway/brains/base.py` | launch adapters with safe instance env |
| `lib/heartbeat/runner.py` | launch pre-fetch, adapters, and sender with safe instance env |
| `bin/jc-workers` | launch worker adapters with safe instance env |
| `lib/watchdog/supervisor.py` | launch supervised children and alerts with safe instance env |
| `lib/watchdog/watchdog.sh` | restore allowlisted bash `.env` loading |

## Backwards compatibility

`voice.asr.transcribe()` and `voice.synth.synthesize()` now require a keyword-only `instance_dir`. In-tree callers have been updated. Out-of-tree callers fail at call time with a clear missing-argument error and should pass the target instance directory.

No `.env` schema change is required.

## Test plan

Automated coverage:

- `tests/gateway/test_config_env.py`: instance `.env` wins over process env; process fallback still works; two instances under the same user resolve separate tokens; runtime-control keys are filtered from env merges.
- `tests/voice/test_env_lookup.py`: ASR/TTS use `DASHSCOPE_API_KEY` from the target instance even when the process env has a different value.
- `tests/test_send_telegram.py`: canonical Telegram sender prefers instance token/chat, while explicit chat override still wins.
- `tests/watchdog/test_supervisor.py`: Python watchdog v2 child launch receives safe instance secrets but not runtime-control keys from `.env`.
- `tests/gateway/test_channels.py`: voice/Telegram call sites pass `instance_dir`.

Manual smoke:

1. Start one gateway with `env -i HOME=... PATH=... jc-gateway --instance-dir <instance> start`.
2. Send a Telegram voice note.
3. Verify ASR succeeds without an exported `DASHSCOPE_API_KEY`.
4. Start a second instance under the same user with different Telegram/DashScope keys and verify it uses its own `.env`.
