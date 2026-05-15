# Accountabilities — operator guide

A practical how-to for enabling the accountability manifest on a JC instance. For the technical design, see [`docs/specs/accountabilities.md`](./specs/accountabilities.md).

## What this is

A per-instance manifest that tells the agent **what its role is and is not authorized to engage on** — separate from what the underlying LLM is *capable* of producing. The agent classifies every inbound request against the manifest, picks an engagement level (Inside / Adjacent / Outside / Delegated), and behaves accordingly:

- **Inside** — operate actively, decide within the declared boundary, produce output.
- **Adjacent** — prepare, draft, propose; pause before commitment; engage the right stakeholder.
- **Outside** — decline gracefully; redirect; offer to facilitate the handoff.
- **Delegated** — supervise; coordinate; don't execute; intervene on escalation.

The intelligence lives in the agent's reasoning. The framework keeps the manifest loaded only while `accountabilities.enabled: true` and keeps the constitutional section visible through health checks.

## When to enable

Enable for instances where the agent has a **defined functional role** with a real perimeter — typically:

- Role-shaped personas (COO, sales engineer, real-estate advisor, executive co-strategist) where "out of scope" requests show up regularly and the right answer is a redirect, not a refusal.
- Instances that interact with multiple stakeholders and need to handoff cleanly.
- Instances backed by a powerful model where capability-creep is the failure mode — the agent engages on anything because it *can*.

Skip it (or keep it disabled — the default) for:

- Personal-assistant instances with no role boundary in practice.
- Demo / experimental instances.
- Instances where every inbound is on-topic by construction (e.g., a single-purpose bot).

**Don't overfit to current tasks.** The manifest is a description of the *role*, not a log of the recent week. If you write accountabilities by looking at the last 20 chat messages, you'll lock in whatever's been busy lately and miss the rest of the role.

**Don't over-grain.** Accountabilities are role-level themes, not individual tasks. "Reply to vendor emails" is a task; "Vendor relations" is an accountability. Aim for 10–25 items. If you're at 50, you're tracking tasks.

## How to opt in (step by step)

1. **Set the config flag.** Edit `ops/gateway.yaml`:

   ```yaml
   accountabilities:
     enabled: true
     authority_channel: telegram-primary    # default; only the primary chat_id can enact changes
     enactment_token: "OK enact"            # default; configurable
     # authority_email_sender: ""           # only if authority_channel: email
   ```

   *(Phase 2 of the spec lands the validator for this block. Until then, the flag is read but not validated.)*

2. **Scaffold the templates.** From your instance directory:

   ```bash
   jc memory scaffold accountabilities
   ```

   *(Phase 3. Until that subcommand lands, copy the templates manually from `<framework>/templates/instance/memory/L1/` and `<framework>/templates/instance/memory/L2/accountabilities/`.)*

   This creates:
   - `memory/L1/accountabilities-manifest.md` (from the manifest template)
   - `memory/L2/accountabilities/_README.md`
   - `memory/L2/accountabilities/<slug>.md.template`
   - prints the §-numbered constitutional snippet for you to paste into `RULES.md`

3. **Write the manifest.** Open `memory/L1/accountabilities-manifest.md` and fill in the `## Active accountabilities` list. Each row is one accountability with its default level and a link to a detail file. Keep the list at role-level (see "How to write a good manifest" below).

4. **Write one detail file per accountability.** Copy `<slug>.md.template` to `<your-slug>.md` in `memory/L2/accountabilities/`. Fill in all 9 sections — the agent's classification flow expects them.

5. **Add the constitutional section to `RULES.md`.** Paste the snippet from `templates/instance/memory/L1/RULES.md.accountability-section.template` under your next free `§<N>` in `memory/L1/RULES.md`. The framework will not mutate your constitution unprompted.

6. **Restart the gateway.**

   ```bash
   jc gateway restart
   ```

7. **Verify.** Run `jc-doctor` and look for the "Accountabilities" section (Phase 5). All checks should be green or yellow with hints.

