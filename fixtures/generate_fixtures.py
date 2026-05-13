#!/usr/bin/env python3
"""
One-time builder for the sample waiver PDFs used by waiver_parser tests.

This script is NOT a runtime dependency — it regenerates the three fixture
PDFs that live in this directory. The generated PDFs are checked in, so
production runs do not need reportlab installed.

Re-run when the canonical fixture shape changes:
    pip install reportlab
    python fixtures/generate_fixtures.py
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FIXTURES_DIR = Path(__file__).parent


def _build_pdf(filename: str, rows: list[list[str]]) -> None:
    """A WFR-style report: title + meta line + one table with header."""
    doc = SimpleDocTemplate(str(FIXTURES_DIR / filename), pagesize=letter)
    styles = getSampleStyleSheet()
    header = ["Camper Name", "Date Completed"]
    table_data = [header] + rows

    story = [
        Paragraph("WFR Waiver Completion Report", styles["Title"]),
        Paragraph("Generated: 2026-05-13 — Mannahouse Youth Camp 2026", styles["Normal"]),
        Spacer(1, 18),
        Table(table_data, hAlign="LEFT", colWidths=[260, 140]),
    ]
    story[-1].setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    doc.build(story)


def main() -> None:
    _build_pdf("sample_waiver_simple.pdf", [
        ["John Smith",      "2026-05-10"],
        ["Sarah Johnson",   "2026-05-11"],
        ["Marcus Williams", "2026-05-11"],
        ["Emma Davis",      "2026-05-12"],
        ["Liam Brown",      "2026-05-12"],
    ])
    _build_pdf("sample_waiver_with_edge_cases.pdf", [
        ["O'Brien, Mary-Kate",   "2026-05-10"],   # apostrophe + hyphen + Last, First
        ["John A. Smith Jr.",    "2026-05-11"],   # middle initial + suffix
        ["Garcia-Lopez, Diego",  "2026-05-11"],   # hyphenated last, Last, First
        ["Dr. Rachel Patel",     "2026-05-12"],   # honorific
        ["Anne-Marie Smith",     "2026-05-12"],   # hyphenated first
    ])
    _build_pdf("sample_waiver_empty.pdf", [])
    print(f"Wrote 3 fixtures to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
