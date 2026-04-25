"""Voice channel — paired-channel ASR/TTS hooks.

The voice channel does not own any transport. It pairs with another I/O
channel (typically `telegram`) and rewrites inbound audio events into text
events, plus offers a TTS render for outbound. ASR/TTS are delegated to
DashScope helpers in `lib/voice/` when available.

For 0.3.0 the voice channel is an enabler: the paired channel is responsible
for receiving audio attachments and pushing them at the gateway under
`source="voice"` events with `meta.audio_path` set. The channel's `run` loop
is therefore a no-op heartbeat — it only logs that voice is enabled and
waits for the daemon to stop. The transcription path is implemented as a
helper used by the runtime when `event.source == "voice"`.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig
from .base import EnqueueFn, LogFn


VOICE_ROOT = Path(__file__).resolve().parents[2] / "voice"


class VoiceChannel:
    name = "voice"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        self.paired_with = str(getattr(cfg, "paired_with", "telegram") or "telegram")
        self.asr_provider = str(getattr(cfg, "asr_provider", "dashscope") or "dashscope")
        self.tts_provider = str(getattr(cfg, "tts_provider", "dashscope") or "dashscope")

    def ready(self) -> bool:
        return VOICE_ROOT.exists()

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None:
        if not self.ready():
            self.log("voice disabled: lib/voice missing")
            return
        self.log(
            f"voice channel ready (paired_with={self.paired_with}, "
            f"asr={self.asr_provider}, tts={self.tts_provider})"
        )
        while not should_stop():
            time.sleep(1)
        self.log("voice channel stopped")

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        # The paired transport sends; this hook lets the runtime ask voice to
        # render an audio reply alongside text. Returning the synthesized path
        # is enough for the paired channel to attach it.
        if not response.strip():
            return None
        try:
            return self._synthesize(response, meta)
        except Exception as exc:  # noqa: BLE001
            self.log(f"voice tts error: {exc}")
            return None

    def transcribe(self, audio_path: Path) -> str:
        """Best-effort ASR. Returns empty string on failure."""
        try:
            return self._asr(audio_path)
        except Exception as exc:  # noqa: BLE001
            self.log(f"voice asr error: {exc}")
            return ""

    # --- provider hooks -------------------------------------------------

    def _asr(self, audio_path: Path) -> str:
        if self.asr_provider == "dashscope":
            from importlib import import_module

            mod = import_module("voice.dashscope_asr") if (VOICE_ROOT / "dashscope_asr.py").exists() else None
            if mod is None or not hasattr(mod, "transcribe"):
                return ""
            return str(mod.transcribe(str(audio_path), instance_dir=str(self.instance_dir)))
        return ""

    def _synthesize(self, text: str, meta: dict[str, Any]) -> str | None:
        """Render `text` as Rachel-voice OGG/Opus.

        Loads voice/references/voice.json from the instance, calls
        voice.synth.synthesize, returns the OGG path. Returns None on
        any missing config / library error so the runtime can fall back
        to text-only delivery.
        """
        if self.tts_provider != "dashscope":
            return None

        ref_path = self.instance_dir / "voice" / "references" / "voice.json"
        if not ref_path.exists():
            self.log("voice tts skipped: voice/references/voice.json missing")
            return None
        try:
            ref = json.loads(ref_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.log(f"voice tts skipped: bad voice.json: {exc}")
            return None

        voice_id = ref.get("voice")
        target_model = ref.get("target_model")
        if not voice_id or not target_model:
            self.log("voice tts skipped: voice.json missing voice or target_model")
            return None

        out_dir = self.instance_dir / "state" / "voice" / "outbound"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{uuid.uuid4().hex}.ogg"

        from importlib import import_module

        synth = import_module("voice.synth")
        result = synth.synthesize(
            text,
            out_path,
            voice_id=str(voice_id),
            target_model=str(target_model),
        )
        return str(result) if result else None
