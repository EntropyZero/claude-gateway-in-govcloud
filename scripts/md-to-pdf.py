#!/usr/bin/env python3
"""Render the review-package Markdown docs to PDF (make docs-pdf).

Markdown -> HTML (python-markdown) -> PDF (weasyprint), landscape letter so
the wide SVG diagrams and rule tables stay readable. Diagrams embed as
VECTORS - they zoom losslessly in the PDF, so keep referencing the .svg
files, never pre-rasterized PNGs.

Deps (not part of the test toolchain):  pip install weasyprint markdown
Usage:  python3 scripts/md-to-pdf.py [doc.md ...]
        default: docs/architecture.md docs/network-access-controls.md
                 docs/om-runbooks.md
Output: <doc>.pdf next to each source. Committed alongside the sources -
        regenerate in the same change whenever a doc or diagram changes.
"""

import pathlib
import sys

import markdown
import weasyprint

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = [REPO / "docs" / "architecture.md",
           REPO / "docs" / "network-access-controls.md",
           REPO / "docs" / "om-runbooks.md"]

CSS = """
@page { size: letter landscape; margin: 14mm 12mm 16mm 12mm;
        @bottom-center { content: counter(page) " / " counter(pages);
                         font: 8pt Helvetica; color: #64748B; } }
body { font: 10pt/1.45 Helvetica, Arial, sans-serif; color: #1e293b; }
h1 { font-size: 17pt; color: #0F172A; border-bottom: 2px solid #CBD5E1;
     padding-bottom: 4pt; }
h2 { font-size: 13pt; color: #0F172A; margin-top: 14pt;
     page-break-after: avoid; }
img { max-width: 100%; page-break-inside: avoid; margin: 6pt 0; }
table { border-collapse: collapse; font-size: 8.5pt; margin: 8pt 0;
        page-break-inside: avoid; }
th, td { border: 0.6pt solid #CBD5E1; padding: 3pt 6pt; text-align: left;
         vertical-align: top; }
th { background: #F1F5F9; }
code { font: 8.5pt "Courier New", monospace; background: #F1F5F9;
       padding: 0 2pt; }
pre { background: #F8FAFC; border: 0.6pt solid #E2E8F0; padding: 6pt;
      font-size: 8pt; white-space: pre-wrap; }
blockquote { border-left: 3pt solid #CBD5E1; margin-left: 0;
             padding-left: 10pt; color: #475569; }
a { color: #2563EB; text-decoration: none; }
"""


def convert(src: pathlib.Path) -> pathlib.Path:
    out = src.with_suffix(".pdf")
    body = markdown.markdown(
        src.read_text(), extensions=["tables", "fenced_code", "toc"])
    doc = (f"<html><head><meta charset='utf-8'><style>{CSS}</style></head>"
           f"<body>{body}</body></html>")
    weasyprint.HTML(string=doc, base_url=str(src.parent)).write_pdf(str(out))
    print("wrote", out.relative_to(REPO))
    return out


if __name__ == "__main__":
    targets = [pathlib.Path(a).resolve() for a in sys.argv[1:]] or DEFAULT
    for t in targets:
        convert(t)
