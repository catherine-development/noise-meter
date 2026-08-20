"""
Storage for the AI report subsystem: prompt templates and generated reports.

Split out of noise_db.py. This subsystem shares the database but has no coupling
to the session/run measurement model beyond the session_date it is keyed on, so
it lives apart from the core noise data layer.

The report_templates / generated_reports tables themselves are still created by
noise_db._migrate(), which remains the single schema authority for the app.
"""
import uuid

from noise_db import get_db, resolve_serial, record_uid_tombstones


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
            "- \"compliance\": structured assessment against: (a) Environmental Protection Act 1990 s.79 — statutory nuisance test (unreasonable and substantial interference); (b) Noise Act 1996 — the night period is 23:00–07:00. There is no single fixed threshold: the permitted level is 34 dB LAeq where the underlying level of noise is no more than 24 dB, otherwise 10 dB above the underlying level, and it is assessed INSIDE the complainant\u2019s dwelling. Do not compare an external 15-minute LAeq against a single number; state the underlying level relied on, and say plainly if measurements were external and therefore not directly comparable; (c) BS 4142:2014+A1:2019 — difference between specific noise level and background LA90 (+10 dB or above: significant adverse impact; +5 dB: likely adverse); (d) WHO Environmental Noise Guidelines for the European Region 2018. Use HTML tables where helpful.\n"
            "- \"conclusions\": whether measurements indicate a statutory nuisance and whether enforcement action is warranted under EPA 1990 s.80; state confidence level where appropriate (HTML)\n"
            "- \"recommendations\": further monitoring strategy, grounds for an abatement notice, evidence requirements for prosecution, referral pathways (HTML, use <ul> where appropriate)\n\n"
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
            "- \"compliance\": assessment against: (a) Control of Noise at Work Regulations 2005. The averaged values are daily personal noise exposure, LEP,d (equivalently LEX,8h), NOT a plain measured LAeq: lower action value 80 dB LEP,d / 135 dB LCpeak, upper action value 85 dB LEP,d / 137 dB LCpeak, exposure limit value 87 dB LEP,d / 140 dB LCpeak. A single measurement is not an exposure figure — LEP,d depends on the levels a person is exposed to and for how long, so state what exposure pattern has been assumed, or say that a dose assessment is required; (b) BS 4142:2014+A1:2019 significance criteria relative to LA90 background; (c) WHO Environmental Noise Guidelines 2018 if applicable. Use HTML tables where helpful.\n"
            "- \"conclusions\": clear professional conclusions about occupational noise exposure and risk to hearing (HTML)\n"
            "- \"recommendations\": hearing protection requirements, engineering controls, audiometric testing, further monitoring (HTML, use <ul> where appropriate)\n\n"
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
            "- \"compliance\": structured assessment against: (a) BS 4142:2014+A1:2019 — derive a rating level by applying character corrections for tonality, impulsivity and intermittency to the specific sound level, then compare that rating level against the background LA90 and interpret it in context (a difference around +5 dB indicates an adverse impact, around +10 dB or more a significant adverse impact); (b) BS 8233:2014 internal ambient noise guideline values for dwellings — living rooms 35 dB LAeq,16h (07:00–23:00), bedrooms 35 dB LAeq,16h during the day and 30 dB LAeq,8h at night (23:00–07:00), dining rooms 40 dB LAeq,16h; for external amenity space it is desirable not to exceed 50 dB LAeq,T, with 55 dB LAeq,T an upper guideline; (c) WHO Environmental Noise Guidelines for the European Region (2018), including sleep disturbance from regular individual night-time events — note that BS 8233 indicates a guideline for such events may be expressed as LAFmax or SEL rather than setting a fixed numeric limit of its own, so do not cite an LAmax figure as a BS 8233 requirement; (d) NPPF 2023 para 185 — whether the development would be adversely affected by, or would itself create, an unacceptable noise impact. State explicitly for each criterion whether it is met, marginal, or exceeded. Use HTML tables where helpful.\n"
            "- \"conclusions\": suitability of the site for proposed use, or impact of the proposed development on the surrounding noise climate (HTML)\n"
            "- \"recommendations\": mitigation measures, planning conditions, further survey requirements, or objection grounds (HTML, use <ul> where appropriate)\n\n"
            "Respond with valid JSON only. No markdown fencing. No preamble."
        ),
    },
]


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
        # updated_at so the change replicates (LWW) instead of leaving the
        # peer with two defaults.
        conn.execute("UPDATE report_templates SET is_default=0, "
                     "updated_at=datetime('now') WHERE is_default=1")
    cur = conn.execute(
        'INSERT INTO report_templates (uid, name, description, prompt, is_default) '
        'VALUES (?,?,?,?,?)',
        (str(uuid.uuid4()), name, description, prompt, 1 if is_default else 0)
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def update_report_template(tid, name, description, prompt, is_default=None):
    conn = get_db()
    if is_default:
        conn.execute("UPDATE report_templates SET is_default=0, "
                     "updated_at=datetime('now') WHERE is_default=1")
    fields = 'name=?, description=?, prompt=?, updated_at=datetime(\'now\')'
    params = [name, description, prompt]
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
