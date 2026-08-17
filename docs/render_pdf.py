#!/usr/bin/env python3
"""
Render a documentation page to a print-quality A4 PDF, and check the result.

    python3 docs/render_pdf.py docs/reading-the-nor140.html
    python3 docs/render_pdf.py docs/*.html          # both

The .html files here are Artifact fragments: they carry a <title>, a <style>
block and the page content, but no <!doctype>/<html>/<head>/<body>. That is what
the Artifact host expects, and browsers fill in the missing structure, so they
open fine directly. Chrome's --print-to-pdf needs a complete document, so this
script wraps the fragment before rendering. Editing the .html and re-running this
keeps the PDF in step; the same file can be republished as an Artifact unchanged.

Rendering uses headless Chrome so the print CSS in the page is honoured (page
size, forced light palette, break rules). The checks afterwards exist because
two real defects in these documents were invisible to plain text extraction:

  * overlapping text — Chrome's print fragmenter can overlay unbreakable blocks
    that do not fit the space left on a page. Extracted text looks perfect.
  * stranded lines — a paragraph leaving a single line at the top of a page.

Verification needs `pypdfium2` and `pypdf`; without them the PDF is still
produced and the checks are skipped with a warning.
"""
import os
import re
import subprocess
import sys
import tempfile

CHROME_CANDIDATES = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
]


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit('No Chrome/Chromium found. Install one, or add its path to '
                     'CHROME_CANDIDATES in this script.')


def wrap(fragment_path):
    """Wrap an Artifact fragment into a standalone document Chrome can print."""
    html = open(fragment_path, encoding='utf-8').read()
    m = re.search(r'<title>(.*?)</title>', html)
    if not m:
        raise SystemExit(f'{fragment_path}: no <title> — needed for the PDF name')
    title = m.group(1)
    body = html.replace(m.group(0), '', 1).lstrip('\n')
    return title, (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<title>{title}</title>\n'
        # the Artifact host applies a minimal reset; match it so the PDF matches
        # what the published page looks like
        '<style>*{margin:0;padding:0}</style>\n</head>\n<body>\n'
        f'{body}\n</body>\n</html>\n'
    )


def check(pdf_path):
    """Report page count, overlapping text, and stranded continuation lines."""
    try:
        import pypdfium2 as pdfium
        from pypdf import PdfReader
    except ImportError:
        print('   (checks skipped — pip install pypdfium2 pypdf)')
        return True

    pdf = pdfium.PdfDocument(pdf_path)
    collisions = 0
    for i in range(len(pdf)):
        tp = pdf[i].get_textpage()
        rects = [tp.get_rect(j) for j in range(tp.count_rects())]
        for a in range(len(rects)):
            l1, b1, r1, t1 = rects[a]
            for b in range(a + 1, len(rects)):
                l2, b2, r2, t2 = rects[b]
                v = min(t1, t2) - max(b1, b2)
                h = min(r1, r2) - max(l1, l2)
                if v > 0.35 * min(t1 - b1, t2 - b2) and h > 1.0:
                    collisions += 1

    reader = PdfReader(pdf_path)
    stranded = []
    for i, page in enumerate(reader.pages, 1):
        lines = [x for x in page.extract_text().split('\n') if x.strip()]
        # a page opening on a lowercase word is a paragraph continuation; the
        # formula block in reading-the-nor140 legitimately starts "dB = ..."
        if lines and lines[0].strip()[:1].islower() and not lines[0].startswith('dB ='):
            stranded.append((i, lines[0][:50]))

    print(f'   {len(reader.pages)} pages')
    print(f'   overlapping text rects : {collisions}'
          + ('' if collisions == 0 else '   <-- RENDERING DEFECT'))
    print(f'   stranded lines         : {stranded or "none"}')
    return collisions == 0 and not stranded


def render(fragment_path):
    title, doc = wrap(fragment_path)
    out = os.path.join(os.path.dirname(os.path.abspath(fragment_path)), f'{title}.pdf')
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, 'page.html')
        with open(src, 'w', encoding='utf-8') as fh:
            fh.write(doc)
        subprocess.run(
            [find_chrome(), '--headless', '--disable-gpu', '--no-sandbox',
             '--force-color-profile=srgb', '--no-pdf-header-footer',
             '--virtual-time-budget=5000',
             f'--print-to-pdf={out}', f'file://{src}'],
            check=True, capture_output=True,
        )
    print(f'{os.path.basename(fragment_path)} -> {os.path.basename(out)}')
    return check(out)


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        raise SystemExit(f'Usage: python3 {sys.argv[0]} docs/*.html')
    ok = all(render(a) for a in args)
    print('\nOK' if ok else '\nCHECKS FAILED')
    sys.exit(0 if ok else 1)
