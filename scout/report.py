"""
Professional PDF report.

Renders the ranked shortlist as a presentation-grade PDF: an exploration summary
(how many sources/links/platforms were explored), then a per-candidate section with
the "why we chose them" verdict, the 12-metric scorecard with evidence, notable work,
tech stack, every resolved profile + published link, and contacts.

Pure-Python via reportlab (no system dependencies).
"""

from __future__ import annotations

import html
import unicodedata
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlmodel import select

from .config import PROJECT_ROOT, load_icp
from .db import session_scope
from .models import Candidate, Contact, Dossier, Identity, Scorecard
from .scoring.scorecard import METRICS, PILLAR_ORDER

PLATFORM_LABELS = {
    "github": "GitHub", "x": "X", "linkedin": "LinkedIn", "huggingface": "Hugging Face",
    "kaggle": "Kaggle", "scholar": "Google Scholar", "stackoverflow": "Stack Overflow",
    "codeforces": "Codeforces", "reddit": "Reddit", "website": "Website",
}
REC_COLORS = {
    "Strong Yes": colors.HexColor("#0a7f3f"), "Yes": colors.HexColor("#2e7d32"),
    "Maybe": colors.HexColor("#b26a00"), "No": colors.HexColor("#9e2b2b"),
}
INK = colors.HexColor("#1b2430")
MUTED = colors.HexColor("#667085")
ACCENT = colors.HexColor("#2f5fdf")
LINE = colors.HexColor("#dfe3ea")


def _pdf_safe(s: str) -> str:
    """Normalize fancy unicode (e.g. mathematical-bold letters) to plain text and drop
    emoji / symbols / control chars that the PDF font can't render."""
    s = unicodedata.normalize("NFKC", s)
    out = []
    for ch in s:
        if ch in "\u200d\ufe0f\u200b":  # ZWJ, variation selector, zero-width space
            continue
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("So", "Sk", "Cs"):
            continue
        out.append(ch)
    return "".join(out).strip()


def _esc(x) -> str:
    return html.escape(_pdf_safe(str(x))) if x is not None else ""


def _styles() -> dict:
    base = getSampleStyleSheet()
    S = {}
    S["title"] = ParagraphStyle("title", parent=base["Title"], fontSize=20, textColor=INK, spaceAfter=2)
    S["sub"] = ParagraphStyle("sub", parent=base["Normal"], fontSize=9, textColor=MUTED, spaceAfter=10)
    S["h2"] = ParagraphStyle("h2", parent=base["Heading2"], fontSize=13, textColor=INK, spaceBefore=6, spaceAfter=2)
    S["name"] = ParagraphStyle("name", parent=base["Heading2"], fontSize=14, textColor=INK, spaceAfter=0)
    S["meta"] = ParagraphStyle("meta", parent=base["Normal"], fontSize=8.5, textColor=MUTED, spaceAfter=4)
    S["body"] = ParagraphStyle("body", parent=base["Normal"], fontSize=9.5, textColor=INK, leading=13)
    S["why"] = ParagraphStyle("why", parent=base["Normal"], fontSize=9.5, textColor=INK, leading=13, spaceBefore=2)
    S["small"] = ParagraphStyle("small", parent=base["Normal"], fontSize=8, textColor=MUTED, leading=11)
    S["cell"] = ParagraphStyle("cell", parent=base["Normal"], fontSize=8, textColor=INK, leading=10)
    S["lab"] = ParagraphStyle("lab", parent=base["Normal"], fontSize=7.5, textColor=MUTED, alignment=TA_LEFT)
    S["sectlab"] = ParagraphStyle("sectlab", parent=base["Normal"], fontSize=8, textColor=ACCENT, spaceBefore=4, spaceAfter=1)
    return S


def _link(url: str, label: str | None = None) -> str:
    u = _esc(url)
    return f'<a href="{u}" color="#2f5fdf">{_esc(label or url)}</a>'


