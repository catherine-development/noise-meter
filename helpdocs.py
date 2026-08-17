"""
In-app help: serves the reference documents kept in docs/.

Those files are Artifact fragments — a <title>, a <style> block and the page
content, with no <!doctype>/<html>/<head>/<body> — so the same file can be
published as an Artifact unchanged. Browsers tolerate the missing structure, but
wrapping it here means the served page declares its charset and viewport and
renders exactly like the published version, plus a slim bar to get back to the
app (the documents carry their own full-page styling and no app chrome).

Slugs are looked up in DOCUMENTS rather than joined onto a path, so nothing a
visitor types can escape docs/.
"""
import os

from flask import Blueprint, Response, abort, render_template, send_file

from config import PI_NAME
from webauth import login_required

bp = Blueprint('helpdocs', __name__)

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs')

DOCUMENTS = {
    'reading-the-nor140': {
        'html':  'reading-the-nor140.html',
        'pdf':   'Reading the NOR140.pdf',
        'title': 'Reading the NOR140',
        'pages': 14,
        'who':   'Practitioners and technical reviewers',
        'blurb': ('Every measurement the meter records and what each one means, how metric '
                  'names are built up, the binary format and how values are decoded from it, '
                  'which figures are derived and how, the traps that produce plausible but '
                  'wrong numbers, which standard asks for which metric, and a worked example.'),
        'note':  ('Contains byte offsets and detector internals. Better suited to a consultant '
                  'reviewing the methodology than to a client.'),
    },
    'where-the-numbers-come-from': {
        'html':  'where-the-numbers-come-from.html',
        'pdf':   'Where the Numbers Come From.pdf',
        'title': 'Where the Numbers Come From',
        'pages': 5,
        'who':   'Clients',
        'blurb': ('Plain-language provenance: what the instrument measures directly, the four '
                  'things this app calculates, how far each figure can be relied on, what is '
                  'not measured on site, how the figures were checked, the calibration '
                  'position, and that the report narrative is AI-drafted.'),
        'note':  'Written to be sent out. No internals.',
    },
}

_BACK_BAR = (
    '<div style="position:sticky;top:0;z-index:99;display:flex;gap:16px;align-items:center;'
    'padding:9px 20px;font:500 13px/1.4 system-ui,-apple-system,sans-serif;'
    'background:var(--surface,#fff);border-bottom:1px solid var(--rule,#ddd);'
    'color:var(--muted,#666)" class="app-backbar">'
    '<a href="/help" style="color:var(--amber,#9A5D06);text-decoration:none">&larr; Help</a>'
    '<a href="/" style="color:var(--muted,#666);text-decoration:none">Sessions</a>'
    '<span style="margin-left:auto"><a href="__PDF__" '
    'style="color:var(--muted,#666);text-decoration:none">Download PDF</a></span>'
    '</div>'
    # NB: substituted with .replace(), not .format() — this string contains CSS
    # braces, which .format() would read as field names.
    '<style>@media print{.app-backbar{display:none}}</style>'
)


def _wrap(fragment, slug):
    """Turn an Artifact fragment into a standalone page."""
    title = doc_title(fragment)
    body = fragment
    if title:
        body = fragment.replace(f'<title>{title}</title>', '', 1).lstrip('\n')
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>{title or slug}</title>\n'
        '<style>*{margin:0;padding:0}</style>\n</head>\n<body>\n'
        + _BACK_BAR.replace('__PDF__', f'/help/{slug}.pdf')
        + body + '\n</body>\n</html>\n'
    )


def doc_title(fragment):
    start = fragment.find('<title>')
    end = fragment.find('</title>')
    return fragment[start + 7:end] if 0 <= start < end else ''


def _read(slug):
    meta = DOCUMENTS.get(slug)
    if not meta:
        abort(404)
    path = os.path.join(DOCS_DIR, meta['html'])
    if not os.path.exists(path):
        abort(404)
    with open(path, encoding='utf-8') as fh:
        return meta, fh.read()


@bp.route('/help')
@login_required
def help_index():
    available = {
        slug: meta for slug, meta in DOCUMENTS.items()
        if os.path.exists(os.path.join(DOCS_DIR, meta['html']))
    }
    return render_template('help.html', pi_name=PI_NAME, documents=available)


@bp.route('/help/<slug>')
@login_required
def help_doc(slug):
    meta, fragment = _read(slug)
    return Response(_wrap(fragment, slug), mimetype='text/html')


@bp.route('/help/<slug>.pdf')
@login_required
def help_pdf(slug):
    meta = DOCUMENTS.get(slug)
    if not meta:
        abort(404)
    path = os.path.join(DOCS_DIR, meta['pdf'])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype='application/pdf',
                     as_attachment=False, download_name=meta['pdf'])
