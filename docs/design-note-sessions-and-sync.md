# Design note — session identity and replicated ids (WP7)

**For:** Catherine Ives-Yim. **Status:** options and a recommendation; no code
changed. Addresses F3 (sessions keyed by calendar date) and F6 (hand-entered
rows replicated by autoincrement id) from the 2026-08-19 review. Everything
below was read off `production-fixes` at `9c55272`; WP1 (run upsert on
`(session_id, source_file)`) is assumed to land first and is taken as given.

Both findings are latent today: one meter, two Pis, 11 runs each on
2026-08-12/13. Neither has bitten. The point of this note is to decide the
shape *before* a second NOR140 or a third Pi makes it bite silently.

---

## 1. The identity model as it stands

| Thing | Identity | Where it is enforced |
| --- | --- | --- |
| Session | calendar date, `sessions.date TEXT UNIQUE` | `noise_db.init_db` |
| Run | `(session_id, source_file)` where `source_file` is the `PROJnnnn` folder (restarts at `PROJ0001` every date); `run_number` is a positional index assigned at import | `idx_runs_stable` (partial unique, `_migrate`); `UNIQUE(session_id, run_number)` still exists |
| Assessment → run link | `assessment_runs(session_date TEXT, source_file TEXT)`, `run_number` as legacy fallback | `idx_assessment_runs_stable` |
| Generated report → run | `generated_reports(session_date, run_number)` — positional; WP3 adds `source_file` | none |
| Run tag | `runs.location_tag`, replicated as `(session_date, run_number, location_tag)` | `sync_db.get_full_sync_payload` |
| Weather | `weather.date PRIMARY KEY` — one row per date although it is fetched for the *session's* lat/lng | `_migrate` |
| Session deletion | `deleted_sessions.date PRIMARY KEY` tombstone | `_migrate` |
| Assessments, locations, links | SQLite `INTEGER PRIMARY KEY` (no `AUTOINCREMENT`, so new id = max(id)+1), replicated verbatim | `sync_db.apply_full_sync`, `apply_sync_event` |
| Report templates, generated reports | local integer ids; **not replicated at all** (`reports.py` never calls `sync_event_to_peer`; `get_full_sync_payload` omits both tables) | — |
| Instrument | one app-wide string, `app_settings['instrument_serial']`, used only to name xlsx exports | `noise_app.save_instrument_settings`, `export_nor140` |

The instrument is not recoverable from the SD card: `NOR140_handoff.md` §"Filename"
records that the serial `6899108` appears in none of GLOB, PROF, DIRFILE or
STP; DIRFILE carries only project number and start timestamp. `MEAS118/` is the
meter's fixed measurement directory, not (as far as we know) an instrument id.
So whatever key we choose, the serial has to be supplied from outside the data.

### 1.1 Every place that assumes one session per date

Schema: `noise_db.init_db` (`sessions.date UNIQUE`), `noise_db._migrate`
(`weather.date PK`, `deleted_sessions.date PK`, `assessment_runs.session_date`,
`generated_reports.session_date`).