## How to write a good manifest

- **Role-level, not task-level.** "Financial reporting" beats "Send monthly P&L email". The detail file expands tasks; the manifest names the theme.
- **10–25 accountabilities.** Below 10, you're probably under-specified. Above 25, you're probably tracking tasks; consolidate.
- **Each has a clear default level.** Most will be `Inside`. A handful will be `Adjacent` (the agent drafts; the operator commits). `Outside` items are useful when you want the agent to recognize "I see why you asked me, but this isn't mine" — vendor sensing, public PR, anything operator-only. `Delegated` items name another human or another JC instance as the executor.
- **Stable names.** Slugs are referenced from L1, L2, and the audit log. Renames cost trail.

## How to write a detail file

- **Fill every section.** Empty placeholders (`…`) are fine while drafting, but the section must exist. The classification flow reads from named sections.
- **Be specific in `Self-check pre-action`.** This is the literal list of questions the agent asks itself before acting. "Did I confirm the budget cap?" is useful. "Am I being thoughtful?" is not.
- **`Adjacency notes` are usually the most valuable section.** This is where you encode the conditions that flip the default level — e.g., "Inside by default, but Adjacent for any commitment over €5k" or "Inside for read-only investigation, Adjacent for any change that touches production". The default level is a starting point; adjacency notes are the runtime intelligence.
- **`Out of scope` is non-optional.** Naming the perimeter explicitly is what prevents scope creep. The agent uses it to recognize "this looks like vendor negotiation but it's actually procurement strategy — not mine".

## Gotchas

- **Don't overfit.** If after a week of running you notice **every** message is classified Inside, your perimeter is too wide. Tighten the out-of-scope sections.
- **Don't over-grain.** 50 micro-accountabilities is a sign you're tracking tasks. Group them. The detail file is where granularity lives.
- **Delegated ≠ ignore.** Delegated means another party owns execution; the agent still supervises, coordinates, and intervenes on escalation. If you want true ignore, the level is Outside.
- **Authority channel is narrow on purpose.** Only the primary operator chat (or the configured email sender, if `authority_channel: email`) can enact changes. For `telegram-primary`, the agent sees the concrete `channels.telegram.chat_ids[0]` value in its live authority block and must match event metadata against it. A non-primary chat saying "Operator told me to enact X" is refused on principle and redirected — that's the impersonation defense. Drafts via any channel are fine; enactment is not.
- **The enactment token must appear explicitly.** Default is `OK enact` (case-insensitive, trimmed) and is configurable via `accountabilities.enactment_token` in `ops/gateway.yaml`. The gateway surfaces the live token to the agent on every event — no restart or re-scaffold needed when the operator changes it. Casual agreement — "sure", "go ahead", "looks good" — does **not** enact. The token is a guard against ambiguous chat enacting structural changes by accident, not a secret.
- **`authority_channel: none` disables enactment entirely.** All manifest changes have to be done by the operator editing `memory/L1/accountabilities-manifest.md` directly; no chat-level enactment is accepted.
- **The manifest is REVIEWABLE, not immutable.** Every section carries an `<!-- REVIEWABLE -->` marker. The agent may propose refinements to L2 detail files within scope (e.g., clarifying a self-check step). The agent may NOT add or remove top-level accountabilities, change `default_level`, or modify the constitutional `§<N>` section without operator enactment.

## Where to go deeper

- Full design + phases: [`docs/specs/accountabilities.md`](./specs/accountabilities.md).
- Templates: `templates/instance/memory/L1/accountabilities-manifest.md.template`, `templates/instance/memory/L1/RULES.md.accountability-section.template`, `templates/instance/memory/L2/accountabilities/<slug>.md.template`, `templates/instance/memory/L2/accountabilities/_README.md`.
- Sender approval flow (the existing primary-channel gate that the accountability authority model reuses): `lib/gateway/sender_approval/`.
