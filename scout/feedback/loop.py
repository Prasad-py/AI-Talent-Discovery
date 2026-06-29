"""
Feedback loop (Stage 7+).

The pipeline should get smarter from our own outcomes. As humans label candidates
(advanced / hired / rejected) we learn which sub-scores actually predict success and
nudge the composite weights accordingly.

v1 uses a simple, transparent estimator: for each sub-score, the new weight is
proportional to how much higher it is, on average, for positive outcomes than for
negative ones (a mean-difference signal). Learned weights are written to
config/learned_weights.json and preferred by the scoring engine when present.

This is deliberately interpretable; it can be swapped for a learning-to-rank model
once there is enough labeled data.
"""

from __future__ import annotations

import json

from sqlmodel import select

from ..config import PROJECT_ROOT
from ..db import session_scope
from ..models import Outcome, Scorecard
from ..scoring.scorecard import METRICS

SUBSCORES = list(METRICS.keys())  # the 12 scorecard metrics
POSITIVE = {"advanced", "hired"}
NEGATIVE = {"rejected"}
LEARNED_PATH = PROJECT_ROOT / "config" / "learned_weights.json"
MIN_PER_CLASS = 3


def _latest_metrics(session, candidate_id: int) -> dict | None:
    sc = session.exec(
        select(Scorecard)
        .where(Scorecard.candidate_id == candidate_id)
        .order_by(Scorecard.created_at.desc())
    ).first()
    if not sc:
        return None
    return {m: float((sc.metrics or {}).get(m, {}).get("score", 0.0)) for m in SUBSCORES}


def record_outcome(candidate_id: int, label: str, note: str | None = None) -> None:
    with session_scope() as session:
        existing = session.exec(
            select(Outcome).where(Outcome.candidate_id == candidate_id)
        ).first()
        if existing:
            existing.label = label
            existing.note = note
        else:
            session.add(Outcome(candidate_id=candidate_id, label=label, note=note))


def get_learned_weights() -> dict | None:
    if LEARNED_PATH.exists():
        try:
            return json.loads(LEARNED_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def update_weights_from_outcomes() -> dict:
    """Recompute composite weights from labeled outcomes. Returns a status dict."""
    pos: list[dict] = []
    neg: list[dict] = []
    with session_scope() as session:
        outcomes = session.exec(select(Outcome)).all()
        for o in outcomes:
            bucket = POSITIVE if o.label in POSITIVE else NEGATIVE if o.label in NEGATIVE else None
            if not bucket:
                continue
            row = _latest_metrics(session, o.candidate_id)
            if not row:
                continue
            (pos if o.label in POSITIVE else neg).append(row)

    if len(pos) < MIN_PER_CLASS or len(neg) < MIN_PER_CLASS:
        return {
            "updated": False,
            "reason": f"need >= {MIN_PER_CLASS} positive and negative labels "
            f"(have {len(pos)} pos, {len(neg)} neg)",
        }

    def mean(rows, key):
        return sum(r[key] for r in rows) / len(rows)

    raw = {k: max(0.0, mean(pos, k) - mean(neg, k)) for k in SUBSCORES}
    total = sum(raw.values())
    if total <= 0:
        # No discriminative signal; fall back to uniform.
        weights = {k: 1.0 / len(SUBSCORES) for k in SUBSCORES}
    else:
        # Blend learned signal with a uniform floor so nothing collapses to zero.
        floor = 0.10
        weights = {k: floor / len(SUBSCORES) + (1 - floor) * (raw[k] / total) for k in SUBSCORES}
        norm = sum(weights.values())
        weights = {k: round(v / norm, 4) for k, v in weights.items()}

    LEARNED_PATH.write_text(json.dumps(weights, indent=2), encoding="utf-8")
    return {"updated": True, "weights": weights, "n_pos": len(pos), "n_neg": len(neg)}
