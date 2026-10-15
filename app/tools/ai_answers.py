"""Whether AI assistants name a roofer when a homeowner asks them who to call.

One question, the way a homeowner would ask it, put to ChatGPT (searching the
web) and Perplexity through DataForSEO's AI Optimization API: about six cents
for both, cached for 30 days per town. The answer is read for two things:
whether this business is named, and which roofers are. Names are pulled from
the answer's bold text, which both assistants use for the businesses they
list; anything that reads like a sentence or a figure is left out.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping

import httpx

from app.config import get_config
from app.store import firestore as store

BASE = "https://api.dataforseo.com/v3/ai_optimization"
TIMEOUT = 180.0
ATTEMPTS = 2
# Which assistant, the endpoint, and the request. ChatGPT searches the web
# here, as it does for a homeowner asking it today.
ASSISTANTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("ChatGPT", "chat_gpt/llm_responses/live", {"model_name": "gpt-6.1-sol", "web_search": True}),
    ("Perplexity", "perplexity/llm_responses/live", {"model_name": "sonar"}),
)
_SUFFIXES = {"llc", "inc", "co", "company", "corp", "corporation", "the", "ltd", "l.l.c."}
_TRADE = re.compile(r"roof|exterior|construct|contract|build|restor|gutter|siding|renovat|remodel|home|"
                    r"\b(llc|inc|co|corp|company|services?|group|solutions|pros?)\b")


class AnswersUnavailable(RuntimeError):
    pass


def question(city: str, state: str | None = None) -> str:
    where = f"{city}, {state}" if state else city
    return f"Who are the best roofing companies in {where}?"


def _text(result: Mapping[str, Any]) -> str:
    out = []
    for item in result.get("items") or []:
        out.append(str(item.get("text") or ""))
        for section in item.get("sections") or []:
            out.append(str(section.get("text") or ""))
    return "\n".join(t for t in out if t)


def named_businesses(text: str) -> list[str]:
    """The roofers an answer lists, in order, once each."""
    found = []
    # Every other piece between ** markers is bold. Pairing markers in order
    # keeps a long bold sentence from shifting which pieces count as bold.
    for raw in text.split("**")[1::2]:
        name = raw.strip(" :.-—|")
        words = name.split()
        if not 1 <= len(words) <= 6 or re.search(r"\d|/|\+|%", name) or not name[0].isupper():
            continue
        if name.lower().split()[0] in {"i", "i'd", "i’d", "if", "for", "when", "before", "best", "why", "what"}:
            continue
        # A bold heading ("My recommendation") is not a business. A roofer's
        # name carries a trade word or a company ending.
        if not _TRADE.search(name.lower()):
            continue
        found.append(name)
    # "Legacy Roofing" and "Legacy Roofing LLC" in one answer are one roofer.
    seen: dict[str, str] = {}
    for name in found:
        seen.setdefault(_core(name), name)
    return list(seen.values())


def _core(name: str) -> str:
    words = [w for w in re.sub(r"[^\w\s]", " ", name.lower()).split() if w not in _SUFFIXES]
    return " ".join(words)


def mentions(text: str, *, name: str, domain: str | None) -> bool:
    """Whether this business is in the answer, by name or by its website."""
    low = text.lower()
    core = _core(name)
    if core and re.search(rf"\b{re.escape(core)}\b", re.sub(r"[^\w\s]", " ", low)):
        return True
    return bool(domain) and domain.lower().removeprefix("www.") in low


def _ask(label: str, path: str, extra: Mapping[str, Any], prompt: str, http: httpx.Client) -> dict[str, Any]:
    cfg = get_config()
    request = {"user_prompt": prompt, **extra}
    cached = store.cache_get("ai_answer", {"path": path, **request})
    if cached is None:
        body = None
        for attempt in range(ATTEMPTS):
            try:
                body = http.post(f"{BASE}/{path}", json=[request],
                                 auth=(cfg.dataforseo_login, cfg.dataforseo_password)).json()
                break
            except (httpx.HTTPError, ValueError):
                if attempt == ATTEMPTS - 1:
                    return {"assistant": label, "ok": False}
        task = ((body or {}).get("tasks") or [{}])[0]
        if int(task.get("status_code") or 0) >= 40000:
            return {"assistant": label, "ok": False}
        cached = {"text": _text((task.get("result") or [{}])[0] or {})}
        store.cache_put("ai_answer", {"path": path, **request}, cached)
    return {"assistant": label, "ok": bool(cached.get("text")), "text": cached.get("text") or ""}


def ask(city: str, state: str | None, *, name: str, domain: str | None,
        client: httpx.Client | None = None) -> dict[str, Any]:
    """{"question", "answers": [{"assistant", "named_you", "named": [...]}]}.
    An assistant that did not answer is left out; none answering raises."""
    cfg = get_config()
    if not cfg.dataforseo_login or not cfg.dataforseo_password:
        raise AnswersUnavailable("DataForSEO credentials are not set.")
    if not city:
        raise AnswersUnavailable("No town on record to ask about.")
    prompt = question(city, state)
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        with ThreadPoolExecutor(max_workers=len(ASSISTANTS)) as pool:
            raw = list(pool.map(lambda a: _ask(a[0], a[1], a[2], prompt, http), ASSISTANTS))
    finally:
        if client is None:
            http.close()
    answers = []
    for r in raw:
        if not r.get("ok"):
            continue
        you = mentions(r["text"], name=name, domain=domain)
        others = [n for n in named_businesses(r["text"]) if not mentions(n, name=name, domain=None)]
        answers.append({"assistant": r["assistant"], "named_you": you, "named": others[:6]})
    if not answers:
        raise AnswersUnavailable("Neither assistant answered. Try again in a minute.")
    return {"question": prompt, "answers": answers}
