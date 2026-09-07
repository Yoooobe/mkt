# Skill digest: email-creative-builder

_Fonte: `/Users/genautech/mkt/.cursor/skills/email-creative-builder/SKILL.md` · truncado a 2800 chars para o contexto de campanha._

# Email Creative Builder

Writes and iterates a single email creative — subject-line variants + preheader, body copy, one clear CTA, and a plain-text alternate — each message-matched to the destination landing page and traced to approved claim wording. This is the build skill that produces the SEND **E/D** unit (the email analog of paid's ad-creative-builder). It does not score the email, run the D1 veto, or compute the EQS — that is `email-quality-auditor` — and it does not design the multi-step flow (`email-sequence-designer`) or the test (`send-experiment-designer`).

**Scope guard**: this skill builds the creative unit + message-match + claim flags only. It drafts subject-line variants for the creative but does **not** pre-score or rank them (spam-trigger flags, length/truncation, emoji-count, inbox-preview render) — that is [subject-line-lab](../subject-line-lab/SKILL.md). It scores no SEND dimension, runs no veto, and does not compute the profile-weighted EQS — [email-quality-auditor](../../deliver/email-quality-auditor/SKILL.md) owns all four vetoes (S1/S2/N1/D1) and the EQS rollup.

## Quick Start

```
Write 5 subject lines + preheader + body + CTA for [offer], destination [URL], promo mode
```

```
Draft a cold-outbound email to [persona] for [offer]; personalize on [signal]; destination [URL]
```

```
Iterate these losing subject lines: [paste]. Keep the winners, replace the rest, hold message-match to [URL].
```

## Skill Contract

**Expected output**: one ready-to-send email creative — 3-5 subject-line variants, a preheader, structured body copy, a single primary CTA, and a plain-text alternate — with a per-claim message-match note to the destination URL and any `[needs source]` flags, plus the standard handoff summary for `memory/email/email-creative-builder/`.

- **Reads**: the offer/topic, destination, email mode, audience/lifecycle stage, existing copy, `memory/projections/narrative.json`, `memory/projections/claims.json`, and the live consent/suppression state required by the selected flow.
- **Writes**: a user-facing email creative and, with permission, a WARM artifact; unresolved claims become authorized proposal events, never direct ledger edits.
- **Done when**: subject/preheader fit declared render limits, the body has one primary CTA, plain text exists, claims/disclosures are accepted and context-valid or visibly blocked, destination message-match holds, and the Narrative/claims dependency tuple is present.
- **Primary next skill**: [send-experiment-designer](../../deliver/send-experiment-designer/SKILL.md) — design the A/B / send-time test across the subject variants; or [email-quality-auditor](../../deliver/email-quality-auditor/SKILL.md) to score the unit and run the D1 claim veto.

### Handoff Summary

> Emit the standard s

…[digest truncado — skill completo em ~/.agents/skills/email-creative-builder]