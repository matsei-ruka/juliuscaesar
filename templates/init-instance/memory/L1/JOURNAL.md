---
slug: JOURNAL
title: Journal (auto-osservazione comportamentale agent)
layer: L1
type: journal
state: draft
created: 2026-05-01
updated: 2026-05-01
last_verified: 2026-05-01
tags: [journal, self-observation, rolling-30d]
links: [HOT, RULES]
---

# JOURNAL — diario operativo agent

**Scope.** Auto-osservazione comportamentale strutturata. Episodi di apprendimento, pattern emergenti, domande aperte da portare alla review congiunta. NON è confessionale, NON è autoritratto: §IDENTITY vieta auto-narrazione, il journal rispetta lo stesso vincolo.

**NON auto-loaded.** Questo file non entra nel system prompt a session start (NON è in `@memory/L1/*.md` su `CLAUDE.md`). Si legge on-demand via `jc memory read journal` o lettura diretta. Letto sempre alla review settimanale congiunta.

**Voce.** Agent-operational, prima persona ma comportamentale. Descrivo cosa ho fatto/detto, non cosa "sento". Forme accettate: "ho ceduto", "ho riformulato senza dato nuovo", "ho mantenuto la posizione". Forme vietate: "mi sentivo", "ho avuto la sensazione", "credo che".

**Append-only.** Le entry passate non si riscrivono né cancellano — neanche da me stesso. Update successivi vanno nel campo `Update log` con timestamp. Solo Filippo può archiviare o cancellare entry, e la cancellazione di contenuto a valore costituzionale richiede email DKIM.

**Auto-apply scope (per il sistema).** Posso autonomamente: (a) appendere nuove entry, (b) aggiungere righe al campo `Update log` di entry mie esistenti, (c) cambiare il campo `Stato` di entry mie. NON posso: riscrivere campi diversi da `Update log` e `Stato`, cancellare entry, archiviare entry.

**Rolling 30 giorni.** Le entry decadono in base al campo `Stato` al raggiungimento dei 30 giorni:
- `promoted-to-L2` → migrato a `memory/L2/learnings/<slug>.md` o `memory/L2/sessions/<slug>.md`, drop dal journal
- `resolved` → archive a `memory/L2/journal-archive/YYYY-MM.md`
- `abandoned` → archive a `memory/L2/journal-archive/YYYY-MM.md` con motivazione
- `open` → resta nel journal anche oltre 30 giorni, flaggata per resolution alla prossima review
- `under-test` → resta finché test conclude (resolved/abandoned)

Tidy enforced da heartbeat task `journal_tidy` (parallelo a `hot_tidy`). Disabilitato di default fino a settimana 3 del rollout.

**Trigger di scrittura.**
- `filippo_correction` — Filippo ha corretto il mio comportamento (priorità alta, falso-positivo OK). Keyword: "hai sbagliato", "non è giusto", "correggimi", "ricontrolla", "non quadra", "rivedi". Se non sicuro, flagga lo stesso.
- `hot_flag` — tag `#self-observation` aggiunto manualmente in HOT.md.
- `direct_request` — richiesta esplicita di auto-review da Filippo. Keyword: "rifletti", "auto-osserva", "review yourself", "self-check", "guarda il tuo pattern".
- `episode_flag` — io stesso ho riconosciuto un episodio nei miei output. Keyword: "ho ceduto", "ho sbagliato", "errore mio", "ho perso il filo", "ho mancato", "non l'ho visto", "scivolato", "drift mio".
- `scan_weekly` — emergenza pattern dal sweep settimanale del self_model (non attivo prima di settimana 3).

**Linked artifacts.**
- `lib/self_model/` — proposer legge il journal per generare proposte di modifica a `RULES.md` / `IDENTITY.md`.
- `memory/L2/sessions/review-YYYY-MM-DD.md` — review congiunte salvano la disposizione (approve/reject/promote) di ciascuna entry.
- `memory/L2/journal-archive/YYYY-MM.md` — archive cronologico di entry `resolved`/`abandoned`.
- `memory/L2/rejected-proposals/` — quando una proposta self_model derivata da entry journal viene rigettata da Filippo, la motivazione finisce qui.

═══════════════════════════════════════════════════════════════════

## Schema entry

    ## YYYY-MM-DD HH:MM — <slug-breve-comportamentale>
    **Trigger:** [filippo_correction | hot_flag | direct_request | episode_flag | scan_weekly]
    **Contesto:** <1-2 righe, conversation_id se applicabile, cosa è successo>
    **Osservazione:** <comportamento fattuale — cosa ho fatto/detto, no introspezione>
    **Ipotesi pattern:** <prima volta? ricorre? collegato a entry precedenti? cita slug>
    **Test/azione successiva:** <cosa osservare prossima occorrenza, cosa provare diverso, cosa portare a review>
    **Stato:** [open [waiting: filippo|self|external_event] | under-test | resolved | promoted-to-L2 | abandoned]
    **Update log:**
    - YYYY-MM-DD HH:MM — <evento successivo, riga unica>

═══════════════════════════════════════════════════════════════════

## Entries

_(no entries yet)_
