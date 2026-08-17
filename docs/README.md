# Documentation

Two documents, written for different readers. Keep them distinct — the whole point
is that they are pitched differently.

## Reading the NOR140

`reading-the-nor140.html` → `Reading the NOR140.pdf` (14 pp)

The practitioner and technical reference. Covers every measurement the meter
records and what each one means, how metric names are constructed, the binary
format and the `raw / 128 - 20` decode, the byte offsets, which figures are
derived and how, the traps that produce plausible-but-wrong numbers, which
standard asks for which metric, and a worked example.

Intended for someone new to doing this work, or for a consultant reviewing the
methodology. **Not** a client hand-out: it contains byte offsets and detector
internals that invite challenge on irrelevancies.

Its final section records the evidence tier behind each class of claim —
measured here and reproducible, checked against public guidance, or
interpretation — plus which sources were and were not read in full.

## Where the Numbers Come From

`where-the-numbers-come-from.html` → `Where the Numbers Come From.pdf` (5 pp)

The client-facing note. Plain language, no internals. Explains what the
instrument measures directly, the four things the app calculates, how far each
figure can be relied on, what is not measured on site, how the figures were
checked, the calibration position, and that the report narrative is AI-drafted.

Deliberately says less. If a client needs more, they should be given the
practitioner document rather than a longer version of this one.

## Regenerating the PDFs

```bash
python3 docs/render_pdf.py docs/*.html
```

Needs headless Chrome (for the print CSS) plus `pypdfium2` and `pypdf` for the
checks. Edit the `.html`, re-run, commit both.

The `.html` files are Artifact fragments: `<title>`, `<style>` and content, with
no `<!doctype>`/`<html>`/`<head>`/`<body>`. That is what the Artifact host
expects, and browsers supply the missing structure, so they open fine directly.
The render script adds the wrapper Chrome needs. The same files can be published
as Artifacts unchanged.

### Why the script checks the output

Two real defects in these documents were invisible to text extraction, so the
checks are not decoration:

- **Overlapping text.** Chrome's print fragmenter can overlay unbreakable blocks
  that do not fit the space remaining on a page. Extracted text reads perfectly
  while the page is unusable. This is why neither document sets
  `break-inside: avoid` on paragraphs — only on genuinely atomic blocks such as
  tables, callouts and figures.
- **Stranded lines.** A paragraph leaving one line alone at the top of the next
  page. Chrome ignores `orphans`/`widows`, so the fix is to shorten the
  paragraph, not to declare it unbreakable.

## Related

`../NOR140_handoff.md` is the reverse-engineering log: binary offsets, how the
decode was derived, the verification history, and the questions still open about
unusual file variants. It is the primary technical record; the two documents here
are written from it.
