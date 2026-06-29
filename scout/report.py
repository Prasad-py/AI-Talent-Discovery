"""
Professional self-contained HTML report.

Renders the ranked shortlist as a clean, offline, presentation-grade document:
recommendation pills, pillar + metric meters (with confidence), the 360 narrative,
notable work, tech stack, a full inventory of published links, contacts, and a
"why we chose them" criteria block per candidate. No external assets.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from sqlmodel import select

from .config import PROJECT_ROOT, load_icp
from .db import session_scope
from .models import Candidate, Contact, Dossier, Identity, Scorecard
from .scoring.scorecard import METRICS

REC_COLORS = {"Strong Yes": "#0a7f3f", "Yes": "#2e7d32", "Maybe": "#b26a00", "No": "#9e2b2b"}


def _esc(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def _meter(label: str, value: float, conf: float | None = None, sub: str = "") -> str:
    pct = max(0, min(100, round(value * 100)))
    conf_txt = f"<span class='conf'>conf {round((conf or 0)*100)}%</span>" if conf is not None else ""
    return (
        f"<div class='meter'><div class='meter-head'><span class='mlabel'>{_esc(label)}</span>"
        f"<span class='mval'>{value:.2f} {conf_txt}</span></div>"
        f"<div class='bar'><div class='fill' style='width:{pct}%'></div></div>"
        f"{('<div class=evidence>'+_esc(sub)+'</div>') if sub else ''}</div>"
    )


def _chips(urls: list[str], cls: str = "chip") -> str:
    out = []
    for u in urls or []:
        if not u:
            continue
        label = u.replace("https://", "").replace("http://", "")[:48]
        out.append(f"<a class='{cls}' href='{_esc(u)}' target='_blank'>{_esc(label)}</a>")
    return "".join(out)


def _candidate_card(rank: int, c: Candidate, sc: Scorecard | None, dossier: Dossier | None,
                    identities: list[Identity], contacts: list[Contact]) -> str:
    st = (dossier.structured if dossier else {}) or {}
    rec = (sc.recommendation if sc else None) or "—"
    rec_color = REC_COLORS.get(rec, "#555")
    comp = sc.composite if sc else (c.composite_score or 0.0)
    conf = sc.confidence if sc else 0.0

    # Pillars
    pillar_html = "".join(_meter(p, v) for p, v in (sc.pillars.items() if sc else []))

    # Metrics (grouped)
    metric_rows = ""
    if sc and sc.metrics:
        for m, (pillar, _w, _d) in METRICS.items():
            cell = sc.metrics.get(m, {})
            metric_rows += _meter(m.replace("_", " "), cell.get("score", 0.0), cell.get("confidence"), cell.get("evidence", ""))

    # Links inventory
    links = st.get("links_inventory") or {}
    links_html = ""
    for cat in ["profiles", "code", "papers", "writing", "talks", "social", "other"]:
        urls = links.get(cat) or []
        if urls:
            links_html += f"<div class='linkgroup'><span class='lcat'>{cat}</span>{_chips(urls)}</div>"

    # Identities
    ident_html = _chips([i.url for i in identities if i.url], cls="chip ident")

    # Contacts
    contact_html = " ".join(
        f"<span class='contact'>{_esc(ct.value)} <em>({_esc(ct.source)})</em></span>" for ct in contacts
    ) or "<span class='muted'>none found</span>"

    notable = "".join(f"<li>{_esc(x)}</li>" for x in (st.get("notable_work") or [])[:8])
    highlights = "".join(f"<li>{_esc(x)}</li>" for x in (st.get("highlights") or [])[:8])
    tech = " ".join(f"<span class='tag'>{_esc(t)}</span>" for t in (st.get("tech_stack") or [])[:18])
    red_flags = "".join(f"<li>{_esc(x)}</li>" for x in (st.get("red_flags") or [])[:5])

    meta = c.meta or {}
    sub = " · ".join(filter(None, [
        _esc(c.location), _esc(st.get("seniority") or meta.get("seniority")),
        _esc(st.get("career_stage") or meta.get("career_stage")),
        (f"company: {_esc(st.get('company') or meta.get('company'))}" if (st.get('company') or meta.get('company')) else ""),
    ]))

    return f"""
    <section class='card'>
      <div class='card-head'>
        <div class='rank'>#{rank}</div>
        <div class='who'>
          <h2>{_esc(c.name or c.primary_github_login)}</h2>
          <div class='headline'>{_esc(c.headline or st.get('role') or '')}</div>
          <div class='submeta'>{sub}</div>
        </div>
        <div class='scorebox'>
          <div class='composite'>{comp:.2f}</div>
          <div class='rec' style='background:{rec_color}'>{_esc(rec)}</div>
          <div class='conf small'>confidence {round(conf*100)}%</div>
        </div>
      </div>

      <p class='summary'>{_esc(dossier.summary if dossier else '')}</p>

      <div class='cols'>
        <div class='col'>
          <h3>Why this score</h3>
          <p class='narrative'>{_esc(sc.narrative if sc else '')}</p>
          <h3>Pillars</h3>
          {pillar_html or "<span class='muted'>not scored</span>"}
          <details><summary>All 12 metrics</summary>{metric_rows}</details>
        </div>
        <div class='col'>
          <h3>Notable work</h3><ul>{notable or '<li class=muted>—</li>'}</ul>
          <h3>Outreach hooks</h3><ul>{highlights or '<li class=muted>—</li>'}</ul>
          <div class='tags'>{tech}</div>
          {("<h3>Red flags</h3><ul>"+red_flags+"</ul>") if red_flags else ""}
        </div>
      </div>

      <div class='footer-row'>
        <div><h4>Profiles</h4>{ident_html or "<span class='muted'>—</span>"}</div>
      </div>
      {("<div class='linksinv'><h4>Published links</h4>"+links_html+"</div>") if links_html else ""}
      <div class='footer-row'>
        <div><h4>Contact</h4>{contact_html}</div>
        <div class='srccount'>{len(dossier.citations) if dossier else 0} sources</div>
      </div>
    </section>
    """


CSS = """
:root{--bg:#0f1115;--card:#171a21;--ink:#e7e9ee;--muted:#8a92a3;--accent:#4f8cff;--line:#252a34;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 80px;}
header.top{border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:26px;}
header.top h1{margin:0 0 6px;font-size:26px;letter-spacing:-.2px;}
header.top .sub{color:var(--muted);font-size:14px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:22px;}
.card-head{display:flex;gap:16px;align-items:flex-start;}
.rank{font-size:22px;font-weight:700;color:var(--accent);min-width:42px;}
.who h2{margin:0;font-size:20px;}
.who .headline{color:var(--ink);opacity:.9;}
.who .submeta{color:var(--muted);font-size:13px;margin-top:2px;}
.scorebox{margin-left:auto;text-align:right;}
.composite{font-size:30px;font-weight:800;line-height:1;}
.rec{display:inline-block;color:#fff;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;margin-top:6px;}
.conf{color:var(--muted);font-size:11px;}
.small{font-size:11px;color:var(--muted);}
.summary{color:#cfd3dc;margin:14px 0 6px;}
.cols{display:flex;gap:24px;margin-top:8px;}
.col{flex:1;min-width:0;}
h3{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:16px 0 8px;}
h4{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 0 6px;}
.narrative{color:#cfd3dc;font-size:14px;margin:0;}
ul{margin:0;padding-left:18px;}li{margin:2px 0;}
.meter{margin:7px 0;}
.meter-head{display:flex;justify-content:space-between;font-size:12.5px;}
.mlabel{text-transform:capitalize;}
.mval{color:var(--muted);}
.bar{height:7px;background:#0c0e13;border-radius:6px;overflow:hidden;margin-top:3px;}
.fill{height:100%;background:linear-gradient(90deg,#3a6df0,#5ad1a0);}
.evidence{color:var(--muted);font-size:11.5px;margin-top:2px;}
.tags{margin-top:10px;}
.tag{display:inline-block;background:#0c0e13;border:1px solid var(--line);color:#bcc3d0;padding:2px 8px;border-radius:6px;font-size:12px;margin:2px;}
.chip{display:inline-block;background:#0c0e13;border:1px solid var(--line);color:#9fc1ff;padding:3px 9px;border-radius:6px;font-size:12px;margin:3px;text-decoration:none;}
.chip.ident{color:#bcd;}
.linkgroup{margin:4px 0;}
.lcat{display:inline-block;color:var(--muted);font-size:11px;text-transform:uppercase;width:64px;}
.footer-row{display:flex;gap:18px;align-items:center;justify-content:space-between;border-top:1px solid var(--line);margin-top:14px;padding-top:10px;flex-wrap:wrap;}
.contact{background:#0c0e13;border:1px solid var(--line);padding:3px 9px;border-radius:6px;font-size:12.5px;margin-right:6px;}
.contact em{color:var(--muted);font-style:normal;}
.muted{color:var(--muted);} .srccount{color:var(--muted);font-size:12px;}
details summary{cursor:pointer;color:var(--accent);font-size:12.5px;margin-top:8px;}
"""


def build_report(top: int = 25, out_path: str | None = None) -> str:
    icp = load_icp()
    with session_scope() as session:
        cands = session.exec(
            select(Candidate).order_by(Candidate.composite_score.desc().nullslast()).limit(top)
        ).all()
        cards = []
        ranked = [c for c in cands if c.composite_score is not None]
        for i, c in enumerate(ranked, 1):
            sc = session.exec(
                select(Scorecard).where(Scorecard.candidate_id == c.id).order_by(Scorecard.created_at.desc())
            ).first()
            dossier = session.exec(
                select(Dossier).where(Dossier.candidate_id == c.id).order_by(Dossier.generated_at.desc())
            ).first()
            idents = session.exec(select(Identity).where(Identity.candidate_id == c.id)).all()
            contacts = session.exec(select(Contact).where(Contact.candidate_id == c.id)).all()
            cards.append(_candidate_card(i, c, sc, dossier, idents, contacts))

    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    body = "\n".join(cards) or "<p class='muted'>No scored candidates yet.</p>"
    doc = f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Talent Scout - Shortlist</title><style>{CSS}</style></head>
<body><div class='wrap'>
<header class='top'>
  <h1>Talent Scout - Candidate Shortlist</h1>
  <div class='sub'>{_esc(icp.get('role'))} &nbsp;·&nbsp; {len(cards)} candidates &nbsp;·&nbsp; generated {generated}
  &nbsp;·&nbsp; target geo: {_esc(icp.get('geo',{}).get('country'))}</div>
</header>
{body}
</div></body></html>"""

    if out_path is None:
        out_dir = PROJECT_ROOT / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"talent_report_{datetime.now().strftime('%Y%m%d_%H%M')}.html")
    Path(out_path).write_text(doc, encoding="utf-8")
    return out_path
