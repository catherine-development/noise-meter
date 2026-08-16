"""
AI-generated noise assessment reports.

Covers the whole report pipeline: the per-run/per-session statistics that feed
the prompt, the Claude call that produces the report sections, and the routes
for managing prompt templates and viewing generated reports.

Split out of noise_app.py as a Flask blueprint. The statistics helpers here are
report-specific — the authoritative measurement values are the GLOB-derived
scalars stored per run; these helpers only fall back to profile-derived
estimates for older sessions imported before those columns existed.
"""
import json
import math
import os

from flask import Blueprint, render_template, request, jsonify, abort

from config import PI_NAME
from webauth import login_required
from noise_db import get_all_sessions_json, get_session_prof_lafspl
from reports_db import (get_report_templates, get_report_template, save_report_template,
                        update_report_template, delete_report_template,
                        save_generated_report, get_generated_reports,
                        get_generated_report, delete_generated_report)

bp = Blueprint('reports', __name__)


def _percentile(sorted_vals, p):
    n = len(sorted_vals)
    if not n:
        return None
    idx = (p / 100) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return round(sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo]), 1)


def _energy_avg_db(values):
    if not values:
        return None
    return round(10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values)), 1)


def _expand_run(proj):
    """Expand the downsampled LAeq,1s profile back to approximate per-second values.
    Uses laeq_profile (PROF field 1 = LAeq,1s).  Use GLOB-derived scalar metrics for
    absolute accuracy; the profile is used for chart shape and pct85 only."""
    step = proj.get('step', 1)
    las = []
    for v in proj.get('laeq_profile', []):
        las.extend([v] * step)
    return las[:proj.get('n', len(las))]


def _run_stats(proj):
    """Compute statistical suite for one run.

    Prefers GLOB-derived scalar metrics (instrument-computed, accurate) over profile-
    derived estimates.  Falls back to profile stats only when GLOB values are absent
    (pre-update imports).  Note: profile values are LAS (slow-weighted SPL), so
    profile-derived LAeq/percentile estimates are ~8–11 dB high for impulsive sessions.
    """
    las_full = _expand_run(proj)
    stats = {'n': proj.get('n'), 'pmx': proj.get('pmx')}
    if las_full:
        s = sorted(las_full)
        n = len(s)
        stats.update({
            'leq':   _energy_avg_db(las_full),        # fallback only — overridden below
            'la10':  _percentile(s, 90),
            'la50':  _percentile(s, 50),
            'la90':  _percentile(s, 10),
            'lmax':  max(s),
            'lmin':  min(s),
            'pct85': round(100 * sum(1 for v in las_full if v >= 85) / n, 1),
        })
    # Override with GLOB-derived scalars where available
    if proj.get('avg')    is not None: stats['leq']  = round(proj['avg'], 1)
    if proj.get('la_l10') is not None: stats['la10'] = round(proj['la_l10'], 1)
    if proj.get('la_l50') is not None: stats['la50'] = round(proj['la_l50'], 1)
    if proj.get('la_l90') is not None: stats['la90'] = round(proj['la_l90'], 1)
    if proj.get('lafmax') is not None: stats['lmax'] = round(proj['lafmax'], 1)
    if proj.get('mn')     is not None: stats['lmin'] = round(proj['mn'], 1)
    return stats


# ── Reports ───────────────────────────────────────────────────────────────────

_MODEL_PRICING = {
    'claude-haiku-4-5-20251001': (0.80, 4.00),
    'claude-sonnet-5':           (3.00, 15.00),
    'claude-opus-5':             (15.00, 75.00),
}


