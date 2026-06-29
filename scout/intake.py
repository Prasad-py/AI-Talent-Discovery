"""
Intake / query understanding.

Turns a recruiter's plain-language brief (a short description of who they want) plus
location parameters into a structured search PLAN: target geography + areas, relevant
programming languages, well-known GitHub repos to mine, X/HuggingFace/publication
topics, search keywords, and which scorecard metrics to emphasize.

This is what lets the user "just describe" the candidate and have the system search
intelligently with many derived keywords.
"""

from __future__ import annotations

from .config import load_icp
from .llm import get_llm
from .progress import emit

PLAN_SYSTEM = (
    "You are the planning brain of an agentic talent-discovery engine. Convert a hiring "
    "brief into a precise, machine-usable search plan, ADAPTING entirely to what the user "
    "asked for - do not assume any default domain or country.\n\n"
    "Infer the target geography and specific cities/areas from the location text (ANY "
    "country, not just India; if no location is given, set country to 'Global' and leave "
    "areas empty). Pick REAL programming languages, well-known GitHub repositories, and "
    "topics relevant to the described role, plus strong search keywords.\n\n"
    "Choose SOURCES intelligently for THIS brief:\n"
    "  - github_geo: true when a location is specified (search devs by city/language).\n"
    "  - codeforces: true ONLY if the brief values competitive programming / students / "
    "raw algorithmic talent (Codeforces only resolves country, best for that case).\n"
    "  - huggingface: true if the brief is about AI/ML model builders.\n"
    "  - publications: true if the brief is research-oriented (papers, PhD, novel methods).\n"
    "  - repos: true if contributing to specific well-known projects matters (then fill target_repos).\n\n"
    "Pick which evaluation metrics matter most for this role (emphasis). Return JSON ONLY:\n"
    "{\n"
    '  "role": "concise role title",\n'
    '  "description": "tightened 1-2 sentence ideal-candidate description",\n'
    '  "plan_reasoning": "1-2 sentences on how you adapted the plan to the brief",\n'
    '  "geo": {"country": "...", "areas": ["city or region", ...]},\n'
    '  "languages": ["Python", ...],   // REAL programming languages only (Python, C++, Rust, TypeScript, Go, Cuda) - NOT frameworks like TensorFlow/PyTorch\n'
    '  "target_repos": ["owner/repo", ...],            // real, well-known repos in this domain\n'
    '  "x_topics": ["..."],\n'
    '  "keywords": ["search keywords/phrases to find such people"],\n'
    '  "sources": {"github_geo": true, "codeforces": false, "huggingface": false, "publications": false, "repos": false},\n'
    '  "emphasis": {"<scorecard_metric>": 0.0-0.2}      // additive weight nudges, optional\n'
    "}\n"
    "Valid scorecard metrics: engineering_depth, code_quality_judgment, open_source_impact, "
    "ai_domain_depth, ahead_of_curve, shipping_velocity, consistency_momentum, research_depth, "
    "communication_writing, community_standing, authenticity, hireability."
)


def build_plan(description: str, location: str, role: str | None = None, notes: str | None = None) -> dict:
    """Ask Claude to expand the brief into a structured search plan."""
    emit("intake", "Understanding your brief and deriving a search plan...")
    llm = get_llm()
    user = (
        f"BRIEF (who we want): {description}\n"
        f"LOCATION PARAMETERS: {location}\n"
        f"ROLE (optional): {role or '(infer)'}\n"
        f"EXTRA NOTES: {notes or '(none)'}\n\n"
        "Produce the search plan now."
    )
    try:
        plan = llm.complete_json(PLAN_SYSTEM, user)
        if not isinstance(plan, dict):
            raise ValueError("plan not a dict")
    except Exception as e:  # noqa: BLE001
        emit("intake", f"Could not derive a plan ({e}); using defaults.", level="warn")
        plan = {}
    if plan.get("plan_reasoning"):
        emit("intake", f"Plan: {plan['plan_reasoning']}")
    geo = plan.get("geo") or {}
    emit("intake", f"Targeting {geo.get('country','?')} - areas: {', '.join(geo.get('areas') or []) or 'any'}")
    src = plan.get("sources") or {}
    chosen = ", ".join(k for k, v in src.items() if v) or "github_geo"
    emit("intake", f"Sources chosen for this brief: {chosen}")
    emit("intake", "Search plan ready.", {
        "geo": geo,
        "languages": plan.get("languages"),
        "target_repos": plan.get("target_repos"),
        "keywords": plan.get("keywords"),
        "sources": src,
    })
    return plan


def plan_to_overrides(plan: dict) -> dict:
    """Translate a plan into icp.yaml runtime overrides."""
    icp = load_icp()
    overrides: dict = {}
    if plan.get("role"):
        overrides["role"] = plan["role"]
    if plan.get("description"):
        overrides["description"] = plan["description"]
    geo = plan.get("geo") or {}
    if geo:
        overrides["geo"] = {
            "country": geo.get("country", icp.get("geo", {}).get("country", "India")),
            "areas": geo.get("areas") or icp.get("geo", {}).get("areas", []),
        }
    gh = {}
    if plan.get("languages"):
        gh["languages"] = plan["languages"]
    if gh:
        overrides["github"] = gh
    if plan.get("target_repos"):
        overrides["target_repos"] = plan["target_repos"]
    if plan.get("x_topics"):
        overrides["x"] = {"topics": plan["x_topics"]}

    # Emphasis -> scorecard weight nudges (additive on top of defaults).
    emphasis = plan.get("emphasis") or {}
    if emphasis:
        from .scoring.scorecard import METRICS

        weights = {m: METRICS[m][1] for m in METRICS}
        for m, delta in emphasis.items():
            if m in weights:
                try:
                    weights[m] = max(0.0, weights[m] + float(delta))
                except (TypeError, ValueError):
                    pass
        overrides["scorecard_weights"] = weights
    return overrides
