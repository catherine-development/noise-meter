"""
Storage for the AI report subsystem: prompt templates and generated reports.

Split out of noise_db.py. This subsystem shares the database but has no coupling
to the session/run measurement model beyond the session_date it is keyed on, so
it lives apart from the core noise data layer.

The report_templates / generated_reports tables themselves are still created by
noise_db._migrate(), which remains the single schema authority for the app.
"""
import hashlib
import logging
import uuid

from noise_db import (get_db, resolve_serial, record_uid_tombstones,
                      LWW_NOW_SQL, local_writer)

log = logging.getLogger('noise.reports_db')


DEFAULT_TEMPLATES = [
    {
        'name': 'Local Government Enforcement',
        'description': 'Statutory nuisance evidence under EPA 1990, Noise Act 1996, BS 4142',
        'is_default': 1,
        'prompt': (
            "You are a qualified UK noise consultant producing a professional noise assessment "
            "report for submission to a local authority as evidence supporting a statutory noise "
            "nuisance investigation under the Environmental Protection Act 1990.\n\n"
            "{{session_data}}\n\n"
            "Produce a professional noise assessment report as a JSON object with exactly these keys:\n"
            "- \"executive_summary\": 2–3 sentence plain-English overview suitable for a non-specialist council officer (HTML)\n"
            "- \"methodology\": instrument (Norsonic NOR140 Class 1, IEC 61672-1:2013), parameters measured (LAeq, LA10, LA90, LCpeak), measurement approach, and any limitations (HTML)\n"
            "- \"results_narrative\": narrative of LAeq, LA10 (intrusive events), LA90 (background noise level), any significant peaks, time patterns; compare specific source level to background where identifiable (HTML)\n"
            "- \"compliance\": structured assessment against: (a) Environmental Protection Act 1990 s.79 — statutory nuisance test (unreasonable and substantial interference); (b) Noise Act 1996 — the night period is 23:00–07:00. There is no single fixed threshold: the permitted level is 34 dB LAeq where the underlying level of noise is no more than 24 dB, otherwise 10 dB above the underlying level, and it is assessed INSIDE the complainant\u2019s dwelling. Do not compare an external 15-minute LAeq against a single number; state the underlying level relied on, and say plainly if measurements were external and therefore not directly comparable; (c) BS 4142:2014+A1:2019 — derive a RATING level: the specific sound level plus character corrections for tonality, impulsivity, intermittency and any other distinctive acoustic characteristic, naming each correction and the dB applied; assess that rating level against the background LA90 (+10 dB or more above the background: significant adverse impact; around +5 dB: adverse impact; both read in context). Character corrections are the assessor's on-site judgement and are not measured by the instrument: state every correction you apply as an explicit assumption, and where none can be justified from the recorded data say so and take the rating level as equal to the specific sound level — never invent one; (d) WHO Environmental Noise Guidelines for the European Region 2018. Use HTML tables where helpful.\n"
            "- \"conclusions\": whether measurements indicate a statutory nuisance and whether enforcement action is warranted under EPA 1990 s.80; state confidence level where appropriate (HTML)\n"
            "- \"recommendations\": further monitoring strategy, grounds for an abatement notice, evidence requirements for prosecution, referral pathways (HTML, use <ul> where appropriate)\n\n"
            "The weather line in the measurement data above is archived regional data from Open-Meteo, not an on-site observation: treat it as context only, state that on-site meteorological conditions were not recorded unless the notes say otherwise, and flag that limitation wherever a BS 4142 conclusion depends on wind or precipitation.\n\n"
            "Respond with valid JSON only. No markdown fencing. No preamble."
        ),
    },
    {
        'name': 'Occupational Health',
        'description': 'Workplace noise under Control of Noise at Work Regulations 2005 / HSE action values',
        'is_default': 0,
        'prompt': (
            "You are a qualified UK noise consultant producing a professional occupational noise "
            "assessment report under the Control of Noise at Work Regulations 2005.\n\n"
            "{{session_data}}\n\n"
            "Produce a professional noise assessment report as a JSON object with exactly these keys:\n"
            "- \"executive_summary\": 2–3 sentence plain-English overview (HTML)\n"
            "- \"methodology\": instrument (Norsonic NOR140 Class 1, IEC 61672-1:2013), parameters measured, and approach (HTML)\n"
            "- \"results_narrative\": narrative of the per-run and session data — note any significant variability, peaks, and background noise level (LA90) (HTML)\n"
            "- \"compliance\": assessment against: (a) Control of Noise at Work Regulations 2005. The averaged values are daily personal noise exposure, LEP,d (equivalently LEX,8h), NOT a plain measured LAeq: lower action value 80 dB LEP,d / 135 dB LCpeak, upper action value 85 dB LEP,d / 137 dB LCpeak, exposure limit value 87 dB LEP,d / 140 dB LCpeak. A single measurement is not an exposure figure — LEP,d depends on the levels a person is exposed to and for how long, so state what exposure pattern has been assumed, or say that a dose assessment is required; (b) BS 4142:2014+A1:2019 significance criteria — compare a RATING level (the specific sound level plus character corrections for tonality, impulsivity, intermittency and any other distinctive acoustic characteristic, each named with the dB applied) against the background LA90: around +5 dB indicates an adverse impact and +10 dB or more a significant adverse impact, both read in context. Character corrections are the assessor's on-site judgement and are not measured by the instrument: state every correction you apply as an explicit assumption, and where none can be justified from the recorded data say so and take the rating level as equal to the specific sound level — never invent one; (c) WHO Environmental Noise Guidelines 2018 if applicable. Use HTML tables where helpful.\n"
            "- \"conclusions\": clear professional conclusions about occupational noise exposure and risk to hearing (HTML)\n"
            "- \"recommendations\": hearing protection requirements, engineering controls, audiometric testing, further monitoring (HTML, use <ul> where appropriate)\n\n"
            "The weather line in the measurement data above is archived regional data from Open-Meteo, not an on-site observation: treat it as context only, state that on-site meteorological conditions were not recorded unless the notes say otherwise, and flag that limitation wherever a BS 4142 conclusion depends on wind or precipitation.\n\n"
            "Respond with valid JSON only. No markdown fencing. No preamble."
        ),
    },
    {
        'name': 'Planning Noise Assessment',
        'description': 'NPPF / BS 4142 / BS 8233 assessment for planning applications',
        'is_default': 0,
        'prompt': (
            "You are a qualified UK noise consultant producing a noise assessment report for "
            "submission in support of or in response to a planning application, in accordance with "
            "the National Planning Policy Framework (NPPF) and relevant British Standards.\n\n"
            "{{session_data}}\n\n"
            "Produce a professional noise assessment report as a JSON object with exactly these keys:\n"
            "- \"executive_summary\": 2–3 sentence overview of the noise environment and its relevance to the planning context (HTML)\n"
            "- \"methodology\": instrument (Norsonic NOR140 Class 1, IEC 61672-1:2013), parameters, and relationship to BS 4142:2014+A1:2019 and BS 8233:2014 (HTML)\n"
            "- \"results_narrative\": characterise the acoustic environment — dominant noise sources, LA90 background level, LA10, LAeq, any tonal or impulsive components (HTML)\n"
            "- \"compliance\": structured assessment against: (a) BS 4142:2014+A1:2019 — derive a rating level by applying character corrections for tonality, impulsivity and intermittency to the specific sound level, then compare that rating level against the background LA90 and interpret it in context (a difference around +5 dB indicates an adverse impact, around +10 dB or more a significant adverse impact); name each character correction and the dB applied. Character corrections are the assessor's on-site judgement and are not measured by the instrument: state every correction you apply as an explicit assumption, and where none can be justified from the recorded data say so and take the rating level as equal to the specific sound level — never invent one; (b) BS 8233:2014 internal ambient noise guideline values for dwellings — living rooms 35 dB LAeq,16h (07:00–23:00), bedrooms 35 dB LAeq,16h during the day and 30 dB LAeq,8h at night (23:00–07:00), dining rooms 40 dB LAeq,16h; for external amenity space it is desirable not to exceed 50 dB LAeq,T, with 55 dB LAeq,T an upper guideline; (c) WHO Environmental Noise Guidelines for the European Region (2018), including sleep disturbance from regular individual night-time events — note that BS 8233 indicates a guideline for such events may be expressed as LAFmax or SEL rather than setting a fixed numeric limit of its own, so do not cite an LAmax figure as a BS 8233 requirement; (d) NPPF 2023 para 185 — whether the development would be adversely affected by, or would itself create, an unacceptable noise impact. State explicitly for each criterion whether it is met, marginal, or exceeded. Use HTML tables where helpful.\n"
            "- \"conclusions\": suitability of the site for proposed use, or impact of the proposed development on the surrounding noise climate (HTML)\n"
            "- \"recommendations\": mitigation measures, planning conditions, further survey requirements, or objection grounds (HTML, use <ul> where appropriate)\n\n"
            "The weather line in the measurement data above is archived regional data from Open-Meteo, not an on-site observation: treat it as context only, state that on-site meteorological conditions were not recorded unless the notes say otherwise, and flag that limitation wherever a BS 4142 conclusion depends on wind or precipitation.\n\n"
            "Respond with valid JSON only. No markdown fencing. No preamble."
        ),
    },
]