def _build_session_data_block(sess, run_rows, all_laeq, total_duration_s):
    """Return the formatted session data text injected into report prompts."""
    loc_parts = [sess.get('loc'), sess.get('post')]
    location_str = ', '.join(p for p in loc_parts if p) or 'Not recorded'
    wx = sess.get('wx') or {}

    def wind_dir(deg):
        if deg is None:
            return ''
        dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
        return dirs[round(deg / 45) % 8]

    wx_parts = []
    if wx.get('ws') is not None:
        wx_parts.append(f"{wx['ws']} mph {wind_dir(wx.get('wd'))}")
    if wx.get('tn') is not None and wx.get('tx') is not None:
        wx_parts.append(f"temp {wx['tn']}–{wx['tx']} °C")
    if wx.get('pr'):
        wx_parts.append(f"precip {wx['pr']} mm")
    wx_str = ', '.join(wx_parts) or 'Not available'

    # Prefer GLOB-derived per-run scalars (already in run_rows via _run_stats).
    # Session LAeq = duration-weighted energy avg of per-run LAeq values.
    run_leq_ns = [(r['leq'], r.get('n') or 1) for r in run_rows if r.get('leq') is not None]
    if run_leq_ns:
        total_n = sum(n for _, n in run_leq_ns)
        session_leq = round(10 * math.log10(
            sum(n * 10 ** (leq / 10) for leq, n in run_leq_ns) / total_n
        ), 1)
    else:
        session_leq = _energy_avg_db(all_laeq) if all_laeq else None
    # LA10/LA90: true percentile of every run's pooled 1-second LAFspl samples
    # (Fast-weighted SPL — the statistical-descriptor convention, matching the
    # meter's own LAF percentiles), not an average of each run's own percentile
    # (percentiles don't compose linearly across sub-samples of different
    # sizes), and not LAeq,1s (a different, energy-averaged quantity). Falls
    # back to PROF-expanded percentiles only for older sessions without full
    # 1s data.
    pooled_lafspl = get_session_prof_lafspl(sess['d'], [r['run'] for r in run_rows])
    if pooled_lafspl:
        s = sorted(pooled_lafspl)
        session_la90 = _percentile(s, 10)
        session_la10 = _percentile(s, 90)
    else:
        session_la90 = _percentile(sorted(all_laeq), 10) if all_laeq else None
        session_la10 = _percentile(sorted(all_laeq), 90) if all_laeq else None
    run_lmaxs = [r['lmax'] for r in run_rows if r.get('lmax') is not None]
    session_lmax = max(run_lmaxs) if run_lmaxs else (max(all_laeq) if all_laeq else None)
    session_pmx  = max((r.get('pmx', 0) or 0 for r in run_rows), default=None)

    run_lines = [
        f"  Run {r['run']} ({r['start']}, {r['n']} s): "
        f"LAeq {r.get('leq')} dB | LA10 {r.get('la10')} | LA50 {r.get('la50')} | "
        f"LA90 {r.get('la90')} | LAmax {r.get('lmax')} | LCpeak {r.get('pmx')} | "
        f"Time≥85dB {r.get('pct85')}%"
        for r in run_rows
    ]

    gps_str = f"{sess['lat']}, {sess['lng']}" if sess.get('lat') and sess.get('lng') else 'Not recorded'
    scope = f"Run {run_rows[0]['run']} only" if len(run_rows) == 1 else f"Full session ({len(run_rows)} runs)"
    stat_label = f"RUN {run_rows[0]['run']} STATISTICS" if len(run_rows) == 1 else "SESSION STATISTICS"
    return (
        f"Instrument: Norsonic NOR140 Class 1 precision sound level meter (IEC 61672-1:2013).\n"
        f"Measurement date: {sess['d']}\n"
        f"Report scope: {scope}\n"
        f"Location: {location_str}\n"
        f"Recorder: {sess.get('name') or 'Not recorded'}\n"
        f"GPS: {gps_str}\n"
        f"Notes: {sess.get('notes') or 'None'}\n"
        f"Weather: {wx_str}\n\n"
        f"{stat_label}:\n"
        f"  Total duration: {total_duration_s} seconds\n"
        f"  LAeq: {session_leq} dB(A)\n"
        f"  LA10: {session_la10} dB(A)\n"
        f"  LA90: {session_la90} dB(A)\n"
        f"  LAmax: {session_lmax} dB(A)\n"
        f"  LCpeak max: {session_pmx} dB(C)\n\n"
        f"PER-RUN BREAKDOWN:\n" + '\n'.join(run_lines)
    ), {
        'session_leq': session_leq, 'session_la10': session_la10,
        'session_la90': session_la90, 'session_lmax': session_lmax,
        'session_pmx': session_pmx, 'wx': wx, 'wx_str': wx_str,
    }


_REPORT_TOOL = {
    'name': 'submit_report',
    'description': 'Submit the completed noise assessment report sections as HTML',
    'input_schema': {
        'type': 'object',
        'properties': {
            'executive_summary': {'type': 'string', 'description': 'HTML content'},
            'methodology':       {'type': 'string', 'description': 'HTML content'},
            'results_narrative': {'type': 'string', 'description': 'HTML content'},
            'compliance':        {'type': 'string', 'description': 'HTML content'},
            'conclusions':       {'type': 'string', 'description': 'HTML content'},
            'recommendations':   {'type': 'string', 'description': 'HTML content'},
        },
        'required': ['executive_summary', 'methodology', 'results_narrative',
                     'compliance', 'conclusions', 'recommendations'],
    },
}


