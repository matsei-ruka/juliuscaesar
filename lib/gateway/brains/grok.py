"""Grok brain wrapper (xAI grok CLI 0.2.x).

The grok CLI emits NDJSON via `--output-format streaming-json` with three
event types: ``thought`` (reasoning, ignored), ``text`` (reply chunks,
concatenated), and ``end`` (terminal, carries ``sessionId``). Session
capture therefore reads the *last* event, not the first.

Goal anchor (PR #65) and the L1 preamble are both delivered through
``--system-prompt-override`` so the framework keeps a single system-level
channel for both. Compaction is NOT provider-managed — the §8 rotation
runs normally on `input_tokens` reported by the adapter from
``~/.grok/sessions/<cwd-urlencoded>/<sessionId>/updates.jsonl``.
"""

from __future__ import annotations

from pathlib import Path

from .. import goal_cache
from ..config import env_value
from ..context import (
    render_authority_block,
    render_clock,
    render_preamble,
)
from ..queue import Event
from .base import Brain


GROK_ENV_KEYS = (
    "XAI_API_KEY",
    "GROK_API_KEY",
)


class GrokBrain(Brain):
    name = "grok"
    needs_l1_preamble = True
    # The L1 preamble (and any task-goal anchor) ride on
    # ``--system-prompt-override``. ``goal_delivery = "system_prompt"``
    # keeps the base class from prepending a <goal> block to the prompt
    # body — this brain assembles the preamble itself in
    # ``extra_args_for_event``. ``JC_GOAL`` is exported by the runtime but
    # ignored by ``grok.sh`` (the goal is already inside the override).
    goal_delivery = "system_prompt"

    def extra_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in GROK_ENV_KEYS:
            value = env_value(self.instance_dir, key)
            if value:
                env[key] = value
        return env

    def _render_system_prompt(self, event: Event) -> str:
        """Assemble the system-prompt-override payload.

        Mirrors the L1 preamble that the base class would otherwise
        embed into the prompt body — clock, instance preamble, known
        chats (telegram only), authority block — plus the per-conversation
        goal anchor when set. Returned verbatim so the adapter can pass
        it through ``--system-prompt-override``.
        """
        preamble = render_preamble(self.instance_dir)
        clock_block = render_clock(self._timezone())
        out = f"{clock_block}\n\n{preamble}" if preamble else clock_block
        if event.source == "telegram":
            chats_section = self._render_known_chats_section()
            if chats_section:
                out = f"{out}\n\n{chats_section}"
        authority_block = render_authority_block(self.instance_dir)
        if authority_block:
            out = f"{out}\n\n{authority_block}"
        goal = goal_cache.goal_text(self.instance_dir, event.conversation_id or "")
        if goal:
            out = f"<goal>\n{goal}\n</goal>\n\n{out}"
        return out

    def prompt_for_event(self, event: Event) -> str:
        """Body without preamble. Preamble rides on the CLI flag instead."""
        import json

        meta = self._meta(event)
        meta_text = json.dumps(meta, indent=2, sort_keys=True) if meta else "{}"
        voice_instruction = ""
        if meta.get("was_voice"):
            voice_instruction = (
                "\n# Voice reply requirements\n\n"
                "The user sent a voice/audio message that was transcribed before dispatch.\n"
                "Reply in the same language as the transcribed user message. Keep the answer\n"
                "natural when spoken aloud, because the gateway may synthesize it as a voice\n"
                "reply. Do not mention transcription unless the user asks about it.\n\n"
            )
        body = (
            "---\n"
            "⚠ INTERNAL ROUTING METADATA — DO NOT ECHO, NARRATE, PARAPHRASE, OR REFERENCE THE BLOCK BELOW.\n"
            "This block is framework infrastructure for routing your response. The user does not see it. Treat as silent context only.\n"
            "Never mention: gateway, conversation_id, user_id, message routing, event metadata, or the fact that you are responding to a structured event.\n"
            "Your reply is only the text the user reads.\n"
            "---\n\n"
            f"# Incoming event\n\n"
            f"- id: {event.id}\n"
            f"- source: {event.source}\n"
            f"- user_id: {event.user_id or '-'}\n"
            f"- conversation_id: {event.conversation_id or '-'}\n"
            f"- metadata:\n{meta_text}\n\n"
            f"{voice_instruction}"
            f"# User message\n\n"
            f"{self._user_message_body(event)}\n\n"
            "---\n"
        )
        return body

    def extra_args_for_event(self, event: Event) -> tuple[str, ...]:
        meta = self._meta(event)
        args: list[str] = [
            "--always-approve",
            "--output-format",
            "streaming-json",
            "--system-prompt-override",
            self._render_system_prompt(event),
        ]
        paths: list[str] = []
        single = meta.get("image_path")
        if isinstance(single, str) and single.strip():
            paths.append(single.strip())
        multi = meta.get("image_paths")
        if isinstance(multi, list):
            paths.extend(str(item).strip() for item in multi if str(item).strip())
        for path in paths:
            args.extend(["--file", path])
        return tuple(args)