def _summary_flowables(n: int, stats: dict, S: dict) -> list:
    plats = sorted(stats["platforms"])
    plat_names = ", ".join(PLATFORM_LABELS.get(p, p) for p in plats) or "-"
    tiles = [
        ("Candidates", str(n)), ("Sources reviewed", str(stats["sources"])),
        ("Published links", str(stats["links"])), ("Profiles resolved", str(len(plats))),
        ("Contacts found", str(stats["contacts"])),
    ]
    num_style = ParagraphStyle("num", parent=S["body"], fontSize=16, textColor=ACCENT, alignment=1)
    lab_style = ParagraphStyle("tlab", parent=S["small"], alignment=1)
    cells = [[Paragraph(v, num_style) for _l, v in tiles], [Paragraph(l, lab_style) for l, _v in tiles]]
    width = (A4[0] - 40 * mm) / len(tiles)
    tbl = Table(cells, colWidths=[width] * len(tiles))
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    note = Paragraph(
        f"The agent explored <b>{stats['sources']}</b> public sources across <b>{len(plats)}</b> "
        f"platforms ({_esc(plat_names)}) to verify and profile these candidates, and found contact "
        f"details for <b>{stats['with_contact']}</b> of {n}.",
        S["small"],
    )
    return [tbl, Spacer(1, 5), note, Spacer(1, 12)]


def _bullets(items: list, S: dict, limit: int = 8) -> list:
    items = [i for i in (items or []) if i][:limit]
    if not items:
        return []
    return [ListFlowable([ListItem(Paragraph(_esc(x), S["cell"]), leftIndent=8) for x in items],
                          bulletType="bullet", start="circle", leftIndent=10)]


def _candidate_flowables(rank, c, sc, dossier, idents, contacts, S) -> list:
    st = (dossier.structured if dossier else {}) or {}
    meta = c.meta or {}
    rec = (sc.recommendation if sc else None) or "-"
    comp = sc.composite if sc else (c.composite_score or 0.0)
    conf = sc.confidence if sc else 0.0
    rc = REC_COLORS.get(rec, MUTED)

    flow = [HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=8, spaceAfter=8)]
    # header row: name + score badge
    name = f"#{rank}  {_esc(c.name or c.primary_github_login)}"
    badge = f'<b><font color="#{rc.hexval()[2:]}">{_esc(rec)}</font></b>'
    header_tbl = Table(
        [[Paragraph(name, S["name"]),
          Paragraph(f'<b>{comp:.2f}</b>  {badge}<br/><font size="7" color="#667085">confidence {round(conf*100)}%</font>',
                    ParagraphStyle("r", parent=S["body"], alignment=2))]],
        colWidths=[(A4[0] - 40 * mm) * 0.72, (A4[0] - 40 * mm) * 0.28],
    )
    header_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    flow.append(header_tbl)

    sub = " · ".join(filter(None, [
        _esc(c.headline or st.get("role")), _esc(c.location),
        _esc(st.get("seniority") or meta.get("seniority")),
        (f"@ {_esc(st.get('company') or meta.get('company'))}" if (st.get("company") or meta.get("company")) else ""),
    ]))
    if sub:
        flow.append(Paragraph(sub, S["meta"]))
    if dossier and dossier.summary:
        flow.append(Paragraph(_esc(dossier.summary), S["body"]))
    if sc and sc.narrative:
        flow.append(Paragraph(f'<b>Why we chose them:</b> {_esc(sc.narrative)}', S["why"]))

    achievements = st.get("key_achievements") or st.get("notable_work") or []
    if achievements:
        flow.append(Paragraph("KEY ACHIEVEMENTS", S["sectlab"]))
        flow += _bullets(achievements, S, limit=6)

    # Pillars row
    if sc and sc.pillars:
        prow = [[Paragraph(f"<b>{(sc.pillars.get(p,0)):.2f}</b>", S["cell"]) for p in PILLAR_ORDER],
                [Paragraph(_esc(p), S["lab"]) for p in PILLAR_ORDER]]
        w = (A4[0] - 40 * mm) / len(PILLAR_ORDER)
        pt = Table(prow, colWidths=[w] * len(PILLAR_ORDER))
        pt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        flow.append(Spacer(1, 4))
        flow.append(pt)

    # Metrics table
    if sc and sc.metrics:
        rows = [[Paragraph("<b>Metric</b>", S["cell"]), Paragraph("<b>Score</b>", S["cell"]),
                 Paragraph("<b>Conf</b>", S["cell"]), Paragraph("<b>Evidence</b>", S["cell"])]]
        for m, (_pillar, _w, _d) in METRICS.items():
            cell = sc.metrics.get(m, {})
            rows.append([
                Paragraph(_esc(m.replace("_", " ")), S["cell"]),
                Paragraph(f"{float(cell.get('score',0)):.2f}", S["cell"]),
                Paragraph(f"{round(float(cell.get('confidence',0))*100)}%", S["cell"]),
                Paragraph(_esc(cell.get("evidence", "")), S["cell"]),
            ])
        usable = A4[0] - 40 * mm
        mt = Table(rows, colWidths=[usable * 0.22, usable * 0.10, usable * 0.10, usable * 0.58], repeatRows=1)
        mt.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.4, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.3, LINE),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f5fb")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        flow.append(Spacer(1, 5))
        flow.append(mt)

    # Only show NOTABLE WORK separately if KEY ACHIEVEMENTS used a distinct source.
    notable = st.get("notable_work") or []
    if notable and st.get("key_achievements"):
        flow.append(Paragraph("NOTABLE WORK", S["sectlab"]))
        flow += _bullets(notable, S)
    hooks = st.get("highlights") or []
    if hooks:
        flow.append(Paragraph("OUTREACH HOOKS", S["sectlab"]))
        flow += _bullets(hooks, S)
    tech = st.get("tech_stack") or []
    if tech:
        flow.append(Paragraph("TECH STACK", S["sectlab"]))
        flow.append(Paragraph(_esc(", ".join(tech[:24])), S["cell"]))

    # Profiles + published links
    prof_links = [(_link(i.url, PLATFORM_LABELS.get(i.platform.value, i.platform.value))) for i in idents if i.url]
    inv = st.get("links_inventory") or {}
    extra = []
    for cat in ["code", "papers", "writing", "talks", "social", "other"]:
        for u in (inv.get(cat) or [])[:6]:
            extra.append(_link(u))
    all_links = prof_links + extra
    if all_links:
        flow.append(Paragraph("PROFILES &amp; PUBLISHED LINKS", S["sectlab"]))
        flow.append(Paragraph(" &nbsp;·&nbsp; ".join(all_links[:30]), S["small"]))

    if contacts:
        flow.append(Paragraph("CONTACT", S["sectlab"]))
        flow.append(Paragraph(
            " &nbsp; ".join(f"{_esc(ct.value)} <font color='#667085'>({_esc(ct.source)})</font>" for ct in contacts),
            S["cell"]))

    flow.append(Paragraph(f'<font color="#667085">{len(dossier.citations) if dossier else 0} sources reviewed</font>', S["small"]))
    return [KeepTogether(flow[:6])] + flow[6:]


