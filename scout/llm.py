"""
LLM layer. Claude is the brain for everything (summarization, classification,
scoring, outreach, deep research). OpenAI / Gemini are wired as best-effort
fallbacks so a single provider hiccup never stalls the pipeline.

Exposes:
  - complete(system, user)          -> str
  - complete_json(system, user)     -> dict | list   (robust parsing + retry)
  - research(instructions)          -> {"text": str, "sources": [{"url","title"}]}
"""

from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from typing import Any

from .config import get_settings


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self._anthropic = None
        self._openai = None
        self._gemini = None

    # ---- lazy clients -------------------------------------------------
    @property
    def anthropic(self):
        if self._anthropic is None:
            import anthropic

            if not self.settings.anthropic_api_key:
                raise LLMError("ANTHROPIC_API_KEY is not set")
            self._anthropic = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._anthropic

    @property
    def openai(self):
        if self._openai is None and self.settings.openai_api_key:
            from openai import OpenAI

            self._openai = OpenAI(api_key=self.settings.openai_api_key)
        return self._openai

    @property
    def gemini(self):
        if self._gemini is None and self.settings.gemini_api_key:
            from google import genai

            self._gemini = genai.Client(api_key=self.settings.gemini_api_key)
        return self._gemini

    # ---- core completion ---------------------------------------------
    def complete(
        self,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.2,
    ) -> str:
        model = model or self.settings.model
        last_err: Exception | None = None

        # Primary: Claude
        for attempt in range(1, 4):
            try:
                msg = self.anthropic.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return _anthropic_text(msg)
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(2 ** attempt)

        # Fallback: OpenAI
        try:
            if self.openai:
                resp = self.openai.chat.completions.create(
                    model="gpt-4o",
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            last_err = e

        # Fallback: Gemini
        try:
            if self.gemini:
                resp = self.gemini.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=f"{system}\n\n{user}",
                )
                return resp.text or ""
        except Exception as e:  # noqa: BLE001
            last_err = e

        raise LLMError(f"All LLM providers failed: {last_err}")

    def complete_json(
        self,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.1,
    ) -> Any:
        """Ask for JSON and parse it robustly. Returns dict or list."""
        sys_json = (
            system
            + "\n\nRESPOND WITH VALID JSON ONLY. No prose, no markdown fences."
        )
        raw = self.complete(
            sys_json, user, model=model, max_tokens=max_tokens, temperature=temperature
        )
        parsed = _parse_json(raw)
        if parsed is None:
            # One corrective retry asking to fix the JSON.
            fix = self.complete(
                "You convert the following text into a single valid JSON value. "
                "Output JSON ONLY.",
                raw,
                model=model,
                temperature=0.0,
            )
            parsed = _parse_json(fix)
        if parsed is None:
            raise LLMError("Could not parse JSON from model output")
        return parsed

    # ---- web research -------------------------------------------------
    def research(self, instructions: str, max_uses: int = 6) -> dict:
        """
        Run a web-grounded research turn. Returns {"text", "sources"}.
        Tries Claude's server-side web_search tool first, then Gemini grounding.
        """
        # Primary: Claude web search tool
        try:
            msg = self.anthropic.messages.create(
                model=self.settings.model,
                max_tokens=8000,
                system=(
                    "You are a meticulous OSINT researcher. Use web search to gather "
                    "factual, sourced information. Prefer primary sources. Always ground "
                    "claims in the pages you actually read."
                ),
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": max_uses,
                    }
                ],
                messages=[{"role": "user", "content": instructions}],
            )
            return {"text": _anthropic_text(msg), "sources": _anthropic_sources(msg)}
        except Exception:  # noqa: BLE001
            pass

        # Fallback: Gemini with Google Search grounding
        try:
            if self.gemini:
                from google.genai import types

                resp = self.gemini.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=instructions,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    ),
                )
                return {"text": resp.text or "", "sources": _gemini_sources(resp)}
        except Exception:  # noqa: BLE001
            pass

        # Last resort: ungrounded completion (clearly marked, no sources).
        text = self.complete(
            "You are a researcher. You have no live web access right now; answer only "
            "from prior knowledge and clearly flag uncertainty.",
            instructions,
        )
        return {"text": text, "sources": []}


# ---- module-level helpers --------------------------------------------
def _anthropic_text(msg) -> str:
    parts = []
    for block in getattr(msg, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def _anthropic_sources(msg) -> list[dict]:
    sources: list[dict] = []
    seen = set()
    for block in getattr(msg, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "web_search_tool_result":
            for item in getattr(block, "content", []) or []:
                url = getattr(item, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"url": url, "title": getattr(item, "title", "") or ""})
        elif btype == "text":
            for cit in getattr(block, "citations", []) or []:
                url = getattr(cit, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"url": url, "title": getattr(cit, "title", "") or ""})
    return sources


def _gemini_sources(resp) -> list[dict]:
    sources: list[dict] = []
    seen = set()
    try:
        for cand in resp.candidates or []:
            meta = getattr(cand, "grounding_metadata", None)
            for chunk in getattr(meta, "grounding_chunks", []) or []:
                web = getattr(chunk, "web", None)
                url = getattr(web, "uri", None)
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"url": url, "title": getattr(web, "title", "") or ""})
    except Exception:  # noqa: BLE001
        pass
    return sources


def _parse_json(raw: str) -> Any | None:
    if not raw:
        return None
    text = raw.strip()
    # Strip ``` fences if present
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Grab the first {...} or [...] block
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


@lru_cache(maxsize=1)
def get_llm() -> LLM:
    return LLM()
