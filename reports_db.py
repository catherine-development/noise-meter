"""
Storage for the AI report subsystem: prompt templates and generated reports.

Split out of noise_db.py. This subsystem shares the database but has no coupling
to the session/run measurement model beyond the session_date it is keyed on, so
it lives apart from the core noise data layer.

The report_templates / generated_reports tables themselves are still created by
noise_db._migrate(), which remains the single schema authority for the app.
"""
from noise_db import get_db


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
            "- \"compliance\": structured assessment against: (a) Environmental Protection Act 1990 s.79 — statutory nuisance test (unreasonable and substantial interference); (b) Noise Act 1996 — nighttime thresholds where measurements include 23:00–07:00 (35 dB LAeq indoors / 45 dB outside); (c) BS 4142:2014+A1:2019 — difference between specific noise level and background LA90 (+10 dB or above: significant adverse impact; +5 dB: likely adverse); (d) WHO Environmental Noise Guidelines for the European Region 2018. Use HTML tables where helpful.\n"
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
            "- \"compliance\": assessment against: (a) Control of Noise at Work Regulations 2005 — lower action value (80 dB LAeq,8h / 135 dB LCpeak), upper action value (85 dB LAeq,8h / 137 dB LCpeak), exposure limit value (87 dB LAeq,8h / 140 dB LCpeak); (b) BS 4142:2014+A1:2019 significance criteria relative to LA90 background; (c) WHO Environmental Noise Guidelines 2018 if applicable. Use HTML tables where helpful.\n"
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
            "- \"compliance\": structured assessment against: (a) BS 4142:2014+A1:2019 — source-background assessment; (b) BS 8233:2014 — internal ambient noise criteria for residential use (living rooms: 35 dB LAeq,16h daytime / 30 dB LAeq,8h night; bedrooms: 35 dB LAeq,8h night); (c) WHO Environmental Noise Guidelines 2018; (d) NPPF 2023 para 185 — whether development would be adversely affected by or create unacceptable noise impact. Use HTML tables where helpful.\n"
            "- \"conclusions\": suitability of the site for proposed use, or impact of the proposed development on the surrounding noise climate (HTML)\n"
            "- \"recommendations\": mitigation measures, planning conditions, further survey requirements, or objection grounds (HTML, use <ul> where appropriate)\n\n"
            "Respond with valid JSON only. No markdown fencing. No preamble."
        ),
    },
]


# ── Report templates ──────────────────────────────────────────────────────────

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
        conn.execute('UPDATE report_templates SET is_default=0')
    cur = conn.execute(
        'INSERT INTO report_templates (name, description, prompt, is_default) VALUES (?,?,?,?)',
        (name, description, prompt, 1 if is_default else 0)
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def update_report_template(tid, name, description, prompt, is_default=None):
    conn = get_db()
    if is_default:
        conn.execute('UPDATE report_templates SET is_default=0')
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
    conn = get_db()
    conn.execute('DELETE FROM report_templates WHERE id=?', (tid,))
    conn.commit()
    conn.close()


# ── Generated reports ─────────────────────────────────────────────────────────

def save_generated_report(session_date, template_id, template_name, model,
                          thinking_level, sections_json, input_tokens, output_tokens, cost_usd,
                          run_number=None, run_label=None):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO generated_reports '
        '  (session_date, run_number, run_label, template_id, template_name, model, thinking_level, '
        '   sections_json, input_tokens, output_tokens, cost_usd) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (session_date, run_number, run_label, template_id, template_name, model, thinking_level,
         sections_json, input_tokens, output_tokens, cost_usd)
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def get_generated_reports():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM generated_reports ORDER BY created_at DESC'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_generated_report(rid):
    conn = get_db()
    row = conn.execute('SELECT * FROM generated_reports WHERE id=?', (rid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_generated_report(rid):
    conn = get_db()
    conn.execute('DELETE FROM generated_reports WHERE id=?', (rid,))
    conn.commit()
    conn.close()
