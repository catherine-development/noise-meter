# Independent review prompt — noise-meter production fixes

Paste everything below this line into the reviewing agent.

---

You are conducting an independent, adversarial code review. The work you are
reviewing was planned and implemented by a different AI system (Claude), so do
not extend it courtesy: your value is in finding what it got wrong, glossed
over, or asserted without evidence. Treat every claim in the documentation as
unverified until you have confirmed it in the code or by running it.

## The system

A Flask + SQLite web application for a Norsonic NOR140 Class 1 sound level
meter. It decodes the meter's proprietary binary SD-card format (GLOB/PROF
files), stores runs and sessions, computes acoustic statistics, produces
BS 4142 / Noise Act / Control of Noise at Work assessment data, CSV and
Nortfr-parity xlsx exports, and AI-drafted reports. It runs on two
dual-redundant Raspberry Pis at different sites which replicate peer-to-peer
over HTTPS with a shared API key; both accept uploads and edits. The output is
**evidence submitted to UK local authorities**, so a plausible-but-wrong number
is the worst possible failure mode — worse than a crash or a blank.

## Scope of the review

Repository: `github.com/catherine-development/noise-meter`, branch `master`.
Review the range `ccfce89..a6083ff` — 50 commits, 33 files, +7,565/−921. It
was produced in ten work packages over two days in response to a production
review. The narrative record is in the repo:

- `docs/fix-plan-2026-08-19.md` — the plan, per-package outcome notes,
  decisions D1–D4, deploy records. Read this first.
- `docs/validation-brief.md` — the pre-existing verification methodology for
  the binary decoding (its claims C1–C10 were NOT supposed to change).
- `docs/design-note-sessions-and-sync.md` — the identity/replication design
  the later packages implemented.
- `NOR140_handoff.md` — the binary-format reverse-engineering record
  (background; note the instrument serial is 1402755, not the 6899108 that
  appears in a Nortfr filename example).

What changed, in brief: run identity re-keyed from position to
`(session, source_file)`; sessions re-keyed from `date` to
`(date, instrument_serial)`; CSRF + fail-closed auth + rate limiting; run-modal
statistics relabelled to the quantities actually shown; duration-weighted
session LAeq; BS 4142 day/night periods; generated reports pinned to a run and
rendered from an input snapshot; weather keyed to the session and replicated;
uid-keyed last-writer-wins replication with tombstones and a conflict ledger
for all hand-entered data; additive user sync; gunicorn/WAL/backups/deploy
hardening.

## How to run it

```bash
python3 -m venv /tmp/nm-venv
/tmp/nm-venv/bin/pip install -q flask flask-limiter openpyxl
/tmp/nm-venv/bin/python3 test_modules.py
```

Expected: `All 673 checks passed.` The suite needs the `MEAS118/` SD-card
archive (527 GLOB/PROF pairs) extracted in the repo root; it is not committed.
If you do not have it, say so prominently — most of the suite and any
binary-level verification is then unavailable to you, and your review should
state which claims you consequently could not check.

## What to attack, in priority order

1. **The upsert and renumbering path** (`noise_db.import_sessions`,
   `_renumber_session_runs`). Construct hostile import sequences: partial
   uploads, re-imports with runs missing at the front/middle, same payload
   twice, interleaved peer pushes, two serials on one date, legacy payloads
   with no `source_file` or `serial`. Does any sequence overwrite a stored
   run's data, orphan an assessment link, or leave `run_number` inconsistent
   with the stored order?
