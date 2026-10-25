"""
xai.py: echte Suche auf X ueber die xAI-API (Werkzeug x_search), seit 2.43. Braucht XAI_API_KEY auf web.
Ohne Schluessel faellt Your Engine auf die allgemeine Websuche zurueck (deutlich schwaecher fuer X).

Kosten laut xAI: Tokens plus 5 USD je 1000 gelesene Posts. Dem Nutzer wird X_SEARCH_USD (Standard 0.25, falls xAI die Zahl der Posts nicht meldet)
je Suche plus die Tokens mit der ueblichen Marge vom Guthaben abgezogen.
"""
import os

import requests

URL = "https://api.x.ai/v1/responses"


def bereit():
    return bool(os.getenv("XAI_API_KEY", "").strip())


def suchen(frage, modell=None):
    """-> {"text", "sources": [{"url","title"}], "usage": {...}} oder {"error"}"""
    key = os.getenv("XAI_API_KEY", "").strip()
    if not key:
        return {"error": "no XAI_API_KEY"}
    body = {"model": modell or os.getenv("XAI_MODEL", "grok-4.7"), "input": [{"role": "user", "content": frage}],
            "tools": [{"type": "x_search"}]}
    try:
        r = requests.post(URL, json=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=90)
        d = r.json()
    except Exception as e:
        return {"error": str(e)[:160]}
    if r.status_code >= 400:
        return {"error": str((d.get("error") if isinstance(d, dict) else d))[:200]}
    text, quellen = [], []
    for item in d.get("output") or []:
        for c in item.get("content") or []:
            if c.get("type") in ("output_text", "text") and c.get("text"):
                text.append(c["text"])
            for a in c.get("annotations") or []:
                u = a.get("url")
                if u and u not in [q["url"] for q in quellen]:
                    quellen.append({"url": u, "title": (a.get("title") or "")[:80]})
    if not text and d.get("output_text"):
        text = [d["output_text"]]
    return {"text": "\n".join(text).strip(), "sources": quellen[:12], "usage": d.get("usage") or {}}


def kosten_usd(usage):
    u = usage or {}
    tin = float(u.get("input_tokens") or u.get("prompt_tokens") or 0)
    tout = float(u.get("output_tokens") or u.get("completion_tokens") or 0)
    quellen = u.get("num_sources_used")
    for k in ("server_side_tool_usage_details", "tool_usage"):
        v = u.get(k)
        if isinstance(v, dict) and quellen is None:
            quellen = v.get("x_search_calls") or v.get("num_sources_used")
    suche = float(quellen) * 0.005 if quellen else float(os.getenv("X_SEARCH_USD", "0.25"))
    return tin * 2e-6 + tout * 6e-6 + suche


STIMMUNG = """Use X search to read what people on X posted about {thema} in the last {stunden} hours.
Search several angles (the ticker with $, the name, the main hashtags) and read at least 30 posts if they exist.

Answer in this exact structure, short and factual, no dashes:
POSTS READ: the number of posts you actually read, and how many distinct accounts.
MOOD: bullish / bearish / mixed, with a rough split like "about 60% bullish, 25% bearish, 15% neutral" based only on the posts you read.
WHAT DRIVES IT: the 3 to 5 main topics or claims, each with one example post link.
LOUDEST ACCOUNTS: up to 5 handles with large reach that posted about it, and what they said.
WARNING SIGNS: copy pasted shilling, bot replies, fake contract addresses, coordinated pushes. Write "none seen" if none.
Only use posts you actually found. If you found few posts, say how few and do not invent a split. No price predictions, no buy or sell advice."""


def stimmung(adr, thema, stunden=24):
    """Bezahlte X-Suche fuer einen Nutzer. -> {"text","sources","cost_usd"} oder {"error"}"""
    import ai
    if not bereit():
        return {"error": "no XAI_API_KEY"}
    reserve = float(os.getenv("X_SEARCH_RESERVE_USD", "0.5"))
    if adr:
        try:
            if not ai._reservieren(adr, reserve):
                return {"error": "Not enough balance for an X search."}
        except Exception:
            return {"error": "Balance check failed."}
    d = suchen(STIMMUNG.format(thema=str(thema)[:200], stunden=int(stunden or 24)))
    usd = 0.0
    if not d.get("error"):
        try:
            usd = round(kosten_usd(d.get("usage")) * (1 + ai.markup(adr)), 6)
        except Exception:
            usd = round(kosten_usd(d.get("usage")) * 1.095, 6)
    if adr:
        try:
            ai._ausgleichen(adr, reserve, usd)
        except Exception:
            pass
    if d.get("error"):
        return d
    t = d["text"].replace("—", ",").replace("–", "-")
    return {"text": t[:2500], "sources": d.get("sources") or [], "cost_usd": usd, "source": "X search (live)"}