# ── WP13: refreshing the defaults that are already on the Pis ─────────────────
#
# The seed above only runs on an EMPTY report_templates table, so correcting a
# DEFAULT_TEMPLATES prompt in the source does nothing to the two live Pis,
# which have held their three seeded rows since the table was created. Codex
# F7 (BS 4142 rating level, and the archived-weather caveat) is a correctness
# fix to evidence-bearing prompts, so it has to reach those stored rows.
#
# The rule: a stored row whose prompt is byte-identical to a text this repo
# once shipped is an untouched default and is rewritten to the current text;
# anything else is Catherine's own edit and is left alone (logged at INFO).
# The digests below are every historical shipped prompt, so a Pi still holding
# a pre-17288f0 prompt is refreshed too.
#
# Both Pis run this migration independently and compute the identical new
# prompt, so the write must not depend on when each one restarted:
# updated_at is set to a FIXED timestamp rather than datetime('now'). WP10's
# LWW gate skips an incoming row only when the local updated_at is strictly
# greater, so two identical timestamps apply in both directions — the pair
# converges with no spurious sync_conflicts row, whichever Pi restarts first.
# The refresh runs before the WP10 uid backfill (see noise_db._migrate), so
# the deterministic template uid — uuid5 of name + sha256(prompt) — is
# computed from the NEW prompt on every database, migrated or freshly seeded.
PREVIOUS_DEFAULT_PROMPT_SHA256 = {
    'Local Government Enforcement': (
        '52ad17dfb56168c914e6ae1abd70a027d54a843eebd6f66a8cf7b8184f1f8d20',  # 17288f0 … 92b65b2
        '23068a64c383b4a29b056ee82e33fd3ec9e9e8580f26bc93bfb3d122bb533ccc',  # 7796097 … ab846e2
    ),
    'Occupational Health': (
        'd771d9db6b70a914312133862fd1823b0871c7b497aff840b29f2e31baf35913',  # 17288f0 … 92b65b2
        'ff89cdda5ffd0027bcce1e4458802edc5db74fe885caf1bbea4e0392e4e96089',  # 7796097 … ab846e2
    ),
    'Planning Noise Assessment': (
        '54313665f0fbf65d242760c0e36a428cc12a035d38a5b88e08632b848ab251bb',  # ab846e2 … 92b65b2
        '5ba3ba1f3bef4d7663653fc6917fd80ca33655fb51b7d446a562aed2963caf3e',  # 7796097
    ),
}

