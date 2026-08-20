"""
Allowlist sanitizer for the model-generated HTML that report sections are made of.

Why this exists (WP13 / Codex F6): the six report sections are HTML written by
Claude and rendered with `| safe`. The prompt that produces them embeds
user-controlled text — session notes, location, recorder name, assessment
metadata — so a prompt injection, or a hostile row arriving over the peer sync,
could put executable HTML into a stored report. Reports replicate between the
two Pis, so that is cross-machine stored XSS in an evidence document. Every
section is passed through here before it is stored and again before it is
rendered, so legacy rows and replicated rows are covered too.

Policy:
  * A small allowlist of structural tags survives. Anything else is dropped but
    its text content is kept — a stray <div class=x> loses the tag, not the
    sentence inside it.
  * script / style / iframe / object / embed are dropped *with* their content:
    what is inside them is code or styling, never report prose.
  * EVERY attribute is stripped, except colspan/rowspan (small positive
    integers) on table cells. No class, no style, no id, and therefore no
    event handlers, no javascript: URLs and no CSS injection. The report
    stylesheet targets element names for prose (.prose p, .prose table, …), so
    nothing legitimate needs an attribute.
  * Comments, processing instructions and doctypes are dropped.
  * Character and entity references are re-emitted verbatim, so `&nbsp;`,
    `&amp;` and `&#8211;` come out of an evidence document as they went in.
  * Unclosed and mis-nested tags are tolerated (the parser closes what is
    still open at the end); nothing here raises. A parser failure falls back
    to html.escape() of the input — the report reads badly but cannot bite.

stdlib only (html.parser): the Pis install from requirements.txt over a
domestic uplink and this is not worth a dependency.
"""
import logging
from html import escape
from html.parser import HTMLParser

log = logging.getLogger('noise.sanitize')

# Structural prose and tables. Deliberately no <a> (a link is an attribute
# carrier and a report cites its sources in text), no <img>, no <form>.
ALLOWED_TAGS = frozenset({
    'p', 'br', 'ul', 'ol', 'li',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'h1', 'h2', 'h3', 'h4',
    'strong', 'em', 'b', 'i', 'u', 'span', 'div',
    'blockquote', 'code', 'pre',
})

# Emitted without a closing tag, and their end tags ignored.
VOID_TAGS = frozenset({'br'})

# Dropped along with everything inside them.
DROPPED_SUBTREES = frozenset({'script', 'style', 'iframe', 'object', 'embed'})

_CELL_TAGS = frozenset({'th', 'td'})
_CELL_ATTRS = ('colspan', 'rowspan')     # ordered, so output is deterministic
_MAX_SPAN = 100                          # a report table, not a spreadsheet


def _cell_attrs(tag, attrs):
    """The only attributes that survive: colspan/rowspan on a table cell,
    and only when they are a small positive integer. Re-emitted from the
    parsed integer, so no attacker-controlled text reaches the output."""
    if tag not in _CELL_TAGS:
        return ''
    got = {}
    for name, value in attrs:
        name = (name or '').lower()
        if name in _CELL_ATTRS and name not in got and value is not None:
            v = str(value).strip()
            if v.isdigit() and 0 < int(v) <= _MAX_SPAN:
                got[name] = int(v)
    return ''.join(' %s="%d"' % (n, got[n]) for n in _CELL_ATTRS if n in got)


class _Sanitizer(HTMLParser):
    def __init__(self):
        # convert_charrefs=False so entity references arrive as themselves and
        # can be re-emitted unchanged rather than decoded and re-escaped.
        super().__init__(convert_charrefs=False)
        self._out = []
        self._open = []          # allowed, non-void tags still open
        self._drop = 0           # nesting depth inside a dropped subtree
        self._drop_tag = None

    # ── tags ────────────────────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        tag = (tag or '').lower()
        if self._drop:
            if tag == self._drop_tag:
                self._drop += 1
            return
        if tag in DROPPED_SUBTREES:
            self._drop, self._drop_tag = 1, tag
            return
        if tag not in ALLOWED_TAGS:
            return               # tag dropped, its children and text kept
        self._out.append('<%s%s>' % (tag, _cell_attrs(tag, attrs)))
        if tag not in VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = (tag or '').lower()
        if self._drop or tag in DROPPED_SUBTREES or tag not in ALLOWED_TAGS:
            return
        self._out.append('<%s%s>' % (tag, _cell_attrs(tag, attrs)))
        if tag not in VOID_TAGS:
            self._out.append('</%s>' % tag)

    def handle_endtag(self, tag):
        tag = (tag or '').lower()
        if self._drop:
            if tag == self._drop_tag:
                self._drop -= 1
                if not self._drop:
                    self._drop_tag = None
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS or tag not in self._open:
            return               # stray or unopened end tag: ignore it
        # Mis-nesting (<b><i></b>) closes the inner tags too, so the output
        # stays a well-formed tree.
        while self._open:
            open_tag = self._open.pop()
            self._out.append('</%s>' % open_tag)
            if open_tag == tag:
                break

    # ── text ────────────────────────────────────────────────────────────────
    def handle_data(self, data):
        if self._drop:
            return
        self._out.append(escape(data, quote=False))

    def handle_entityref(self, name):
        if not self._drop:
            self._out.append('&%s;' % name)

    def handle_charref(self, name):
        if not self._drop:
            self._out.append('&#%s;' % name)

    # ── everything else is dropped ──────────────────────────────────────────
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def unknown_decl(self, data):
        pass

    def handle_pi(self, data):
        pass

    def result(self):
        """Close whatever the model left open, then hand back the HTML."""
        while self._open:
            self._out.append('</%s>' % self._open.pop())
        return ''.join(self._out)


def sanitize_html(value):
    """Return `value` reduced to the allowlist above. Never raises.

    A non-string is stringified; None becomes ''. If the parser fails for any
    reason the whole input is html.escape()d instead — the section then shows
    its own markup as text, which is ugly and obviously wrong, rather than
    executing."""
    if value is None:
        return ''
    if not isinstance(value, str):
        value = value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value)
    try:
        parser = _Sanitizer()
        parser.feed(value)
        parser.close()
        return parser.result()
    except Exception:            # pragma: no cover — defensive, see docstring
        log.exception('HTML sanitizer failed; falling back to full escaping')
        return escape(value)


def sanitize_sections(sections):
    """Sanitize every value of a report's sections dict (the six HTML blobs).

    Anything that is not a dict is handed back untouched — a malformed stored
    row is view_report's problem, not the sanitizer's."""
    if not isinstance(sections, dict):
        return sections
    return {k: sanitize_html(v) for k, v in sections.items()}
