#!/usr/bin/env python3
"""Render the review-package Markdown docs to PDF (make docs-pdf).

Markdown -> HTML (python-markdown) -> PDF (weasyprint), landscape letter so
the wide SVG diagrams and rule tables stay readable. Diagrams embed as
VECTORS - they zoom losslessly in the PDF, so keep referencing the .svg
files, never pre-rasterized PNGs.

Deps (not part of the test toolchain):  pip install weasyprint markdown
Usage:  python3 docs/md-to-pdf.py [doc.md | user-manual ...]
        default: docs/ato/architecture.md docs/ato/network-access-controls.md
                 docs/operations/om-runbooks.md docs/operations/cost-controls.md
                 docs/operations/monitoring-and-retention.md
                 docs/ato/conops.md docs/ato/security-review-2026-07-resubmission.md
                 + user-manual (Part I of client-config.md -> user-manual.pdf;
                 the full client-config.md is deliberately not rendered)
Output: docs/generated/<doc>.pdf. Committed alongside the sources -
        regenerate in the same change whenever a doc or diagram changes.
"""

import pathlib
import sys

import markdown
import weasyprint

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "generated"
DEFAULT = [REPO / "docs" / "ato" / "architecture.md",
           REPO / "docs" / "ato" / "network-access-controls.md",
           REPO / "docs" / "operations" / "om-runbooks.md",
           REPO / "docs" / "operations" / "cost-controls.md",
           REPO / "docs" / "operations" / "monitoring-and-retention.md",
           REPO / "docs" / "ato" / "conops.md",
           REPO / "docs" / "ato" / "security-review-2026-07-resubmission.md"]

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


def convert(src: pathlib.Path, text: str | None = None,
            out_name: str | None = None) -> pathlib.Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / ((out_name or src.stem) + ".pdf")
    body = markdown.markdown(
        text if text is not None else src.read_text(),
        extensions=["tables", "fenced_code", "toc"])
    doc = (f"<html><head><meta charset='utf-8'><style>{CSS}</style></head>"
           f"<body>{body}</body></html>")
    weasyprint.HTML(string=doc, base_url=str(src.parent)).write_pdf(str(out))
    print("wrote", out.relative_to(REPO))
    return out


def user_manual() -> pathlib.Path:
    """docs/generated/user-manual.pdf — Part I of client-config.md only,
    extracted between its Part I / Part II H1s so the developer-facing manual
    can be handed out without the administrator reference. The source stays
    one file (no content duplication to drift); regenerate here whenever
    client-config.md changes."""
    src = REPO / "docs" / "operations" / "client-config.md"
    text = src.read_text()
    start = text.index("# Part I — Developer user manual")
    end = text.index("# Part II — Administrators")
    part1 = text[start:end].rstrip().removesuffix("---").rstrip()
    note = ("\n\n---\n\n*This manual is Part I (§1–§5) of "
            "`docs/operations/client-config.md`. References to §6–§9 point "
            "at Part II — Administrators — in the full document; end users "
            "normally don't need it (§8 self-service requires local admin).*\n")
    return convert(src, text=part1 + note, out_name="user-manual")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        for t in DEFAULT:
            convert(t)
        user_manual()
    else:
        for a in args:
            if a == "user-manual":
                user_manual()
            else:
                convert(pathlib.Path(a).resolve())