Import/replication: `noise_parser.parse_zip` (groups pairs `by_date`, emits `d`;
two meters' ZIPs collapse to one `d`), `noise_db.import_sessions`
(`ON CONFLICT(date)`, `SELECT id FROM sessions WHERE date=?`, tombstone clear by
date), `noise_db.get_sessions_since` / `get_sessions_export_format` /
`get_all_sessions_json` (payload keyed `d`), `noise_db.get_existing_dates`,
`get_existing_run_starts` (dedup map `date → {start_time}` used by
`noise_app.do_upload`), `noise_db.delete_session`, `_record_tombstones`,
`purge_sessions_before`, `recompute_session_aggregates(dates)`,
`sync_db._apply_tombstones`, `sync_db.get_full_sync_payload` (`sessions_meta`
by `date`, `run_tags` by `(session_date, run_number)`), `sync_db.apply_full_sync`
(`UPDATE sessions … WHERE date=:date`), `sync_db.apply_sync_event`
(`session_meta`, `run_tag`, `session/delete`), `sync_db.get_import_log`,
`import_sdcard.latest_date_on_pi` (push filter `s['d'] >= latest`),
`sync_peer.py` (watermark is `imported_at`, date-agnostic — fine).

Reads: `noise_db.get_full_run_row(date, run_number)`, `get_session_prof_lafspl(date, …)`,
`get_run_prof_laeq(date, run_number)`, `update_run_location_tag(date, run_number, tag)`,
`update_session_metadata(date, …)`, `save_weather/get_weather(date)`.

Assessments: `assessments_db.assign_runs` (`JOIN sessions s ON s.date=?`),
`get_assessment_detail`, `get_all_runs_for_assessment`,
`prepare_assessment_report_data` (all `JOIN sessions s ON s.date = ar.session_date`),
`list_assessments` (`MIN/MAX(ar.session_date)`), `get_assessment_runs_by_pairs`.

Reports: `reports._prepare_session_for_report(date, run_number)`,
`reports.api_generate_report` (`session_date` in the request body),
`reports_db.save_generated_report(session_date, …)`.

Routes: `noise_app` `/session/<date>/edit`, `/session/<date>/run/<n>/tag`,
`/session/<date>/run/<n>/export/nor140/<type>`, `/session/<date>/delete`,
`/session/<date>/fetch-weather`, `/api/existing-dates`, `export_sessions_csv`.

UI: `templates/index.html` (`DATA.sessions.find(s => s.d === date)` ×10,
`selectedDate`, `?date=` deep link, `TODAY_DATE`, run keys `s.d + '|' + n`),
`templates/assessments.html` (run key `session_date|run_number|source_file`),
`templates/reports.html` (`session_date` selector), `manage.html` session list.

That is roughly forty sites. Any change to the session key touches most of them,
which is the main argument for choosing the *smallest* key change that still
distinguishes instruments.

### 1.2 What actually goes wrong today

*Second meter, same date.* Both ZIPs parse to `d = 2026-08-12`; `import_sessions`
upserts one session; WP1's `(session_id, source_file)` upsert makes the second
meter's `PROJ0001` **overwrite** the first meter's `PROJ0001` (pre-WP1 the
positional upsert does the same by `run_number`). Nothing is flagged.
`do_upload`'s dedup only skips a run whose `start_time` already exists on that
date, so the overwrite happens unless both meters started a run in the same
second.

*Same meter, two sites on one date.* This is **not** an identity problem: runs
are still distinct `PROJnnnn`, and site already lives at run level
(`runs.location_tag`, `assessment_locations`). What is wrong is that the
session-level `location_label`/`lat`/`lng` and the single `weather` row pretend
the day had one site. Cosmetic, not corrupting.

*Concurrent hand entry (F6).* Because the id tables have no `AUTOINCREMENT`,
after any sync both Pis hold the same `max(id)`. Create an assessment on each
while the link is down and they get the **same id with certainty**, not by bad
luck; the next `/api/peer-sync-full` in each direction overwrites one with the
other (`ON CONFLICT(id) DO UPDATE`), and whichever Pi restarts second wins. A
deleted assessment has no tombstone either: if the `delete` event is missed the
peer's startup full-sync resurrects it (the exact hazard `deleted_sessions` was
added to fix for sessions).

---

## 2. Sessions — options

### (a) Keep date-keyed; add instrument/site as an attribute

`ALTER TABLE sessions ADD COLUMN instrument_serial TEXT;` shown in the UI, carried
in the payload, nothing else changes. Cheap, but it does not remove the
collision — two meters on one date still share a session and their `PROJ0001`s
still overwrite. It only labels the wreckage. Acceptable *only* if a second
meter would never be used on the same calendar date as the first. Not
recommended on its own, but its column is step one of (b).

