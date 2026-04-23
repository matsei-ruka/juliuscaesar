# Voice

DashScope Qwen TTS + ASR. The runner lives in the JuliusCaesar framework.

## First-time setup

1. Set `DASHSCOPE_API_KEY` in the instance's `.env` (at the repo root — shared with other skills).
2. Enroll a voice sample:

   ```bash
   jc-voice enroll path/to/sample.mp3 --name <name>
   ```

   This writes `voice/references/voice.json` with the cloned voice id.
   Audio requirements: 10–20s recommended (≤60s), mono, ≥24kHz, clean speech.

## Usage

```bash
jc-voice speak "text to speak"                         # → voice/tmp/out.ogg
jc-voice speak "text" --out /tmp/custom.ogg
jc-voice transcribe path/to/audio.ogg                  # → stdout
jc-voice transcribe audio.ogg --out transcript.txt
jc-voice list-voices                                   # all enrolled voices on this DashScope account
```

## Files

- `references/voice.json` — your cloned voice id (versioned in git; not a secret).
- `tmp/` — scratch dir for synthesized output. Gitignored.
