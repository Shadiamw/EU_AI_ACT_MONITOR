"""Report renderers (Phase 5 spec 3.4, D4, D5): the JSON and the PDF of one report dict.

Each is called once, at assembly, on the same in-memory dict (never by re-parsing the JSON), and
the bytes are then stored as produced. A download never renders again.

- render_json: UTF-8, keys in the dict's insertion order (the field order of spec 3.3.1), the
  exact-number writer of app/jsonout.py, no trailing newline.
- render_pdf: reportlab in invariant mode, so the same dict gives the same bytes (a test shows
  it; integrity does not depend on it, because the stored bytes are the record). A
  NOT_SERIOUS closure report carries a "NOT FOR SUBMISSION" banner at the top of every page,
  in a type size larger than the title (spec 3.3.5).

Pure: no database and no clock.
"""

from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app import jsonout

BANNER = "NOT FOR SUBMISSION"
TITLE_SIZE = 15
BANNER_SIZE = 18


def render_json(report: dict[str, Any]) -> bytes:
    return jsonout.dumps(report).encode("utf-8")


def _text(value: Any) -> str:
    """A value as printed: null as a dash, a list joined, an object as compact key=value."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return "; ".join(_text(item) for item in value) if value else "—"
    if isinstance(value, dict):
        return ", ".join(f"{key}={_text(item)}" for key, item in value.items())
    return str(value)


def _p(value: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(_text(value)), style)


def render_pdf(report: dict[str, Any], json_sha256: str | None = None) -> bytes:
    """The PDF of a report. json_sha256, when given, is printed in the provenance block; it is
    the hash of the JSON bytes rendered from the same dict just before."""
    closure = report["submission_marking"] is not None
    sheet = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=sheet["BodyText"], fontSize=9, leading=11)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=9.5)
    title = ParagraphStyle(
        "title", parent=sheet["Title"], fontSize=TITLE_SIZE, leading=TITLE_SIZE + 3
    )
    heading = ParagraphStyle(
        "heading", parent=sheet["Heading2"], fontSize=11, leading=13, spaceBefore=8, spaceAfter=3
    )

    def table(rows: list[list[Any]], widths: list[float], header: bool = False) -> Table:
        data = [[_p(cell, small) for cell in row] for row in rows]
        grid = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        style = [
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
        if header:
            style.append(("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke))
        grid.setStyle(TableStyle(style))
        return grid

    event = report["event"]
    determination = report["determination"]
    stamps = report["timestamps"]
    regulation = report["regulation"]
    anchor = report["ledger_anchor"]
    key_col, value_col = [38 * mm, 132 * mm]

    story: list[Any] = [
        Paragraph(
            escape("Serious incident report: Regulation (EU) 2024/1689, Article 73")
            if not closure
            else escape("Closure report: event assessed as not a serious incident"),
            title,
        ),
        _p(
            "System-produced annex, hash-sealed at assembly. It is not the Commission template "
            "and does not replace the provider's submission.",
            small,
        ),
        Paragraph("Report", heading),
        table(
            [
                ["Report id", report["report_id"]],
                ["Report version", report["report_version"]],
                ["Report kind", report["report_kind"]],
                ["Schema version", report["schema_version"]],
                ["JSON SHA-256", json_sha256 or "(see X-Report-Sha256 on the JSON download)"],
                ["Ledger anchor", f"seq {anchor['seq']}, hash {anchor['hash']}"],
            ],
            [key_col, value_col],
        ),
        Paragraph("Event", heading),
        table(
            [
                ["Event id", event["event_id"]],
                ["System id", event["system_id"]],
                ["Type", event["type"]],
                ["Metric", event["metric_name"]],
                ["Observed value", event["observed_value"]],
                ["Threshold value", event["threshold_value"]],
                ["Detected at", event["detected_at"]],
                ["Evidence SHA-256", event["evidence_sha256"]],
            ],
            [key_col, value_col],
        ),
        Paragraph("Article 3(49) harm fields", heading),
        table(
            [["Field", "Status", "Confirmed by, at", "Evidence"]]
            + [
                [
                    name,
                    field["status"],
                    f"{field['confirmed_by']} {field['confirmed_at']}",
                    field["evidence"],
                ]
                for name, field in report["harm_fields"].items()
            ],
            [56 * mm, 30 * mm, 36 * mm, 48 * mm],
            header=True,
        ),
        Paragraph("Reviewer's determination", heading),
        table(
            [
                ["Tier", f"{determination['tier']} ({determination['tier_label']})"],
                ["Rationale", determination["rationale"]],
                ["Reviewer", determination["reviewer_id"]],
                ["Assigned at", determination["assigned_at"]],
                ["Deadline (UTC)", determination["deadline_utc"]],
                ["Calendar", determination["calendar"]],
                ["Disposition id", determination["disposition_id"]],
            ],
            [key_col, value_col],
        ),
    ]
    if determination["revisions"]:
        story += [
            Spacer(1, 3),
            table(
                [["Prior tier", "New tier", "Reviewer", "Revised at"]]
                + [
                    [r["prior_tier"], r["new_tier"], r["reviewer_id"], r["revised_at"]]
                    for r in determination["revisions"]
                ],
                [50 * mm, 50 * mm, 20 * mm, 50 * mm],
                header=True,
            ),
        ]
    story += [
        Paragraph("Timestamps", heading),
        table(
            [
                ["Detection", stamps["detected_at"]],
                ["Determination", stamps["determined_at"]],
                ["Last revised", stamps["last_revised_at"]],
                ["Report assembly completed", stamps["assembled_at"]],
                [
                    "Detection to assignment (seconds)",
                    report["risk_indicator"]["detection_to_assignment_seconds"],
                ],
            ],
            [key_col + 12 * mm, value_col - 12 * mm],
        ),
        Paragraph("Regulation", heading),
        table(
            [
                ["Instrument", regulation["instrument"]],
                ["Reporting article", regulation["reporting_article"]],
                ["Serious incident definition", regulation["serious_incident_definition"]],
                ["Reporting window (days)", regulation["reporting_window_days"]],
            ],
            [key_col + 12 * mm, value_col - 12 * mm],
        ),
        Paragraph("Article 73 template mapping", heading),
        _p(
            f"{report['article73_template']['template']}, version "
            f"{report['article73_template']['template_version']} "
            f"({report['article73_template']['template_reference']}). Fields marked "
            "requires_reviewer_input or not_available are listed by number and name and are "
            "never guessed. Dates are given in the template's YYYY-MM-DD format as the first "
            "ten characters of the full timestamp.",
            small,
        ),
        Spacer(1, 3),
        table(
            [["Field", "Name", "Status", "Value"]]
            + [
                [f["field"], f["name"], f["status"], f["value"]]
                for f in report["article73_template"]["fields"]
            ],
            [22 * mm, 64 * mm, 34 * mm, 50 * mm],
            header=True,
        ),
    ]

    def decorate(canvas: Any, document: Any) -> None:
        canvas.saveState()
        if closure:
            canvas.setFont("Helvetica-Bold", BANNER_SIZE)
            canvas.setFillColor(colors.red)
            canvas.drawCentredString(A4[0] / 2, A4[1] - 12 * mm, BANNER)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(20 * mm, 10 * mm, f"{report['report_id']} v{report['report_version']}")
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"page {document.page}")
        canvas.restoreState()

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=18 * mm,
        title=f"Report {report['report_id']}",
        author="EU AI Act monitor",
        invariant=1,
    )
    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()