# Deliberately fixed and deliberately in the past-tense of the WP13 deploy:
# see the note above on why this is not datetime('now').
DEFAULT_TEMPLATE_REFRESH_AT = '2026-08-20 00:00:00'


def refresh_default_templates(conn):
    """Rewrite stored default templates that still hold a previously shipped
    prompt. Returns the names refreshed. Caller commits.

    Idempotent: a row already carrying the current prompt is skipped, so a
    second migration pass writes nothing and does not re-bump updated_at."""
    refreshed = []
    for t in DEFAULT_TEMPLATES:
        known_old = PREVIOUS_DEFAULT_PROMPT_SHA256.get(t['name'], ())
        rows = conn.execute(
            'SELECT id, prompt FROM report_templates WHERE name=? ORDER BY id',
            (t['name'],)).fetchall()
        for row in rows:
            stored = row['prompt'] or ''
            if stored == t['prompt']:
                continue                      # already current
            if hashlib.sha256(stored.encode()).hexdigest() not in known_old:
                log.info("Report template %r (id=%s) has been edited locally — "
                         "leaving it as it is; the shipped default has changed "
                         "(WP13: BS 4142 rating level, archived-weather caveat)",
                         t['name'], row['id'])
                continue
            conn.execute(
                'UPDATE report_templates SET prompt=?, updated_at=? WHERE id=?',
                (t['prompt'], DEFAULT_TEMPLATE_REFRESH_AT, row['id']))
            refreshed.append(t['name'])
    if refreshed:
        log.info('Refreshed %d unmodified default report template(s) to the '
                 'current shipped text: %s', len(refreshed), ', '.join(refreshed))
    return refreshed


# ── Report templates ──────────────────────────────────────────────────────────
# Replicated since WP10 (F6): keyed across the pair by uid with LWW on
# updated_at, like the other hand-entered tables. The three DEFAULT_TEMPLATES
# rows each Pi seeded independently unify under one deterministic uid apiece
# (the migration hashes name + prompt); user templates replicate as their own
# records. Deletes tombstone the uid so a full sync cannot resurrect them.