def _call_claude(prompt, model, thinking_level):
    """Call Claude and return (sections_dict, input_tokens, output_tokens, cost_usd)."""
    ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    if not ANTHROPIC_KEY:
        raise RuntimeError('ANTHROPIC_API_KEY not set')

    import anthropic as _anthropic
    client = _anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # Strip any old JSON-output instruction from the template prompt — tool_use handles that now
    for phrase in (
        'Respond with valid JSON only. No markdown fencing. No preamble.',
        'No markdown fencing. No preamble.',
    ):
        prompt = prompt.replace(phrase, '').strip()

    kwargs = dict(
        model=model,
        max_tokens=16000,
        tools=[_REPORT_TOOL],
        tool_choice={'type': 'tool', 'name': 'submit_report'},
        messages=[{'role': 'user', 'content': prompt}],
    )
    if thinking_level == 'standard':
        kwargs['thinking']      = {'type': 'adaptive'}
        kwargs['output_config'] = {'effort': 'medium'}
    elif thinking_level == 'extended':
        kwargs['thinking']      = {'type': 'adaptive'}
        kwargs['output_config'] = {'effort': 'high'}

    message = client.messages.create(**kwargs)

    # tool_use block follows any thinking blocks; SDK already parsed the JSON for us
    tool_use = next(
        (b for b in message.content if getattr(b, 'type', '') == 'tool_use'),
        None,
    )
    if tool_use is None:
        raise RuntimeError('No tool_use block in Claude response')
    sections = tool_use.input  # dict — no JSON parsing needed, no escaping issues

    u = message.usage
    tok_in  = getattr(u, 'input_tokens', 0)
    tok_out = getattr(u, 'output_tokens', 0)
    price_in, price_out = _MODEL_PRICING.get(model, (3.0, 15.0))
    cost_usd = (tok_in * price_in + tok_out * price_out) / 1_000_000
    return sections, tok_in, tok_out, round(cost_usd, 4)


def _prepare_session_for_report(date, run_number=None):
    """Fetch session + compute stats. run_number=None means all runs.
    Returns (sess, run_rows, all_laeq, total_s) or None."""
    all_sessions = get_all_sessions_json()['sessions']
    sess = next((s for s in all_sessions if s['d'] == date), None)
    if not sess:
        return None
    all_projects = sess.get('projects', [])
    if run_number is not None:
        if run_number < 1 or run_number > len(all_projects):
            return None
        projects = [all_projects[run_number - 1]]
        base_num = run_number
    else:
        projects = all_projects
        base_num = None
    run_rows, all_laeq = [], []
    for i, proj in enumerate(projects, 1):
        rn = base_num if base_num is not None else i
        st = _run_stats(proj)
        run_rows.append({'run': rn, 'start': proj['start'], **st})
        all_laeq.extend(_expand_run(proj))
    total_s = sum(p.get('n', 0) for p in projects)
    return sess, run_rows, all_laeq, total_s


@bp.route('/reports')
@login_required
def reports_page():
    all_sessions = get_all_sessions_json()['sessions']
    sessions_with_runs = [
        {
            'd': s['d'],
            'runs': [{'n': i + 1, 'start': p['start'], 'dur': p.get('n', 0)}
                     for i, p in enumerate(s.get('projects', []))],
        }
        for s in reversed(all_sessions)
    ]
    templates = get_report_templates()
    history = get_generated_reports()
    return render_template(
        'reports.html',
        pi_name=PI_NAME,
        sessions_with_runs=sessions_with_runs,
        templates=templates,
        history=history,
    )


@bp.route('/api/report-templates', methods=['GET'])
@login_required
def api_list_templates():
    return jsonify(get_report_templates())


@bp.route('/api/report-templates', methods=['POST'])
@login_required
def api_create_template():
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    tid = save_report_template(
        name=name,
        description=(data.get('description') or '').strip(),
        prompt=(data.get('prompt') or '').strip(),
        is_default=bool(data.get('is_default')),
    )
    return jsonify({'id': tid})