### (b) Key sessions on `(date, instrument_serial)`

Natural composite key; date stays the human handle.

```sql
-- sessions (SQLite cannot drop a UNIQUE, so rebuild, as _migrate already does
-- for assessment_runs; ids are copied so runs.session_id stays valid)
CREATE TABLE sessions_new (
  id INTEGER PRIMARY KEY, date TEXT NOT NULL,
  instrument_serial TEXT NOT NULL DEFAULT '',
  … existing columns …,
  UNIQUE(date, instrument_serial));
INSERT INTO sessions_new SELECT id, date, :default_serial, … FROM sessions;
DROP TABLE sessions; ALTER TABLE sessions_new RENAME TO sessions;
-- companions
ALTER TABLE deleted_sessions   ADD COLUMN instrument_serial TEXT NOT NULL DEFAULT '';  -- + rebuild PK to (date, serial)
ALTER TABLE assessment_runs    ADD COLUMN instrument_serial TEXT NOT NULL DEFAULT '';  -- backfill := :default_serial
ALTER TABLE generated_reports  ADD COLUMN instrument_serial TEXT NOT NULL DEFAULT '';
-- weather: re-key to session id (it is per-site data)
```

* **Migration of the two live DBs.** `:default_serial` is whatever
  `app_settings['instrument_serial']` holds; it must be the *same string on
  both Pis* before either migrates, or the two copies of 2026-08-12 stop
  matching and the next sync creates a twin. Verify first
  (`sqlite3 noise.db "select value from app_settings where key='instrument_serial'"`
  on each). 22 runs; the migration is a few ms.
* **Upload path.** The app must learn the serial from outside the data:
  (i) a form field on `/upload` defaulting to the last-used serial, as the
  handoff already recommends, (ii) `import_sdcard.py --serial`, (iii) missing
  → the Pi's default serial. DIRFILE cannot supply it. A `--serial` that
  differs from the default should be a deliberate act, not a typo: the upload
  page should show which instrument it is about to file under.
* **Peer sync.** Payload gains `serial` beside `d`; `d` keeps its meaning.
  `import_sessions` treats a missing `serial` as the receiver's default (so an
  older peer, `sync_peer.py`, and old JSON exports keep importing unchanged);
  `get_sessions_since`/`get_all_sessions_json`/`get_sessions_export_format`
  emit it. `sessions_meta`, `run_tags`, `deleted_sessions`, `session/delete`
  events carry `serial` the same way. Old receiver, new sender: the unknown key
  is ignored and you get today's behaviour — tolerable only for the deploy
  window, so deploy both Pis in one go.
* **Assessments.** `assessment_runs` joins become
  `s.date = ar.session_date AND s.instrument_serial = ar.instrument_serial`;
  `assign_runs` receives the serial from the client along with `source_file`.
* **Reports.** `generated_reports.instrument_serial`; `_prepare_session_for_report`
  takes it.
* **UI.** Session list stays a list of dates; show a serial badge and a second
  row only when more than one distinct serial exists (today: none). Deep links
  `?date=…&serial=…`, routes `/session/<date>/<serial>/…` or serial as a query
  parameter with the default assumed. `assessments.html` run key gains the
  serial.
* **JSON import format.** Backward compatible both ways: old files import
  under the default serial; new files carry `serial` and old code ignores it.

Cost: roughly one day, most of it the forty call sites and the templates; the
SQL is small. The test suite already exercises import/assign/sync so
regressions would show.

### (c) Session id independent of date, date as an attribute

A synthetic session key (UUID or opaque text) with `date` a plain column. It is
the "right" relational answer but it loses the property that makes the present
sync converge: two Pis that import the same SD card independently
(`import_sdcard.py --push <A> --push <B>` does exactly this) must arrive at the
*same* session, which a random id cannot give. Make the id deterministic — say
`date || '/' || serial` — and (c) is (b) with the key concatenated into one
column. That variant ("`session_key TEXT UNIQUE`") is worth a thought because it
leaves every `WHERE date=?` a one-token change to `WHERE session_key=?` and
keeps routes single-segment, at the price of a key that looks like a date but
is not one. A genuinely opaque id would also need the F6 machinery (§3) for
sessions, which today they do not need.