def get_report_templates():
    conn = get_db()
    rows = conn.execute('SELECT * FROM report_templates ORDER BY is_default DESC, id').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_report_template(tid):
    conn = get_db()
    row = conn.execute('SELECT * FROM report_templates WHERE id=?', (tid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_report_template(name, description, prompt, is_default=0):
    conn = get_db()
    if is_default:
        # Clearing the old default is an edit of those rows too: bump their
        # updated_at (millisecond, with writer, since WP12) so the change
        # replicates (LWW) instead of leaving the peer with two defaults.
        conn.execute(f'UPDATE report_templates SET is_default=0, '
                     f'updated_at={LWW_NOW_SQL}, writer=? WHERE is_default=1',
                     (local_writer(),))
    cur = conn.execute(
        f'INSERT INTO report_templates (uid, name, description, prompt, is_default, '
        f'  updated_at, writer) '
        f'VALUES (?,?,?,?,?,{LWW_NOW_SQL},?)',
        (str(uuid.uuid4()), name, description, prompt, 1 if is_default else 0,
         local_writer())
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def update_report_template(tid, name, description, prompt, is_default=None):
    conn = get_db()
    if is_default:
        conn.execute(f'UPDATE report_templates SET is_default=0, '
                     f'updated_at={LWW_NOW_SQL}, writer=? WHERE is_default=1',
                     (local_writer(),))
    fields = f'name=?, description=?, prompt=?, updated_at={LWW_NOW_SQL}, writer=?'
    params = [name, description, prompt, local_writer()]
    if is_default is not None:
        fields += ', is_default=?'
        params.append(1 if is_default else 0)
    params.append(tid)
    conn.execute(f'UPDATE report_templates SET {fields} WHERE id=?', params)
    conn.commit()
    conn.close()


def delete_report_template(tid):
    """Returns {'uid','deleted_at'} for the sync event, or None. The uid is
    tombstoned so the delete replicates and survives full syncs (F6)."""
    conn = get_db()
    info = _delete_by_uid(conn, 'report_templates', tid)
    conn.commit()
    conn.close()
    return info


def _delete_by_uid(conn, table, row_id):
    row = conn.execute(f'SELECT uid FROM {table} WHERE id=?', (row_id,)).fetchone()
    conn.execute(f'DELETE FROM {table} WHERE id=?', (row_id,))
    if not (row and row['uid']):
        return None
    record_uid_tombstones(conn, table, [row['uid']])
    ts = conn.execute(
        'SELECT deleted_at FROM deleted_uids WHERE table_name=? AND uid=?',
        (table, row['uid'])).fetchone()
    return {'uid': row['uid'], 'deleted_at': ts['deleted_at'] if ts else None}


# ── Generated reports ─────────────────────────────────────────────────────────

def save_generated_report(session_date, template_id, template_name, model,
                          thinking_level, sections_json, input_tokens, output_tokens, cost_usd,
                          run_number=None, run_label=None,
                          source_file=None, input_snapshot_json=None,
                          instrument_serial=None):
    """source_file pins a single-run report to the run's stable identity
    (run_number is display metadata that moves on re-import);
    input_snapshot_json is the JSON of every input the report was rendered
    from, so view_report can show what was true at generation time.
    instrument_serial names the session with session_date (None/blank = the
    default serial). uid is minted here (WP9): it is the report's identity
    across the two Pis — the peer upserts on it, never on the local id."""
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO generated_reports '
        '  (uid, session_date, instrument_serial, run_number, run_label, template_id, '
        '   template_name, model, thinking_level, sections_json, input_tokens, '
        '   output_tokens, cost_usd, source_file, input_snapshot_json) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (str(uuid.uuid4()), session_date, resolve_serial(instrument_serial, conn),
         run_number, run_label, template_id, template_name, model, thinking_level,
         sections_json, input_tokens, output_tokens, cost_usd, source_file, input_snapshot_json)
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def get_generated_reports():
    """Listing rows. The input snapshot is a per-report blob that only
    view_report needs, so it is left out of the listing."""
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM generated_reports ORDER BY created_at DESC'
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d.pop('input_snapshot_json', None)
        out.append(d)
    return out


def get_generated_report(rid):
    conn = get_db()
    row = conn.execute('SELECT * FROM generated_reports WHERE id=?', (rid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_generated_report(rid):
    """Returns {'uid','deleted_at'} or None (a pre-WP9 row has no uid and
    never replicated — nothing to tombstone). The tombstone closes the WP9
    gap: without it, a full sync from a Pi still holding the row resurrected
    it on the Pi that deleted while the holder was offline."""
    conn = get_db()
    info = _delete_by_uid(conn, 'generated_reports', rid)
    conn.commit()
    conn.close()
    return info
