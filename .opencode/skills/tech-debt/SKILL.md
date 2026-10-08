---
name: tech-debt
description: Track in-repo tech debt in TECH_DEBT.md. Load before git-commit to mine shortcuts and evaluate triggers, or on demand to log/assess debt.
---

## What counts as tech debt here

Only work meeting BOTH bars, per `TECH_DEBT.md` header rules:

1. The proper fix is complex, open-ended, and off the critical path.
2. The proper fix costs MORE than the shortcut taken plus the
   instrumentation needed to validate its trigger.

Anything else is not debt: fix it now, drop it, or (for the select few
small risks that could manifestly misbehave) leave a 1-line
`NOTE(<trigger>): …` comment at the call site. A wishlist is not a tracker.

## Log (human-in-the-loop)

1. Propose entries from the current diff's shortcuts, one per candidate,
   each with Shortcut / Proper Solution / Trigger (true/false condition:
   observation, metric, or complexity threshold).
2. Present each for human approve/reject. Frivolous entries die here —
   default to rejecting anything that fails the two bars above.
3. Append approved entries to the QUEUED list in `TECH_DEBT.md`.

## Pre-commit step (one of the last steps before git-commit)

1. Mine `git diff HEAD` for new shortcuts taken in this cycle.
2. Evaluate every trigger in `TECH_DEBT.md` (QUEUED and PENDING) against
   current evidence; move fired QUEUED items to PENDING.
3. Report: new candidates (for approval), fired triggers, state changes.
4. Tracker updates commit together with the code, never standalone.

## Out-of-band use

Trigger assessment and debt logging may also run on demand outside review
or commit flow (`assess triggers now`, `log this as debt`). Same bars,
same approval gate.
