#!/usr/bin/env python3
"""render-report-html.py — Render the EEVDF deep-dive report to a single HTML file.

Minimal stdlib-only Markdown -> HTML converter scoped to the constructs used in
`research/DEEP-DIVE-EEVDF-EXEC.md` (ATX headings, pipe tables, bullet lists,
images, bold, inline code, paragraphs).  The HTML keeps the report's RELATIVE
image paths, so opening it from `research/` in a browser resolves the PNGs and
plays the animated GIFs.

Usage:
    render-report-html.py <report.md> <out.html>
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

CSS = """
<style>
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
         max-width: 960px; margin: 2rem auto; padding: 0 1rem;
         color: #1f2328; line-height: 1.55; }
  h1 { border-bottom: 1px solid #d0d7de; padding-bottom: .3rem; }
  h2 { border-bottom: 1px solid #d0d7de; padding-bottom: .3rem; margin-top: 2rem; }
  h3 { margin-top: 1.5rem; }
  table { border-collapse: collapse; margin: 1rem 0; width: 100%; }
  th, td { border: 1px solid #d0d7de; padding: .35rem .6rem; font-size: .9rem;
           text-align: left; }
  th { background: #f6f8fa; }
  tr:nth-child(even) td { background: #fafbfc; }
  img { max-width: 100%; height: auto; display: block; margin: .6rem 0;
        border: 1px solid #d0d7de; border-radius: 4px; }
  code { background: #f6f8fa; border-radius: 4px; padding: .1em .35em;
         font-size: .88em; }
  li { margin: .25rem 0; }
  p { margin: .5rem 0; }
</style>
"""


def inline(text: str) -> str:
    """Apply inline markdown: code spans, images, bold. Returns HTML."""
    parts: list[str] = []
    # Split on backticks; odd segments are code, even are prose.
    segments = text.split("`")
    for i, seg in enumerate(segments):
        if i % 2 == 1:
            parts.append(f"<code>{html.escape(seg)}</code>")
            continue
        esc = html.escape(seg)
        esc = re.sub(r"!\[([^\]]*)\]\(([^)]*)\)", r'<img src="\2" alt="\1">', esc)
        esc = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", esc)
        parts.append(esc)
    return "".join(parts)


def render(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    list_open = False
    table: list[str] = []
    para: list[str] = []

    def close_para() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    def close_table() -> None:
        if not table:
            return
        header = table[0]
        body = (
            table[2:]
            if len(table) > 1
            and set(table[1].replace("|", "").replace("-", "").replace(":", "").strip())
            == set()
            else table[1:]
        )
        out.append(
            "<table><thead><tr>"
            + "".join(
                f"<th>{inline(c.strip())}</th>" for c in header.strip("|").split("|")
            )
            + "</tr></thead>"
        )
        if body:
            out.append("<tbody>")
            for row in body:
                cells = [inline(c.strip()) for c in row.strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            out.append("</tbody>")
        out.append("</table>")
        table.clear()

    def flush() -> None:
        close_para()
        close_list()
        close_table()

    for line in lines:
        if not line.strip():
            flush()
            continue
        if line.startswith("```"):
            flush()
            out.append("<pre><code>" + html.escape(line.strip("`")) + "</code></pre>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            continue
        if line.startswith("|"):
            flush()
            table.append(line)
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            close_para()
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            continue
        # Plain paragraph line.
        close_list()
        close_table()
        para.append(line.strip())

    flush()
    return f'<!doctype html><html><head><meta charset="utf-8"><title>EEVDF deep dive</title>{CSS}</head><body>{"".join(out)}</body></html>\n'


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 2:
        print("usage: render-report-html.py <report.md> <out.html>", file=sys.stderr)
        return 2
    md_path = Path(args[0])
    out_path = Path(args[1])
    if not md_path.is_file():
        print(f"error: report not found: {md_path}", file=sys.stderr)
        return 1
    out_path.write_text(render(md_path.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
