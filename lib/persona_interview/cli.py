"""CLI for jc persona — interview, gaps, doctor.

Subcommands:

  interview [--include-populated] [--redo <slot_id>] [--instance-dir <path>]
      Walk gaps, prompt, validate, splice.

  gaps [--json]
      Read-only list of unfilled / missing slots.

  doctor
      Schema-alignment check between this instance and the framework's
      current questions.yaml.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .engine import (
    InterviewResult,
    Prompter,
    interview,
)
from .gaps import GapState, find_gaps, summarize
from .questions import Prompt, QuestionsBank, Slot, load_questions


# ---------------------------------------------------------------------------
# Terminal Prompter — real interactive I/O
# ---------------------------------------------------------------------------

class TerminalPrompter:
    """Stdin/stdout-backed Prompter implementation."""

    def __init__(self, *, color: bool = True):
        self.color = color and sys.stdout.isatty()

    # ----- output helpers -----

    def _bold(self, s: str) -> str:
        return f"\033[1m{s}\033[0m" if self.color else s

    def _dim(self, s: str) -> str:
        return f"\033[2m{s}\033[0m" if self.color else s

    def _cyan(self, s: str) -> str:
        return f"\033[36m{s}\033[0m" if self.color else s

    def announce_phase(self, phase: str, detail: str = "") -> None:
        line = f"\n══ Phase: {self._bold(phase)} ══"
        if detail:
            line += f"  {self._dim(detail)}"
        print(line)

    def announce_slot(self, slot, gap, position) -> None:
        i, total = position
        state = gap.state.value
        print()
        print(self._cyan(f"[{i}/{total}] {slot.slot_id}  ({state}, {slot.kind})"))
        print(self._dim(f"  → {slot.target_file} :: {slot.target_section}"))

    def show_message(self, message: str) -> None:
        print(message)

    # ----- input helpers -----

    def ask_macro(self, macro_key: str, hint: str = "") -> str:
        prompt = f"  {self._bold(macro_key)} — {hint}\n  > "
        return input(prompt)

    def ask_prompt(self, prompt: Prompt, slot: Slot) -> str | None:
        # Header.
        print(f"\n  {self._bold(prompt.id)}: {prompt.text}")
        if prompt.help:
            print(self._dim(f"    ({prompt.help})"))
        if prompt.examples:
            print(self._dim("    Examples:"))
            for ex in prompt.examples[:3]:
                # Show only the first line of each example to stay tidy.
                first = ex.splitlines()[0] if ex else ""
                print(self._dim(f"      • {first[:120]}"))

        if prompt.kind == "choice":
            return self._ask_choice(prompt)
        if prompt.kind == "list":
            return self._ask_list()
        if prompt.kind == "longtext":
            return self._ask_longtext()
        return self._ask_text()

    def confirm_overwrite(self, slot, current_body: str) -> str:
        print(f"\n  Slot {self._bold(slot.slot_id)} is already populated:")
        snippet = current_body.strip().splitlines()
        for line in snippet[:6]:
            print(self._dim(f"    │ {line}"))
        if len(snippet) > 6:
            print(self._dim(f"    │ … ({len(snippet) - 6} more lines)"))
        while True:
            ans = input("  [k]eep / [r]eplace / [s]kip ? ").strip().lower()
            if ans in ("k", "keep"):
                return "keep"
            if ans in ("r", "replace"):
                return "replace"
            if ans in ("s", "skip"):
                return "skip"
            print("  Invalid; choose k / r / s.")

    # ----- input mode helpers -----

    def _ask_text(self) -> str | None:
        ans = input("  > ")
        return ans if ans != "" else None

    def _ask_longtext(self) -> str | None:
        print(self._dim("    (multi-line; end with a blank line)"))
        lines: list[str] = []
        while True:
            try:
                line = input("  > ")
            except EOFError:
                break
            if line == "" and lines:
                break
            if line == "" and not lines:
                # First-line empty: treat as skip.
                return None
            lines.append(line)
        return "\n".join(lines) if lines else None

    def _ask_list(self) -> str | None:
        print(self._dim("    (one item per line; end with a blank line)"))
        lines: list[str] = []
        while True:
            try:
                line = input("  > ")
            except EOFError:
                break
            if line == "":
                break
            lines.append(line)
        return "\n".join(lines) if lines else None

    def _ask_choice(self, prompt: Prompt) -> str | None:
        choices = prompt.choices
        choice_str = " / ".join(choices)
        while True:
            ans = input(f"  ({choice_str}) > ").strip()
            if ans == "":
                return None
            if ans in choices:
                return ans
            print(f"  Invalid; pick one of: {choice_str}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="jc persona — interview engine")
    parser.add_argument(
        "--instance-dir", type=Path, default=None,
        help="Instance directory (defaults via JC_INSTANCE_DIR / .jc walk-up / cwd)",
    )
    parser.add_argument(
        "--questions-bank", type=Path, default=None,
        help="Path to questions.yaml (defaults to framework's templates/persona-interview/questions.yaml)",
    )
    sub = parser.add_subparsers(dest="command", help="Subcommand")

    p_interview = sub.add_parser("interview", help="Walk gaps and fill slots")
    p_interview.add_argument(
        "--include-populated", action="store_true",
        help="Walk every slot, prompting keep/replace/skip on populated ones.",
    )
    p_interview.add_argument(
        "--redo", metavar="SLOT_ID", default=None,
        help="Re-ask exactly one slot (force replace).",
    )

    p_gaps = sub.add_parser("gaps", help="List unfilled / missing slots")
    p_gaps.add_argument("--json", action="store_true", help="Machine-readable output")

    sub.add_parser("doctor", help="Verify alignment with framework template")

    args = parser.parse_args()
    instance_dir = _resolve_instance_dir(args.instance_dir)
    bank = _load_bank(args.questions_bank)

    if args.command == "interview":
        return _cmd_interview(instance_dir, bank, args.include_populated, args.redo)
    if args.command == "gaps":
        return _cmd_gaps(instance_dir, bank, args.json)
    if args.command == "doctor":
        return _cmd_doctor(instance_dir, bank)
    parser.print_help()
    return 1


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _cmd_interview(
    instance_dir: Path,
    bank: QuestionsBank,
    include_populated: bool,
    redo: str | None,
) -> int:
    prompter = TerminalPrompter()
    if redo:
        result = interview(
            instance_dir, bank, prompter,
            include_populated=True, only_slot_id=redo,
        )
    else:
        result = interview(
            instance_dir, bank, prompter,
            include_populated=include_populated,
        )
    _print_summary(result)
    return 0 if not result.failed else 1


def _cmd_gaps(instance_dir: Path, bank: QuestionsBank, as_json: bool) -> int:
    gaps = find_gaps(instance_dir, bank)
    if as_json:
        def _prompt_dict(p: Prompt) -> dict:
            d = {
                "id": p.id,
                "text": p.text,
                "kind": p.kind,
            }
            if p.choices:
                d["choices"] = p.choices
            if p.examples:
                d["examples"] = p.examples
            if p.help:
                d["help"] = p.help
            if p.validation.required or p.validation.min_chars or p.validation.max_chars or p.validation.pattern:
                d["validation"] = {
                    "required": p.validation.required,
                }
                if p.validation.min_chars is not None:
                    d["validation"]["min_chars"] = p.validation.min_chars
                if p.validation.max_chars is not None:
                    d["validation"]["max_chars"] = p.validation.max_chars
                if p.validation.pattern is not None:
                    d["validation"]["pattern"] = p.validation.pattern
            if p.depends_on:
                d["depends_on"] = {
                    "prompt_id": p.depends_on.prompt_id,
                    "op": p.depends_on.op,
                    "value": p.depends_on.value,
                }
            return d

        def _slot_dict(s: Slot) -> dict:
            d = {
                "slot_id": s.slot_id,
                "target_file": s.target_file,
                "target_section": s.target_section,
                "kind": s.kind,
                "status": s.status,
                "prompts": [_prompt_dict(p) for p in s.prompts],
            }
            if s.composition:
                d["composition"] = {
                    "template": s.composition.template,
                }
                if s.composition.when:
                    d["composition"]["when"] = {
                        "prompt_id": s.composition.when.prompt_id,
                        "op": s.composition.when.op,
                        "value": s.composition.when.value,
                    }
                if s.composition.fallback:
                    d["composition"]["fallback"] = s.composition.fallback
            return d

        print(json.dumps({
            "gaps": [
                {
                    "slot_id": g.slot.slot_id,
                    "state": g.state.value,
                    "slot": _slot_dict(g.slot),
                }
                for g in gaps
            ],
            "summary": summarize(gaps),
        }, indent=2))
        return 0
    summary = summarize(gaps)
    print(f"Total: {summary['total']}  "
          f"missing: {summary['missing']}  "
          f"unfilled: {summary['unfilled']}  "
          f"populated: {summary['populated']}")
    if not gaps:
        print("(none)")
        return 0
    print()
    by_file: dict[str, list] = {}
    for g in gaps:
        by_file.setdefault(g.slot.target_file, []).append(g)
    for file_rel, items in sorted(by_file.items()):
        print(f"== {file_rel}")
        for g in items:
            print(f"  [{g.state.value:9}] {g.slot.slot_id}")
    return 0


def _cmd_doctor(instance_dir: Path, bank: QuestionsBank) -> int:
    # Phase 5 MVP: count gaps, surface schema mismatches.
    gaps = find_gaps(instance_dir, bank, include_populated=True)
    summary = summarize(gaps)
    print(f"Slots in framework bank: {len(bank.slots)}")
    print(f"  missing in instance:   {summary['missing']}")
    print(f"  unfilled placeholders: {summary['unfilled']}")
    print(f"  populated:             {summary['populated']}")
    if summary["missing"]:
        print("\n  Hint: run `jc persona interview` to fill missing/unfilled slots.")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_instance_dir(arg: Path | None) -> Path:
    if arg is not None:
        return arg.resolve()
    env = os.environ.get("JC_INSTANCE_DIR")
    if env:
        return Path(env).resolve()
    # Walk up for .jc marker.
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        if (parent / ".jc").exists():
            return parent
    return cwd


def _load_bank(arg: Path | None) -> QuestionsBank:
    if arg is None:
        # Framework root = parent of lib/.
        framework_root = Path(__file__).resolve().parent.parent.parent
        arg = framework_root / "templates" / "persona-interview" / "questions.yaml"
    return load_questions(arg)


def _print_summary(result: InterviewResult) -> None:
    print()
    print("══ Summary ══")
    print(f"  Macros bound: {len(result.macros_bound)}  "
          f"({', '.join(sorted(result.macros_bound)) or 'none'})")
    print(f"  Slots filled:  {len(result.filled)}")
    print(f"  Slots skipped: {len(result.skipped)}")
    if result.failed:
        print(f"  Slots FAILED:  {len(result.failed)}")
        for slot_id, err in result.failed:
            print(f"    - {slot_id}: {err}")
    if result.audit_log_path:
        print(f"  Audit log:    {result.audit_log_path}")


if __name__ == "__main__":
    sys.exit(main())