### Verdict on sessions

(b), expressed either as two columns or as the concatenated `session_key`. (a)
does not fix the hazard; (c) without determinism breaks convergence and with it
collapses into (b).

---

## 3. Replicated ids — options

> **Implemented (WP10, 2026-08-20):** option 2 (UUID sync key, local ints
> kept) plus §3.4's timestamps, stale-write rejection with a conflict list
> (`sync_conflicts`, `GET /api/sync-conflicts`) and delete tombstones
> (`deleted_uids`) — extended beyond this note to `report_templates`, which
> now replicate, and to generated-report delete tombstones. One deviation
> from the sketch below: existing rows get `uuid5(namespace, content-seed)`
> (created_at|name etc.) rather than `'legacy-' || id`, so the backfill is
> correct even if the ids had drifted, at the cost of twinning any row whose
> seed fields held unsynced divergent edits at migration time. Details and
> deploy steps: `docs/fix-plan-2026-08-19.md`, WP10.


The tables in question: `assessments`, `assessment_locations`, `assessment_runs`
(replicated by `id`), and `report_templates`/`generated_reports` (not
replicated; each Pi's ids are private, so no hazard *yet* — but the moment
someone adds them to the full-sync payload the same collision appears, and the
default templates seeded by `_migrate` already share ids by coincidence only).

### 0. Operational rule: one writer

Declare one Pi the place where assessments are edited; the other is a mirror.
Zero schema change; removes the hazard entirely as long as the rule holds.
Fragile (nothing enforces it, and the UI on the mirror still offers the forms),
but it is the honest "smallest change" and may match how the Pis are actually
used. Only Catherine knows.

### 1. Per-Pi id ranges

Catherine allocates 1…999 999, Gladys 1 000 000…. Needs either `AUTOINCREMENT`
(table rebuild, then seed `sqlite_sequence`) or id selection in Python
(`SELECT COALESCE(MAX(id), :base) + 1 … WHERE id BETWEEN :base AND :top`).
Small, keeps integer ids in every URL, and existing rows need no change if the
two DBs currently agree (they should — both were populated through sync from
one origin — but **check by diffing the two `/api/peer-sync-full` payloads
before assuming**). Weak points: a third Pi needs a new range and a restore
from backup onto a different Pi silently re-uses the wrong range; and it does
nothing about concurrent *edits* of the same row (§3.4).

### 2. UUID sync key, local ints kept

```sql
-- one ADD COLUMN per statement (SQLite), then a UNIQUE index on each uid
ALTER TABLE assessments          ADD COLUMN uid TEXT;
ALTER TABLE assessment_locations ADD COLUMN uid TEXT;
ALTER TABLE assessment_locations ADD COLUMN assessment_uid TEXT;
ALTER TABLE assessment_runs      ADD COLUMN uid TEXT;
ALTER TABLE assessment_runs      ADD COLUMN assessment_uid TEXT;
ALTER TABLE assessment_runs      ADD COLUMN location_uid TEXT;
```

New rows get `uuid4()`. Existing rows get a **deterministic** uid so both Pis
agree without talking: `'legacy-' || id` (valid because the ids agree today;
if the payload diff says otherwise, fix that by hand first). Replication keys
on `uid` (`ON CONFLICT(uid) DO UPDATE`), payloads carry parent `*_uid`s and the
receiver resolves them to its local ints on insert; the int `id` stays the URL
handle and FK locally. Migration is additive; the full-sync payload gains three
fields per row; an old peer ignores `uid` and keeps colliding on `id` until
upgraded, so again deploy both together. About half a day including tests.

### 3. `(origin_pi, local_id)` composite

