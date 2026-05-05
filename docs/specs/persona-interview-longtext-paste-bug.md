# Persona Interview Longtext Paste Corruption

Status: implemented in `2026.05.05.2`
Reporter: Florian Berger / BNESIM ops, 2026-05-02
Component: `lib/persona_interview/`
Severity: High

## Summary

Before `2026.05.05.2`, `jc persona interview` ended longtext input on the
first blank line after any content. Terminal paste cannot distinguish a
paragraph break from a deliberate blank-line terminator, so pasted
multi-paragraph answers could be split across later prompts in the same slot.
For structured multi-prompt slots, this could silently splice misaligned
content into L1 memory files.

## Reproduction

1. Run `jc persona interview --redo identity.public-character`.
2. At the first longtext prompt, paste content with an internal blank line:

   ```text
   Personal Details

   Full name: Florian Berger
   ```

3. The old prompt reader captured only `Personal Details`.
4. Remaining pasted lines were consumed as answers to later prompts.
5. The composed body was spliced immediately with no preview step.

## Fix

Two independent safety changes shipped together:

- Longtext input now terminates only when the operator types `EOF` on its own
  line. Blank lines are preserved as answer content.
- Multi-prompt slots now preview the composed body and require an explicit
  `apply`, `re-do`, or `abort` decision before splicing.

## Tests

Covered cases:

- pasted longtext with internal blank lines is captured as one answer;
- first-line `EOF` skips a longtext prompt;
- empty body before `EOF` skips;
- multi-prompt preview happens before splice;
- `re-do` restarts the prompt loop without intermediate splice;
- `abort` leaves the file unchanged and records `slot_skipped` in the audit log.