2. **The LWW replication layer** (`sync_db.py`). The claim is: uid-keyed
   apply, newer `updated_at` wins, stale writes land in `sync_conflicts`,
   deletes tombstone and never resurrect, id-keyed events from an old peer
   still apply, and the deterministic uuid5 backfill unifies rather than twins.
   Look for: clock skew between Pis (LWW uses wall-clock text timestamps —
   what breaks if one Pi's clock is behind?), the NULL-`updated_at`-is-oldest
   convention, delete-vs-edit races, stub-parent creation, and whether any
   apply path still keys on a local integer id.
3. **Acoustic correctness.** Verify the duration-weighted session LAeq
   formula and its consistency across `noise_parser`, `noise_db`, and
   `reports`; the percentile conventions (LAF-based pooled percentiles vs
   LAeq,1s series); `round_half_up` usage; the BS 4142 day/night boundaries
   and `spans_boundary`; and that every label in the run modal and the CSV
   headers names the quantity actually computed (this class of bug occurred
   before — the fix is asserted by static guards in the suite; check the
   guards themselves for gaps).
4. **The decode boundary invariants.** `nor140_format.py` was meant to be
   untouched in spirit: one implementation of each decode, the −20 dB no-data
   sentinel dropped only at the boundary, `prof_record_size()` failing closed.
   Confirm nothing in the new code re-decodes, re-rounds, or resurrects the
   sentinel (grep for `struct.unpack` outside `nor140_format.py`, for `-20`
   comparisons, and for Python `round(` on dB values).
5. **Security posture.** CSRF: the token check is a `before_request` hook with
   an API-key bypass — find a state-changing route that escapes it (blueprints,
   the fetch-wrapper approach in `templates/_csrf.html`, JSON routes, the 413
   handler). Auth: `check_startup_security()` fail-closed logic and the
   `ALLOW_UNAUTHENTICATED` escape. The API key comparison paths. Rate-limiter
   coverage. Also: the peer-sync surface is authenticated by one shared key —
   assess what a compromised key allows (including `/admin/purge-before` and
   the users-sync endpoint, which carries names/emails/phones).
6. **Migrations against real-world databases.** `_migrate` now performs
   several table rebuilds (`sessions`, `deleted_sessions`, `weather`,
   `assessment_runs`) and deterministic uid backfills. Check idempotency,
   partial-failure behaviour (what state is left if it dies mid-rebuild?),
   foreign-key preservation, and ordering assumptions between the WP1→WP10
   migrations when applied in one jump from a pre-WP1 database versus
   incrementally.
7. **The test suite itself.** 673 checks sounds thorough; is it? Look for
   checks that assert the implementation rather than the requirement, static
   guards that a plausible regression would slip past, monkeypatching that
   neuters the thing being tested, and paths with no coverage at all (the
   JS in `templates/index.html` is ~2,000 lines with only grep-based guards).
8. **Operational.** gunicorn 1-worker/4-thread with SQLite WAL: any request
   path that holds a connection across a blocking call? The in-memory rate
   limiter and CSRF session interplay behind Cloudflare. `backup_db.py`
   correctness under WAL. The deploy script's `git ls-files` filter. The
   15-minute full-sync payload growth as reports accumulate (it ships every
   report's full sections + snapshot every tick — when does that become a
   problem?).

## Ground rules

- Read code before documentation; where they disagree, the code is the fact
  and the disagreement is a finding.
- Reproduce before reporting: for each claimed defect, give a concrete
  input/sequence and the wrong output, ideally as a runnable snippet against
  a scratch database (`NOISE_DB_PATH=/tmp/x.db`).
- Do not touch the live Raspberry Pis, do not push, and treat `.env` values
  as out of scope.
- Severity scale: **Critical** = wrong number or lost data in an evidence
  path; **High** = data loss/corruption possible under realistic sequences,
  or an auth/CSRF bypass; **Medium** = wrong behaviour outside evidence
  paths, or a latent trap; **Low** = hygiene.

## Deliverable

A written report: (1) an executive verdict — is this deployable-quality work,
and what is the single riskiest remaining thing; (2) findings ordered by
severity, each with file:line, a reproduction, and a suggested fix direction;
(3) a list of claims in `docs/fix-plan-2026-08-19.md` you checked and whether
each held; (4) what you could not verify and why. Do not pad: if an area is
sound, one sentence saying so and what you tried is worth more than a page of
description.