@bp.route('/api/report-templates/<int:tid>', methods=['PUT'])
@login_required
def api_update_template(tid):
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    update_report_template(
        tid=tid,
        name=name,
        description=(data.get('description') or '').strip(),
        prompt=(data.get('prompt') or '').strip(),
        is_default=data.get('is_default'),
    )
    return jsonify({'ok': True})


@bp.route('/api/report-templates/<int:tid>', methods=['DELETE'])
@login_required
def api_delete_template(tid):
    delete_report_template(tid)
    return jsonify({'ok': True})


@bp.route('/api/generate-report', methods=['POST'])
@login_required
def api_generate_report():
    data = request.get_json(force=True) or {}
    date           = data.get('session_date', '').strip()
    template_id    = data.get('template_id')
    model          = data.get('model', 'claude-sonnet-5')
    thinking_level = data.get('thinking_level', 'none')
    run_number     = data.get('run_number')  # int or None = all runs
    if run_number is not None:
        run_number = int(run_number)

    if not date:
        return jsonify({'error': 'session_date required'}), 400

    result = _prepare_session_for_report(date, run_number)
    if not result:
        return jsonify({'error': f'Session {date} not found'}), 404
    sess, run_rows, all_laeq, total_s = result

    tmpl = get_report_template(template_id) if template_id else None
    if not tmpl:
        templates = get_report_templates()
        tmpl = next((t for t in templates if t['is_default']), templates[0] if templates else None)
    if not tmpl:
        return jsonify({'error': 'No report templates configured'}), 400

    session_data_block, _ = _build_session_data_block(sess, run_rows, all_laeq, total_s)
    prompt = tmpl['prompt'].replace('{{session_data}}', session_data_block)

    try:
        sections, tok_in, tok_out, cost_usd = _call_claude(prompt, model, thinking_level)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    run_label = f"Run {run_number}" if run_number else "All runs"
    rid = save_generated_report(
        session_date=date,
        template_id=tmpl['id'],
        template_name=tmpl['name'],
        model=model,
        thinking_level=thinking_level,
        sections_json=json.dumps(sections),
        input_tokens=tok_in,
        output_tokens=tok_out,
        cost_usd=cost_usd,
        run_number=run_number,
        run_label=run_label,
    )
    print(f"Report generated [{date}] template={tmpl['name']} model={model} "
          f"thinking={thinking_level}: {tok_in}/{tok_out} tokens ≈ ${cost_usd:.4f}")
    return jsonify({'report_id': rid, 'cost_usd': cost_usd,
                    'input_tokens': tok_in, 'output_tokens': tok_out})


@bp.route('/reports/<int:rid>')
@login_required
def view_report(rid):
    stored = get_generated_report(rid)
    if not stored:
        abort(404)

    sections = json.loads(stored['sections_json'])
    date = stored['session_date']

    result = _prepare_session_for_report(date, stored.get('run_number'))
    if result:
        sess, run_rows, all_laeq, total_s = result
        _, stats = _build_session_data_block(sess, run_rows, all_laeq, total_s)
    else:
        sess, run_rows, stats = {'d': date}, [], {}
        total_s = 0

    usage_info = {
        'input_tokens':  stored.get('input_tokens'),
        'output_tokens': stored.get('output_tokens'),
        'cost_usd':      stored.get('cost_usd'),
        'model':         stored.get('model'),
        'thinking_level': stored.get('thinking_level'),
        'template_name': stored.get('template_name'),
    }

    return render_template(
        'report.html',
        date=date,
        pi_name=PI_NAME,
        sess=sess,
        run_rows=run_rows,
        session_leq=stats.get('session_leq'),
        session_la10=stats.get('session_la10'),
        session_la90=stats.get('session_la90'),
        session_lmax=stats.get('session_lmax'),
        session_pmx=stats.get('session_pmx'),
        total_duration_s=total_s,
        wx=stats.get('wx', {}),
        wx_str=stats.get('wx_str', ''),
        sections=sections,
        usage_info=usage_info,
        generated_at=stored.get('created_at', ''),
    )


@bp.route('/api/generated-reports', methods=['GET'])
@login_required
def api_list_reports():
    return jsonify(get_generated_reports())


@bp.route('/api/generated-reports/<int:rid>', methods=['DELETE'])
@login_required
def api_delete_report(rid):
    delete_generated_report(rid)
    return jsonify({'ok': True})
