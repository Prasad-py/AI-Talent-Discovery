"""
X / Twitter source - a Claude port of xPool.

xPool's core idea: use the X API to find people using first-person build language
("I built", "shipped", "working on"), then use an LLM to read their recent tweets and
classify them as developer / influencer / recruiter / company / bot (with confidence,
tech stack, seniority, and a source/maybe/skip recommendation) - so we keep real
builders and drop the noise.

We swap Grok for Claude. X data acquisition is optional/free-first:
  - If X_BEARER_TOKEN is set, we use the X API v2 (recent search + user lookup + tweets).
  - If not, classification falls back to Claude web research on the public handle.
"""

from __future__ import annotations

from typing import Optional

from ..config import get_settings, load_icp
from ..http import get_json
from ..llm import get_llm

X_API = "https://api.twitter.com/2"


# --------------------------------------------------------------------------
# Query generation (xPool step 1)
# --------------------------------------------------------------------------
def generate_search_queries(n: int = 5) -> list[str]:
    """Claude generates X search queries that surface real builders, not influencers."""
    icp = load_icp()
    x_cfg = icp.get("x", {})
    llm = get_llm()
    system = (
        "You generate X (Twitter) search queries to FIND software/AI engineers who "
        "actually build - not influencers, recruiters, or companies. Favor first-person "
        "build language and exclude recruiting spam. Return JSON only."
    )
    user = (
        f"Role we are hiring for: {icp.get('role')}\n"
        f"Topics: {x_cfg.get('topics')}\n"
        f"First-person markers to favor: {x_cfg.get('first_person_markers')}\n\n"
        f"Produce {n} X search query strings. Each should combine a topic with a "
        "first-person build phrase and exclude obvious noise (e.g. -hiring -\"we are\" "
        "-job). Return JSON: {\"queries\": [\"...\", ...]}"
    )
    try:
        data = llm.complete_json(system, user)
        queries = data.get("queries") if isinstance(data, dict) else data
        return [q for q in (queries or []) if isinstance(q, str)][:n]
    except Exception:  # noqa: BLE001
        # Deterministic fallback if the LLM call fails.
        markers = x_cfg.get("first_person_markers", ["I built"])
        topics = x_cfg.get("topics", ["LLM"])
        return [f'"{m}" {t} -hiring -job' for m, t in zip(markers, topics)][:n]


# --------------------------------------------------------------------------
# X API access (optional; free-first => may be absent)
# --------------------------------------------------------------------------
def x_enabled() -> bool:
    return bool(get_settings().x_bearer_token)


def _x_headers() -> dict:
    return {"Authorization": f"Bearer {get_settings().x_bearer_token}"}


def fetch_user(handle: str) -> Optional[dict]:
    if not x_enabled():
        return None
    handle = handle.lstrip("@")
    try:
        data = get_json(
            f"{X_API}/users/by/username/{handle}",
            headers=_x_headers(),
            params={
                "user.fields": "description,public_metrics,created_at,verified,location,url"
            },
            ttl=86400,
        )
        return data.get("data")
    except Exception:  # noqa: BLE001
        return None


def fetch_user_tweets(handle: str, max_results: int = 20) -> list[dict]:
    if not x_enabled():
        return []
    user = fetch_user(handle)
    if not user:
        return []
    try:
        data = get_json(
            f"{X_API}/users/{user['id']}/tweets",
            headers=_x_headers(),
            params={
                "max_results": max(5, min(max_results, 100)),
                "tweet.fields": "created_at,public_metrics,lang",
                "exclude": "retweets,replies",
            },
            ttl=86400,
        )
        return data.get("data", []) or []
    except Exception:  # noqa: BLE001
        return []


def search_users(query: str, max_users: int = 40) -> list[str]:
    """Find candidate handles via recent tweet search (requires X token)."""
    if not x_enabled():
        return []
    try:
        data = get_json(
            f"{X_API}/tweets/search/recent",
            headers=_x_headers(),
            params={
                "query": f"{query} -is:retweet lang:en",
                "max_results": 100,
                "expansions": "author_id",
                "user.fields": "username",
            },
            ttl=3600,
        )
        users = (data.get("includes") or {}).get("users", [])
        handles = []
        seen = set()
        for u in users:
            h = u.get("username")
            if h and h not in seen:
                seen.add(h)
                handles.append(h)
        return handles[:max_users]
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------
# Classification (xPool step 2) - Claude deep analysis
# --------------------------------------------------------------------------
CLASSIFY_SYSTEM = (
    "You are an expert technical talent analyst. You read a person's public X/Twitter "
    "presence and classify them. Be skeptical: distinguish real engineers who BUILD "
    "from influencers, recruiters, companies, and bots/AI-generated accounts.\n"
    "Return JSON ONLY with keys:\n"
    "{\n"
    '  "type": "developer|influencer|recruiter|company|bot|unknown",\n'
    '  "confidence": 0-100,\n'
    '  "is_human": true|false,\n'
    '  "tech_stack": ["..."],\n'
    '  "seniority": "junior|mid|senior|lead|unknown",\n'
    '  "ahead_of_curve": 0-100,\n'
    '  "recommendation": "source|maybe|skip",\n'
    '  "reasoning": "1-3 sentences citing concrete evidence"\n'
    "}"
)


def classify_user(handle: str, profile: Optional[dict] = None, tweets: Optional[list] = None) -> dict:
    """
    Classify an X user. Uses provided profile/tweets if available (X API), otherwise
    asks Claude to research the public handle on the web.
    """
    llm = get_llm()
    handle = handle.lstrip("@")

    if profile is None and not tweets and x_enabled():
        profile = fetch_user(handle)
        tweets = fetch_user_tweets(handle)

    if profile or tweets:
        tweet_text = "\n".join(f"- {t.get('text','')}" for t in (tweets or [])[:20])
        user = (
            f"X handle: @{handle}\n"
            f"Profile: {profile}\n\n"
            f"Recent tweets:\n{tweet_text or '(none available)'}\n\n"
            "Classify this account."
        )
        try:
            return llm.complete_json(CLASSIFY_SYSTEM, user)
        except Exception:  # noqa: BLE001
            pass

    # Fallback: research the handle on the web, then classify the findings.
    research = llm.research(
        f"Research the X/Twitter account @{handle}. Summarize who they are, whether they "
        f"are a real engineer who builds (vs an influencer/recruiter/company/bot), their "
        f"tech stack, seniority, and how early they adopt new AI models/tools. Cite sources."
    )
    try:
        result = llm.complete_json(
            CLASSIFY_SYSTEM,
            f"X handle: @{handle}\n\nWeb research findings:\n{research['text']}\n\nClassify.",
        )
        result.setdefault("_sources", research.get("sources", []))
        return result
    except Exception:  # noqa: BLE001
        return {
            "type": "unknown",
            "confidence": 0,
            "is_human": True,
            "tech_stack": [],
            "seniority": "unknown",
            "ahead_of_curve": 0,
            "recommendation": "maybe",
            "reasoning": "Insufficient data to classify.",
        }
