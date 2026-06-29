"""
Lightweight geography helpers for India area-wise targeting.

Location data (GitHub `location`, Codeforces `city/country`) is free-text and messy,
so we use a tolerant keyword match to (a) decide if someone is in the target country
and (b) bucket them into a region/area. This is intentionally simple; the deep-dive
LLM refines location later.
"""

from __future__ import annotations

# Common Indian tech hubs + aliases -> canonical area label.
INDIA_AREAS: dict[str, list[str]] = {
    "Bengaluru": ["bengaluru", "bangalore", "blr"],
    "Hyderabad": ["hyderabad", "secunderabad", "hyd"],
    "Delhi NCR": ["delhi", "new delhi", "ncr", "gurgaon", "gurugram", "noida", "faridabad", "ghaziabad"],
    "Mumbai": ["mumbai", "bombay", "navi mumbai", "thane"],
    "Pune": ["pune", "pimpri"],
    "Chennai": ["chennai", "madras"],
    "Kolkata": ["kolkata", "calcutta"],
    "Ahmedabad": ["ahmedabad", "gandhinagar"],
    "Jaipur": ["jaipur"],
    "Kerala": ["kochi", "cochin", "trivandrum", "thiruvananthapuram", "kerala"],
    "Other India": ["india", "bharat", "indore", "bhopal", "chandigarh", "lucknow", "coimbatore", "nagpur", "vizag", "visakhapatnam", "bhubaneswar", "guwahati", "roorkee", "kanpur", "kharagpur", "varanasi"],
}

_COUNTRY_MARKERS = ["india", "bharat"]


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def is_in_country(location: str | None, country: str = "India") -> bool:
    loc = _norm(location)
    if not loc:
        return False
    if country.lower() == "india":
        # Any India marker, OR any known Indian city alias.
        if any(m in loc for m in _COUNTRY_MARKERS):
            return True
        for aliases in INDIA_AREAS.values():
            if any(a in loc for a in aliases):
                return True
        return False
    return country.lower() in loc


def area_of(location: str | None) -> str | None:
    """Return the canonical Indian area for a free-text location, if any."""
    loc = _norm(location)
    if not loc:
        return None
    for area, aliases in INDIA_AREAS.items():
        if any(a in loc for a in aliases):
            return area
    return None