def build_report(top: int = 25, out_path: str | None = None) -> str:
    icp = load_icp()
    S = _styles()
    story: list = []

    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    story.append(Paragraph("Talent Scout - Candidate Shortlist", S["title"]))
    story.append(Paragraph(
        f"{_esc(icp.get('role'))} &nbsp;·&nbsp; target geo: {_esc(icp.get('geo',{}).get('country'))} "
        f"&nbsp;·&nbsp; generated {generated}", S["sub"]))

    with session_scope() as session:
        cands = session.exec(
            select(Candidate).where(Candidate.composite_score.is_not(None))
            .order_by(Candidate.composite_score.desc()).limit(top)
        ).all()
        stats = {"sources": 0, "links": 0, "contacts": 0, "with_contact": 0, "platforms": set()}
        cand_flows = []
        for i, c in enumerate(cands, 1):
            sc = session.exec(select(Scorecard).where(Scorecard.candidate_id == c.id)
                              .order_by(Scorecard.created_at.desc())).first()
            dossier = session.exec(select(Dossier).where(Dossier.candidate_id == c.id)
                                   .order_by(Dossier.generated_at.desc())).first()
            idents = session.exec(select(Identity).where(Identity.candidate_id == c.id)).all()
            contacts = session.exec(select(Contact).where(Contact.candidate_id == c.id)).all()

            stats["sources"] += len(dossier.citations) if dossier else 0
            stats["contacts"] += len(contacts)
            stats["with_contact"] += 1 if contacts else 0
            for ident in idents:
                stats["platforms"].add(ident.platform.value)
            if dossier and dossier.structured:
                for urls in (dossier.structured.get("links_inventory") or {}).values():
                    stats["links"] += len(urls or [])

            cand_flows.append(_candidate_flowables(i, c, sc, dossier, idents, contacts, S))

        story += _summary_flowables(len(cands), stats, S)
        if not cands:
            story.append(Paragraph("No scored candidates yet.", S["body"]))
        for cf in cand_flows:
            story += cf

    if out_path is None:
        out_dir = PROJECT_ROOT / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"talent_report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf")

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=16 * mm,
        title="Talent Scout - Candidate Shortlist",
    )
    doc.build(story)
    return out_path
