"""
Assessments UI and API.

An assessment groups measurement runs under named locations for a BS 4142 or
Noise Act 1996 report, and carries the site/client metadata that goes with it.

Split out of noise_app.py as a Flask blueprint. Every mutation is mirrored to
the peer Pi via peer_client.sync_event_to_peer(), because assessment data is
entered by hand and is not recoverable from the SD card.
"""
import csv
import io

from flask import Blueprint, render_template, request, jsonify, abort, Response

from config import PI_NAME
from webauth import login_required
from peer_client import sync_event_to_peer
from assessments_db import (create_assessment, list_assessments, get_assessment,
                            update_assessment, delete_assessment,
                            add_assessment_location, get_assessment_location,
                            update_assessment_location, delete_assessment_location,
                            assign_runs, unassign_run, update_assessment_run,
                            get_assessment_run, get_assessment_runs_by_pairs,
                            get_assessment_detail, get_all_runs_for_assessment,
                            prepare_assessment_report_data)

bp = Blueprint('assessments', __name__)


@bp.route('/assessments')
@login_required
def assessments_page():
    return render_template('assessments.html', pi_name=PI_NAME)


@bp.route('/api/assessments', methods=['GET'])
@login_required
def api_get_assessments():
    return jsonify(list_assessments())


@bp.route('/api/assessments', methods=['POST'])
@login_required
def api_create_assessment():
    data = request.json or {}
    aid = create_assessment(
        name=data.get('name', 'Untitled'),
        purpose=data.get('purpose', ''),
        standard=data.get('standard', 'noise_act'),
        address=data.get('address', ''),
        postcode=data.get('postcode', ''),
        lat=data.get('lat'),
        lng=data.get('lng'),
        client_ref=data.get('client_ref', ''),
        notes=data.get('notes', ''),
    )
    sync_event_to_peer('assessment', 'upsert', get_assessment(aid))
    return jsonify({'id': aid})


@bp.route('/api/assessments/<int:aid>', methods=['GET'])
@login_required
def api_get_assessment(aid):
    detail = get_assessment_detail(aid)
    if not detail:
        abort(404)
    runs = get_all_runs_for_assessment(aid)
    return jsonify({'assessment': detail, 'runs': runs})


@bp.route('/api/assessments/<int:aid>', methods=['PUT'])
@login_required
def api_update_assessment(aid):
    data = request.json or {}
    update_assessment(
        aid,
        name=data.get('name', ''),
        purpose=data.get('purpose', ''),
        standard=data.get('standard', 'noise_act'),
        address=data.get('address', ''),
        postcode=data.get('postcode', ''),
        lat=data.get('lat'),
        lng=data.get('lng'),
        client_ref=data.get('client_ref', ''),
        notes=data.get('notes', ''),
    )
    sync_event_to_peer('assessment', 'upsert', get_assessment(aid))
    return jsonify({'status': 'ok'})


@bp.route('/api/assessments/<int:aid>', methods=['DELETE'])
@login_required
def api_delete_assessment(aid):
    delete_assessment(aid)
    sync_event_to_peer('assessment', 'delete', {'id': aid})
    return jsonify({'status': 'ok'})


@bp.route('/api/assessments/<int:aid>/locations', methods=['POST'])
@login_required
def api_add_location(aid):
    data = request.json or {}
    loc_id = add_assessment_location(
        assessment_id=aid,
        label=data.get('label', ''),
        description=data.get('description', ''),
        lat=data.get('lat'),
        lng=data.get('lng'),
        notes=data.get('notes', ''),
    )
    sync_event_to_peer('assessment_location', 'upsert', get_assessment_location(loc_id))
    return jsonify({'id': loc_id})


@bp.route('/api/assessments/<int:aid>/locations/<int:loc_id>', methods=['PUT'])
@login_required
def api_update_location(aid, loc_id):
    data = request.json or {}
    update_assessment_location(
        loc_id,
        label=data.get('label', ''),
        description=data.get('description', ''),
        lat=data.get('lat'),
        lng=data.get('lng'),
        notes=data.get('notes', ''),
    )
    sync_event_to_peer('assessment_location', 'upsert', get_assessment_location(loc_id))
    return jsonify({'status': 'ok'})


@bp.route('/api/assessments/<int:aid>/locations/<int:loc_id>', methods=['DELETE'])
@login_required
def api_delete_location(aid, loc_id):
    delete_assessment_location(loc_id)
    sync_event_to_peer('assessment_location', 'delete', {'id': loc_id})
    return jsonify({'status': 'ok'})


@bp.route('/api/assessments/<int:aid>/assign', methods=['POST'])
@login_required
def api_assign_runs(aid):
    data = request.json or {}
    location_id = data.get('location_id')
    runs = data.get('runs', [])
    pairs = [(r['date'], r['run_number']) for r in runs]
    assign_runs(aid, location_id, pairs)
    for row in get_assessment_runs_by_pairs(aid, pairs):
        sync_event_to_peer('assessment_run', 'upsert', row)
    return jsonify({'status': 'ok', 'assigned': len(pairs)})


@bp.route('/api/assessment-runs/<int:ar_id>', methods=['DELETE'])
@login_required
def api_unassign_run(ar_id):
    unassign_run(ar_id)
    sync_event_to_peer('assessment_run', 'delete', {'id': ar_id})
    return jsonify({'status': 'ok'})


@bp.route('/api/assessment-runs/<int:ar_id>', methods=['PUT'])
@login_required
def api_update_ar(ar_id):
    data = request.json or {}
    update_assessment_run(ar_id, data.get('conditions', ''), data.get('notes', ''))
    sync_event_to_peer('assessment_run', 'upsert', get_assessment_run(ar_id))
    return jsonify({'status': 'ok'})


@bp.route('/export/assessment/<int:aid>.csv')
@login_required
def export_assessment_csv(aid):
    data = prepare_assessment_report_data(aid)
    if not data:
        abort(404)
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(['sub_location', 'description', 'date', 'start_time', 'end_time', 'duration_s',
                'time_period', 'avg_laeq_db', 'min_laeq_db', 'max_laeq_db',
                'la10_db', 'la90_db', 'max_lcpeak_db', 'max_laimax_db',
                'conditions', 'notes'])
    for loc in data['locations']:
        for r in loc['runs']:
            w.writerow([
                loc['label'], loc['description'] or '', r['date'], r['start_time'],
                r.get('end_time') or '', r['duration_s'], r['time_period'],
                r.get('avg_laeq', ''), r.get('min_laeq', ''), r.get('max_laeq', ''),
                r.get('la10', ''), r.get('la90', ''),
                r.get('max_lcpeak', ''), r.get('max_laimax', ''),
                r.get('conditions', '') or '', r.get('notes', '') or '',
            ])
    aname = data['assessment']['name'].replace(' ', '_').replace('"', '').replace('\r', '').replace('\n', '')[:40]
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="assessment_{aname}.csv"'},
    )