Same effect as 2 with readable keys (`('Catherine', 17)`), but two-column FKs
and URLs, and it hard-wires `PI_NAME` into data (rename a Pi, restore onto a
different host, and the keys lie). UUID does the same job with one column.

### 3.4 Conflict detection (orthogonal to the id scheme)

Today every upsert is "last payload applied wins", which on startup is
"whichever Pi booted second wins". Minimum: add `updated_at TEXT` (UTC,
`datetime('now')`) and `updated_by TEXT` (`PI_NAME`) to the three tables,
include them in every event and the full payload, and upsert only when
`excluded.updated_at > current.updated_at` (last-writer-wins by clock). Then
either (i) accept LWW — good enough for two people who talk to each other —
or (ii) when an incoming row is *older* than ours but differs, keep ours and
write the loser to a small `sync_conflicts` table surfaced on the Manage page.
(ii) is a further hour and is what I would do; silent LWW is how F6 became a
finding. Also add tombstones for assessment deletes (`deleted_assessments(uid,
deleted_at)`), replayed in `apply_full_sync` before the upserts, mirroring
`deleted_sessions`.

### Verdict on ids

**Done — WP10 implemented exactly this.** Option 2 (UUID) plus §3.4 timestamps and delete tombstones. Option 1 is
smaller but leaves the edit-conflict and restore hazards; option 0 costs
nothing and should be adopted as policy *today* regardless, until 2 lands.

---

## 4. Recommendation

**Now, before any second meter (an afternoon, can ride with WP3):**

1. Add `sessions.instrument_serial TEXT NOT NULL DEFAULT ''`, backfilled from
   the setting; carry `serial` in every session payload; make
   `import_sessions` **refuse** (skip with a visible reason, like WP1's
   skipped-file report) a session whose `serial` differs from the stored
   session's for that date. This turns the silent merge into a loud one and is
   step one of (b) anyway.
2. Operational rule: assessments are edited on one Pi only, until item 4.
3. Diff the two Pis' `/api/peer-sync-full` payloads once, to confirm the id
   tables agree (precondition for both 1-range and UUID migrations).

**Before a second instrument is bought:** finish (b) — `UNIQUE(date,
instrument_serial)`, the companion columns, the routes and templates — with
the serial entered at upload and defaulting to the last used. Decide then
whether to spell it as two columns or one `session_key`; I lean to two columns
with the default serial elided from URLs, because the UI stays date-shaped for
the one-meter case, which is almost certainly the common case forever.

**Before a third Pi (or before both Pis are routinely edited offline):**
UUID sync keys, `updated_at`/`updated_by` with stale-write rejection and a
conflict list, assessment tombstones. If reports are ever replicated, do them
with the uid scheme from the start.

Smallest change that removes each hazard: F3 → item 1 (refuse the merge);
F6 → item 2 (one writer). Neither is the end state, but both are safe and
reversible.

---

## 5. Questions only Catherine can answer

1. Is a second NOR140 actually likely, and if so would it be used on the same
   calendar dates as the first (two sites, two operators), or in series
   (replacement/loan)? In-series makes (a)+refusal sufficient for years.
2. Should both Pis accept uploads and assessment edits, or is one the working
   machine and the other a mirror? If the latter, §3 option 0 is the design.
3. Is the serial `6899108` set identically on both Pis today? (It gates the
   backfill.) Is the serial the right instrument identifier, or would a short
   name ("meter A") be what you would rather see in the UI and in filenames?
4. Is the date-keyed UI sacred — i.e. must `/?date=2026-08-12` keep working and
   the session list stay one row per day — or is one row per (day, instrument)
   acceptable once there are two?
5. Is a third Pi or a laptop copy of the app plausible? That decides whether
   per-Pi ranges would ever be enough.
6. Should weather follow the session (site) rather than the date? It matters
   only for two sites on one day.
7. Should report templates and generated reports replicate at all? Today they
   do not, and that may be a feature.
