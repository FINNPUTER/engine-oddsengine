"""
projekt.py: Deep Research zu einem Projekt. Seit 2.39.2.

Was ein Coin ueber sich behauptet, gegen sich selbst, gegen die Chain und gegen das Netz gehalten:

  1. Links finden: DexScreener, pump.fun, Metadaten des Tokens (Website, X, Telegram, GitHub).
  2. Website lesen: Startseite plus Unterseiten (Docs, Whitepaper, Tokenomics, Terms, FAQ, Roadmap),
     auch nicht verlinkte Standardpfade. Alter der Domain (RDAP), kostenloser Hoster, Baukasten-Spuren,
     tote Links, steht die CA auf der Seite oder eine andere (Nachahmer), benannte Wallets.
  3. Partner-Seiten: nennen sie das Projekt zurueck?
  4. X-Konto: Follower, Alter, Posts. Wer spricht gerade auf X darueber (Live-Suche ueber ein Modell
     mit Websuche, vom Guthaben).
  5. Telegram: gibt es die Gruppe, wie viele Mitglieder.
  6. GitHub: gibt es das Repo, wie alt, letzte Aenderung, Sterne, nur eine Kopie (Fork)?
  7. DexScreener: bezahltes Profil, Boosts, Werbung.
  8. EIN KI-Aufruf: Behauptungen herausziehen, Seite gegen Seite und Seite gegen Chain vergleichen.

Kein Urteil "Scam" oder "safe": Befunde mit Quelle. Ergebnis 6 Stunden im Speicher.
Variablen: PROJECT_MODEL (Vergleich, Standard wie Smart), X_PULSE_MODEL (Live-Suche auf X),
GITHUB_TOKEN (optional, hoehere Grenze).
"""
import hashlib
import html as _html
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests

import storage
from logger import log

UA = {"User-Agent": "Mozilla/5.0 (compatible; YourEngine research)", "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
CACHE_S = 6 * 3600
MAX_PAGES = 8
PAGE_CHARS = 6000
PROBE = ("/docs", "/whitepaper", "/tokenomics", "/terms", "/about", "/roadmap", "/faq")
SUB = ("terms", "tos", "legal", "docs", "doc", "whitepaper", "litepaper", "tokenomics", "faq", "about", "roadmap",
       "gitbook", "conditions", "disclaimer")
DOC_HOSTS = ("gitbook.io", "notion.site", "notion.so", "medium.com", "mirror.xyz", "substack.com")
NICHT_PARTNER = ("x.com", "twitter.com", "t.me", "telegram.", "discord.", "dexscreener.", "dextools.", "birdeye.",
                 "solscan.", "etherscan.", "basescan.", "bscscan.", "jup.ag", "raydium.", "pump.fun", "coingecko.",
                 "coinmarketcap.", "youtube.", "medium.com", "github.com", "gitbook.io", "google.", "apple.com",
                 "instagram.", "tiktok.", "facebook.", "linkedin.", "reddit.", "uniswap.", "geckoterminal.",
                 "blockscout", "explorer", "torchpad.", "phantom.", "metamask.")
FREE_HOSTS = ("vercel.app", "netlify.app", "github.io", "pages.dev", "web.app", "firebaseapp.com", "surge.sh",
              "onrender.com", "replit.app", "streamlit.app", "glitch.me", "herokuapp.com", "railway.app")
BAUKASTEN = {"Lovable": ("lovable.dev", "lovable-uploads", "gpteng.co"), "v0": ("v0.dev", "v0-generated"),
             "Bolt": ("bolt.new", "stackblitz"), "Framer": ("framerusercontent.com",), "Wix": ("wix.com", "wixstatic"),
             "Carrd": ("carrd.co",), "Dorik": ("dorik.io",)}
SOL_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
GH_RE = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?")
GH_SKIP = {"sponsors", "orgs", "features", "about", "login", "topics", "marketplace", "settings", "apps", "solana-labs",
           "ethereum", "metamask", "phantom"}
LABELS = ("marketing", "team", "treasury", "dev", "developer", "burn", "liquidity", "airdrop", "cex", "reserve",
          "rewards", "community", "ecosystem", "buyback", "founder", "staking")
BEKANNT = {"11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
           "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
           "So11111111111111111111111111111111111111112", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
           "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}


# ── Hilfen ───────────────────────────────────────────────────────────────

def text_of(h):
    h = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", h or "")
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t\r\f\v]+", " ", h)
    return re.sub(r"\n\s*\n+", "\n", h).strip()


def title_of(h):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", h or "")
    return _html.unescape(m.group(1)).strip()[:120] if m else ""


def host_of(u):
    try:
        return (urlparse(u).hostname or "").lower()
    except Exception:
        return ""


def registrable(h):
    p = [x for x in (h or "").split(".") if x]
    if len(p) >= 3 and p[-2] in ("co", "com", "org", "net") and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def _get(url, timeout=12):
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        return r.status_code, (r.text or "")[:500_000]
    except Exception as e:
        return 0, str(e)[:100]


def _sicher(url):
    """Nur oeffentliche http(s)-Ziele (kein localhost, keine privaten Netze)."""
    try:
        import motor
        return motor._sicher(url)
    except Exception:
        return bool(re.match(r"^https?://[a-z0-9.-]+\.[a-z]{2,}", url or "", re.I))


def sub_links(base, h, limit=MAX_PAGES):
    reg = registrable(host_of(base))
    out, seen = [], set()
    for m in re.finditer(r"(?is)<a\s[^>]*href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", h or ""):
        href, label = m.group(1).strip(), text_of(m.group(2)).lower()
        if href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        u = urljoin(base, href)
        if not u.startswith("http"):
            continue
        hh = host_of(u)
        same = registrable(hh) == reg
        doc = any(hh.endswith(d) for d in DOC_HOSTS) or hh.startswith("docs.")
        if not (same or doc):
            continue
        path = urlparse(u).path.lower()
        own_sub = same and hh != host_of(base) and path in ("", "/")
        if not (doc or own_sub or any(w in path or w == label or w in label.split() for w in SUB)):
            continue
        k = u.rstrip("/").lower()
        if k in seen or k == base.rstrip("/").lower():
            continue
        seen.add(k)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def page_name(u, base):
    if u.rstrip("/") == (base or "").rstrip("/"):
        return "homepage"
    p = urlparse(u).path.lower().strip("/")
    for w in ("terms", "tos", "whitepaper", "litepaper", "tokenomics", "faq", "roadmap", "about", "docs", "legal", "disclaimer"):
        if w in p:
            return {"tos": "terms"}.get(w, w)
    h = host_of(u)
    if h.startswith("docs.") or any(h.endswith(d) for d in DOC_HOSTS):
        return "docs"
    return p.split("/")[-1][:20] or h


def partner_links(base, h, limit=4):
    reg = registrable(host_of(base))
    out, seen = [], set()
    for m in re.finditer(r"(?is)<a\s[^>]*href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>", h or ""):
        u = urljoin(base, m.group(1).strip())
        if not u.startswith("http"):
            continue
        hh = host_of(u)
        rg = registrable(hh)
        if not hh or rg == reg or rg in seen or any(x in hh for x in NICHT_PARTNER):
            continue
        seen.add(rg)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def nennt(text, names):
    t = (text or "").lower()
    for n in names:
        n = (n or "").strip().lower().lstrip("$")
        if len(n) >= 3 and re.search(r"(?<![a-z0-9])\$?" + re.escape(n) + r"(?![a-z0-9])", t):
            return True
    return False


def adressen(text, raw=""):
    blob = f"{text}\n{raw}"
    found = []
    for rx in (SOL_RE, EVM_RE):
        for a in rx.findall(blob):
            if a in BEKANNT or a in found:
                continue
            if not a.startswith("0x") and (not re.search(r"\d", a) or not re.search(r"[a-z]", a) or not re.search(r"[A-Z]", a)):
                continue
            found.append(a)
    return found[:20]


def label_bei(text, a):
    i = (text or "").find(a)
    if i < 0:
        return ""
    ctx = text[max(0, i - 60):i].split("\n")[-1].lower()
    best, pos = "", -1
    for w in LABELS:
        j = ctx.rfind(w)
        if j > pos:
            best, pos = w, j
    return {"developer": "dev"}.get(best, best)


def fingerabdruck(text, title=""):
    t = (text or "").lower()
    for w in re.findall(r"[a-z0-9]{3,}", (title or "").lower()):
        t = t.replace(w, " ")
    t = re.sub(r"\$[a-z0-9_]+", " ", t)
    t = SOL_RE.sub(" ", t)
    t = EVM_RE.sub(" ", t)
    t = re.sub(r"[^a-z ]+", " ", t)
    w = t.split()
    return hashlib.sha1(" ".join(w[:600]).encode()).hexdigest() if len(w) >= 120 else ""


def _tage(iso):
    try:
        import datetime as dt
        d0 = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return max(0, (dt.datetime.now(dt.timezone.utc) - d0).days)
    except Exception:
        return None


def _ago(ts):
    try:
        s = max(0, time.time() - (int(ts) / 1000 if int(ts) > 1e11 else int(ts)))
    except Exception:
        return ""
    return f"{int(s // 60)} min ago" if s < 3600 else f"{s / 3600:.0f} h ago" if s < 172800 else f"{s / 86400:.0f} days ago"


# ── Quellen ──────────────────────────────────────────────────────────────

def links(chain, token, markt=None, meta_uri=None):
    """Website, X, Telegram, GitHub aus DexScreener, pump.fun und den Metadaten."""
    out = {"websites": [], "x": None, "telegram": None, "github": None, "other": []}

    def nimm(art, u):
        u = str(u or "").strip()
        if not u:
            return
        ul = u.lower()
        if "x.com/" in ul or "twitter.com/" in ul:
            out["x"] = out["x"] or u
        elif "t.me/" in ul or "telegram" in art:
            out["telegram"] = out["telegram"] or u
        elif "github.com/" in ul:
            out["github"] = out["github"] or u
        elif art in ("website", "web") or ul.startswith("http"):
            if u not in out["websites"]:
                out["websites"].append(u)
    for k, u in ((markt or {}).get("links") or {}).items():
        nimm(k, u)
    if chain == "solana":
        try:
            r = requests.get(f"https://frontend-api-v3.pump.fun/coins/{token}", headers=UA, timeout=6)
            if r.status_code == 200:
                d = r.json() or {}
                nimm("website", d.get("website"))
                nimm("twitter", d.get("twitter"))
                nimm("telegram", d.get("telegram"))
        except Exception:
            pass
        if meta_uri:
            try:
                u = meta_uri.replace("ipfs://", "https://ipfs.io/ipfs/")
                d = requests.get(u, headers=UA, timeout=8).json()
                ext = d.get("extensions") or {}
                for k in ("website", "twitter", "telegram", "github"):
                    nimm(k, d.get(k) or ext.get(k))
            except Exception:
                pass
    return out


def _domain_alter(host):
    if any(host.endswith(h) for h in FREE_HOSTS):
        return None
    dom = registrable(host)
    try:
        r = requests.get(f"https://rdap.org/domain/{dom}", headers=UA, timeout=10)
        for ev in (r.json().get("events") or []) if r.status_code == 200 else []:
            if ev.get("eventAction") == "registration" and ev.get("eventDate"):
                return _tage(ev["eventDate"])
    except Exception:
        pass
    return None


def _x_handle(u):
    u = (u or "").strip()
    if u.startswith("@"):
        return re.sub(r"[^A-Za-z0-9_]", "", u[1:])[:15]
    m = re.match(r"(?i)https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/?(?:$|\?)", u)
    if m and m.group(1).lower() not in ("i", "home", "search", "intent", "share"):
        return m.group(1)
    m = re.match(r"(?i)https?://(?:www\.)?(?:x|twitter)\.com/i/communities/(\d+)", u)
    return ""


def _x_profil(handle):
    try:
        r = requests.get(f"https://api.fxtwitter.com/{handle}", headers=UA, timeout=10)
        u = (r.json() or {}).get("user") or {} if r.status_code == 200 else {}
        if not u:
            return {"handle": handle, "read": False}
        alter = None
        try:
            import email.utils as eu
            alter = int((time.time() - eu.parsedate_to_datetime(u.get("joined") or "").timestamp()) // 86400)
        except Exception:
            pass
        return {"handle": handle, "read": True, "name": u.get("name"), "followers": int(u.get("followers") or 0),
                "posts": int(u.get("tweets") or 0), "age_days": alter, "bio": (u.get("description") or "")[:200]}
    except Exception:
        return {"handle": handle, "read": False}


def _github(urls_or_html):
    m = None
    for mm in GH_RE.finditer(urls_or_html or ""):
        if mm.group(1).lower() not in GH_SKIP:
            m = mm
            break
    if not m:
        return None
    o, r = m.group(1), re.sub(r"\.git$", "", m.group(2) or "")
    h = {"Accept": "application/vnd.github+json", "User-Agent": UA["User-Agent"]}
    if os.getenv("GITHUB_TOKEN", "").strip():
        h["Authorization"] = "Bearer " + os.getenv("GITHUB_TOKEN").strip()
    out = {"repo": f"{o}/{r}" if r else o, "read": False}
    try:
        if not r:
            res = requests.get(f"https://api.github.com/users/{o}/repos", params={"sort": "pushed", "per_page": 5}, headers=h, timeout=10)
            if res.status_code == 404:
                return dict(out, missing=True)
            rs = res.json() if res.status_code == 200 else []
            if not rs:
                return dict(out, read=True, repos=0)
            j = rs[0]
            out["repo"] = j.get("full_name")
            out["repos"] = len(rs)
        else:
            res = requests.get(f"https://api.github.com/repos/{o}/{r}", headers=h, timeout=10)
            if res.status_code == 404:
                return dict(out, missing=True)
            if res.status_code != 200:
                return out
            j = res.json() or {}
        out.update(read=True, age_days=_tage(j.get("created_at")), push_days=_tage(j.get("pushed_at")),
                   stars=int(j.get("stargazers_count") or 0), language=j.get("language"),
                   fork_of=((j.get("parent") or {}).get("full_name") if j.get("fork") else ("a fork" if j.get("fork") else None)),
                   description=(j.get("description") or "")[:160])
        try:
            c = requests.get(f"https://api.github.com/repos/{out['repo']}/commits", params={"per_page": 1}, headers=h, timeout=10)
            lk = c.headers.get("Link") or ""
            m2 = re.search(r'page=(\d+)>; rel="last"', lk)
            out["commits"] = int(m2.group(1)) if m2 else (1 if c.status_code == 200 and c.json() else 0)
        except Exception:
            pass
    except Exception as e:
        log.debug(f"projekt github: {e}")
    return out


def _dex_bezahlt(chain, token):
    import scan
    dex = scan.KETTEN.get(chain, (None, None))[1]
    try:
        r = requests.get(f"https://api.dexscreener.com/orders/v1/{dex}/{token}", headers=UA, timeout=10)
        d = r.json() if r.status_code == 200 else []
    except Exception:
        d = []
    if isinstance(d, dict):
        d = d.get("orders") or []
    out = {"profile_paid": None, "cto": None, "ads": 0}
    for o in d if isinstance(d, list) else []:
        if o.get("status") not in ("approved", "processing", None):
            continue
        t = o.get("type")
        if t == "tokenProfile":
            out["profile_paid"] = _ago(o.get("paymentTimestamp"))
        elif t == "communityTakeover":
            out["cto"] = _ago(o.get("paymentTimestamp"))
        elif t in ("tokenAd", "trendingBarAd"):
            out["ads"] += 1
    return out


def _fingerabdruck_merken(fp, chain, token):
    if not fp:
        return 0
    try:
        d = json.loads(storage.kv_get("pr_fp_" + fp) or "[]")
    except Exception:
        d = []
    k = f"{chain}:{token}"
    andere = [x for x in d if x != k]
    if k not in d:
        d = (d + [k])[-20:]
        storage.kv_set("pr_fp_" + fp, json.dumps(d))
    return len(andere)


# ── KI: Behauptungen gegen Seite und Chain ───────────────────────────────

PROMPT = """You check a crypto token's own website for the reader before they buy.

You get: the text of up to eight pages of the project's site, and facts measured on chain. Today is {today}.

Do this:
1. Pull out every concrete claim: fees or taxes, total supply, team or dev allocation, liquidity locked or burned, mint or freeze authority, buyback or burn promises, audits, partners, roadmap dates, product or utility claims.
2. Compare each claim with the other pages and with the chain facts.
3. Report contradictions first, then claims the chain confirms, then notable facts (draft terms, roadmap dates already passed, big partner or audit names with no link to proof, a product that is promised but not shown).

Rules:
- Use ONLY the text and facts given. Never use outside knowledge. If something cannot be checked from what you got, leave it out.
- Quote numbers exactly as written and name the page they come from.
- No verdicts: never write scam, rug, safe, legit, trustworthy, or advice to buy or sell.
- Plain English, short sentences, no dashes.
- At most 8 findings. If the pages make no checkable claims, return an empty list.

Answer with JSON only:
{{"summary": "one sentence: what the project says it is", "findings": [{{"level": "warn" | "ok" | "info", "text": "...", "source": "homepage | terms | docs | ... | chain"}}]}}

PAGES:
{pages}

CHAIN FACTS:
{facts}"""

X_PROMPT = """Search X (Twitter) for what people are saying right now about this crypto token: {name} (${sym}), contract {ca} on {chain}{handle}.

Report, short and factual:
1. How active the talk is in the last 24 hours (rough number of distinct accounts and posts you found).
2. The notable accounts talking about it (handle, follower size if visible) and whether they look organic or paid.
3. What they say: the main claims, narrative, hype or concerns.
4. Red flags you can see: the same text copy pasted by many accounts, brand new accounts, bot like replies, an impersonated or different contract address being pushed.
Only use what you actually find. If you find little or nothing, say so plainly. No price predictions, no buy or sell advice, no dashes. Give links to the posts you used."""


def _ki(adr, prompt, modell, max_tokens=1200, web=None):
    import ai
    body = {"model": modell, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if web:
        body["plugins"] = [web]
    d = ai.anfrage(adr, None, body)
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    quellen = []
    for a in msg.get("annotations") or []:
        u = (a.get("url_citation") or {})
        if u.get("url") and u["url"] not in [q["url"] for q in quellen]:
            quellen.append({"title": (u.get("title") or "")[:80], "url": u["url"]})
    return msg.get("content") or "", float((d.get("usage") or {}).get("oddsengine_charged_usd") or 0), quellen


def _befunde(raw):
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None, []
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None, []
    out = []
    for f in (d.get("findings") or [])[:8]:
        if not isinstance(f, dict) or not f.get("text"):
            continue
        lv = str(f.get("level") or "info").lower()
        lv = lv if lv in ("warn", "ok", "info") else "info"
        t = str(f["text"]).replace("—", ",").replace("–", "-")[:300]
        if re.search(r"\b(scam|rug ?pull|is safe|legit|trustworthy)\b", t, re.I):
            continue
        out.append({"level": lv, "text": t, "source": str(f.get("source") or "")[:30]})
    return str(d.get("summary") or "")[:240], sorted(out, key=lambda f: {"warn": 0, "ok": 1, "info": 2}[f["level"]])


def x_puls(adr, r, handle=None):
    """Wer spricht gerade auf X darueber. Mit XAI_API_KEY echte X-Suche (xai.py), sonst Websuche."""
    import ai
    import xai
    if xai.bereit():
        thema = f"the crypto token {r.get('name') or ''} (${r.get('symbol') or ''}), contract {r['address']} on {r.get('chain_name') or r.get('chain')}"
        if handle:
            thema += f", official account @{handle}"
        d = xai.stimmung(adr, thema)
        if not d.get("error"):
            return {"text": d["text"][:2200], "sources": d["sources"][:8], "cost_usd": d["cost_usd"], "model": "xai x_search"}
        logging.getLogger("projekt").warning(f"x_puls xai: {d.get('error')}")
    modell = os.getenv("X_PULSE_MODEL", "").strip() or "x-ai/grok-4.7"
    web = {"id": "web", "engine": "native", "max_results": 6}
    try:
        if not ai.modell(modell):
            modell, web = os.getenv("MOTOR_FAST", "").strip() or "anthropic/claude-haiku-5.5", {"id": "web", "max_results": 6}
    except Exception:
        pass
    p = X_PROMPT.format(name=r.get("name") or r.get("symbol") or "", sym=r.get("symbol") or "", ca=r["address"],
                        chain=r.get("chain_name") or r.get("chain"), handle=f", official account @{handle}" if handle else "")
    try:
        txt, kosten, quellen = _ki(adr, p, modell, 700, web)
    except Exception as e:
        return {"error": str(e)[:160]}
    txt = txt.replace("—", ",").replace("–", "-").strip()
    return {"text": txt[:1800], "sources": [q for q in quellen if "x.com" in q["url"] or "twitter.com" in q["url"]][:8] or quellen[:6],
            "cost_usd": kosten, "model": modell}


# ── Alles zusammen ───────────────────────────────────────────────────────

def recherche(adr, q, frisch=False, mit_x=True):
    import scan
    r = scan.analyse(q, deep=False)
    if not r.get("ok"):
        return r
    chain, token = r["chain"], r["address"]
    key = f"pr_{chain}_{token}"
    if not frisch:
        try:
            c = json.loads(storage.kv_get(key) or "null")
            if c and time.time() - c.get("ts", 0) < CACHE_S:
                return dict(c, cached=True)
        except Exception:
            pass
    kosten = 0.0
    meta_uri = scan.sicherheit_sol(token).get("uri") if chain == "solana" else None
    L = links(chain, token, r.get("market"), meta_uri)
    d = {"ts": int(time.time()), "chain": chain, "token": token, "symbol": r.get("symbol"), "name": r.get("name"),
         "links": L, "pages": [], "dead": []}
    site = next((u for u in L["websites"] if _sicher(u)), None)
    seiten = []
    # Seit 2.40 parallel: X-Puls, X-Profil, Telegram, bezahlte Sichtbarkeit und Domain-Alter laufen neben dem Lesen der Seite
    pool = ThreadPoolExecutor(6)
    xh = _x_handle(L.get("x") or "")
    f_puls = pool.submit(x_puls, adr, r, xh) if mit_x else None
    f_xprof = pool.submit(_x_profil, xh) if xh else None
    f_dex = pool.submit(_dex_bezahlt, chain, token)
    f_alter = pool.submit(_domain_alter, host_of(site)) if site else None
    tg = L.get("telegram")
    tg_url = (tg if tg.startswith("http") else f"https://t.me/{tg.lstrip('@')}") if tg else None
    f_tg = pool.submit(_get, tg_url) if tg_url else None
    if site:
        st, home = _get(site)
        if st == 200:
            seiten.append({"name": "homepage", "url": site, "text": text_of(home), "_h": home})
            subs = [u for u in sub_links(site, home) if _sicher(u)]
            with ThreadPoolExecutor(6) as ex:
                got = list(ex.map(_get, subs))
            for u, (s2, h2) in zip(subs, got):
                if s2 == 200 and h2:
                    seiten.append({"name": page_name(u, site), "url": u, "text": text_of(h2), "_h": h2})
                else:
                    d["dead"].append(f"{page_name(u, site)} page answers {s2 or 'nothing'}")
            have = {p["url"].rstrip("/").lower() for p in seiten}
            gesehen = {hashlib.md5(p["text"][:4000].encode()).hexdigest() for p in seiten}
            pu = urlparse(site)
            proben = [f"{pu.scheme}://{pu.hostname}{p}" for p in PROBE]
            reg = registrable(pu.hostname or "")
            if reg and not (pu.hostname or "").startswith("docs."):
                proben.append(f"https://docs.{reg}")
            proben = [u for u in proben if u.rstrip("/").lower() not in have]
            if len(seiten) < MAX_PAGES:
                with ThreadPoolExecutor(6) as ex:
                    pg = list(ex.map(_get, proben))
                for u, (s4, h4) in zip(proben, pg):
                    if len(seiten) >= MAX_PAGES:
                        break
                    if s4 != 200 or not h4:
                        continue
                    t4 = text_of(h4)
                    hx = hashlib.md5(t4[:4000].encode()).hexdigest()
                    if len(t4) < 300 or hx in gesehen:
                        continue
                    gesehen.add(hx)
                    seiten.append({"name": page_name(u, site), "url": u, "text": t4, "_h": h4})
            namen = [r.get("symbol") or "", registrable(host_of(site)).split(".")[0], (title_of(home).split("|")[0].split(" - ")[0].strip())[:30]]
            pl = [u for u in partner_links(site, home) if _sicher(u)]
            if pl:
                with ThreadPoolExecutor(4) as ex:
                    pr = list(ex.map(_get, pl))
                d["partners"] = [{"host": registrable(host_of(u)), "read": s5 == 200 and bool(h5),
                                  "names_back": bool(s5 == 200 and nennt(text_of(h5), namen))} for u, (s5, h5) in zip(pl, pr)]
            roh = home.lower()
            d["builder"] = next((n for n, zeichen in BAUKASTEN.items() if any(z in roh for z in zeichen)), None)
        else:
            d["dead"].append(f"Website answers {st or 'nothing'}")
        h = host_of(site)
        d["website"] = {"url": site, "host": h, "domain_age_days": f_alter.result() if f_alter else None,
                        "free_host": any(h.endswith(x) for x in FREE_HOSTS)}
    # X-Konto
    if f_xprof:
        d["x"] = f_xprof.result()
    elif L.get("x"):
        d["x"] = {"link": L["x"], "read": False, "community": "/communities/" in L["x"]}
    # Telegram
    if f_tg:
        s3, h3 = f_tg.result()
        m = re.search(r"(?is)tgme_page_extra[^>]*>\s*([\d\s,.]+)\s*(members|subscribers)", h3 or "")
        d["telegram"] = {"link": tg_url, "alive": s3 == 200 and "tgme_page_title" in (h3 or ""),
                         "members": int(re.sub(r"[^\d]", "", m.group(1))) if m else None}
    # GitHub (verlinkt in den Links oder auf der Seite)
    d["github"] = _github((L.get("github") or "") + "\n" + "\n".join(p["_h"] for p in seiten))
    d["dex"] = f_dex.result()
    # CA auf der Seite?
    alles = "\n".join(p["text"] for p in seiten)
    roh = "\n".join(p["_h"] for p in seiten)
    gef = adressen(alles, roh)
    evm = token.startswith("0x")
    gleich = [a for a in gef if a.startswith("0x") == evm]
    if any(a.lower() == token.lower() for a in gleich):
        d["ca_on_site"] = "match"
    elif gleich:
        cas = [a for a in gleich if re.search(r"(?i)(ca|contract|token address|mint)[^\n]{0,40}" + re.escape(a), alles)]
        if cas:
            d["ca_on_site"], d["other_cas"] = "other", cas[:3]
    if seiten and not d.get("ca_on_site"):
        d["ca_on_site"] = "missing"
    d["named_wallets"] = [{"address": a, "label": label_bei(alles, a)} for a in gleich
                          if a.lower() != token.lower() and a not in d.get("other_cas", []) and label_bei(alles, a)][:4]
    terms = [p for p in seiten if p["name"] in ("terms", "legal", "disclaimer")]
    d["terms"] = "none" if seiten and not terms else ("draft" if terms and re.search(r"(?i)\bdraft\b", " ".join(p["text"][:3000] for p in terms)) else ("present" if terms else None))
    if seiten:
        d["same_site_as"] = _fingerabdruck_merken(fingerabdruck(seiten[0]["text"], title_of(seiten[0]["_h"])), chain, token)
    # KI-Vergleich
    if seiten:
        import datetime as dt
        fakten = {"chain": r.get("chain_name"), "symbol": r.get("symbol"), "market": {k: (r.get("market") or {}).get(k) for k in ("price", "mcap", "liq")},
                  "safety": r.get("safety"), "supply": {k: v for k, v in (r.get("supply") or {}).items() if k != "top"},
                  "dev": r.get("dev"), "age": r.get("age")}
        teile = [f"--- {p['name']} ({p['url']}) ---\n{p['text'][:PAGE_CHARS]}" for p in seiten]
        modell = os.getenv("PROJECT_MODEL", "").strip() or os.getenv("MOTOR_FAST", "").strip() or "anthropic/claude-haiku-5.5"
        try:
            raw, k, _ = _ki(adr, PROMPT.format(today=dt.date.today().isoformat(), pages="\n\n".join(teile),
                                               facts=json.dumps(fakten, default=str)[:6000]), modell, 1400)
            kosten += k
            d["summary"], d["findings"] = _befunde(raw)
        except Exception as e:
            d["ai_note"] = str(e)[:160]
    d["pages"] = [{"name": p["name"], "url": p["url"]} for p in seiten]
    # X-Puls
    if f_puls:
        xp = f_puls.result()
        if xp.get("cost_usd"):
            kosten += xp["cost_usd"]
        d["x_pulse"] = xp
    d["cost_usd"] = round(kosten, 5)
    pool.shutdown(wait=False)
    try:
        storage.kv_set(key, json.dumps(d, default=str))
    except Exception:
        pass
    return d


# ── Karte ────────────────────────────────────────────────────────────────

def karte(d):
    if d.get("error"):
        return d["error"]
    z = [f"Project research ${d.get('symbol') or ''}".strip()]
    if d.get("summary"):
        z.append(d["summary"])
    w = d.get("website") or {}
    z.append("")
    z.append("Website")
    if w:
        bits = [w.get("host")]
        if w.get("domain_age_days") is not None:
            bits.append(f"domain {w['domain_age_days']} days old")
        if w.get("free_host"):
            bits.append("free hosting, no own domain")
        if d.get("builder"):
            bits.append(f"built with {d['builder']}")
        z.append(" · ".join(b for b in bits if b))
        if d.get("pages"):
            z.append("Read: " + ", ".join(p["name"] for p in d["pages"]))
        ca = d.get("ca_on_site")
        if ca == "match":
            z.append("✓ This contract is on the site")
        elif ca == "other":
            z.append("! The site shows a DIFFERENT contract: " + ", ".join(x[:6] + "…" + x[-4:] for x in d.get("other_cas", [])))
        elif ca == "missing":
            z.append("! The contract is not on the site")
        if d.get("terms") == "none":
            z.append("! No terms or legal page")
        elif d.get("terms") == "draft":
            z.append("! Terms are marked as draft")
        for x in d.get("dead") or []:
            z.append("! " + x)
        if d.get("same_site_as"):
            z.append(f"! Same site text as {d['same_site_as']} other coin{'s' if d['same_site_as'] != 1 else ''} we checked")
    else:
        z.append("! No website linked")
    if d.get("named_wallets"):
        z.append("Wallets named on the site: " + ", ".join(f"{w['label']} {w['address'][:4]}…{w['address'][-4:]}" for w in d["named_wallets"]))
    if d.get("findings"):
        z += ["", "Claims checked"]
        for f in d["findings"][:6]:
            z.append({"warn": "! ", "ok": "✓ ", "info": "· "}[f["level"]] + f["text"] + (f" ({f['source']})" if f.get("source") else ""))
    for p in d.get("partners") or []:
        if p.get("read"):
            z.append(("✓ " if p["names_back"] else "! ") + f"{p['host']} {'names this project back' if p['names_back'] else 'does not mention this project'}")
    x = d.get("x") or {}
    z += ["", "X"]
    if x.get("read"):
        z.append(f"@{x['handle']}: {x['followers']:,} followers · {x.get('posts', 0):,} posts" + (f" · account {x['age_days']} days old" if x.get("age_days") is not None else ""))
    elif x.get("community"):
        z.append("X community link, no own account")
    elif x.get("handle"):
        z.append(f"@{x['handle']} could not be read")
    else:
        z.append("! No X account linked")
    xp = d.get("x_pulse") or {}
    if xp.get("text"):
        z.append(xp["text"][:900])
        for q in (xp.get("sources") or [])[:3]:
            z.append(q["url"])
    tg = d.get("telegram")
    z += ["", "Telegram"]
    if tg:
        z.append(("Group is live" if tg["alive"] else "! Link is dead") + (f" · {tg['members']:,} members" if tg.get("members") else ""))
    else:
        z.append("No Telegram linked")
    g = d.get("github")
    z += ["", "GitHub"]
    if not g:
        z.append("No GitHub linked")
    elif g.get("missing"):
        z.append(f"! Linked GitHub {g['repo']} does not exist")
    elif g.get("read"):
        s = f"{g['repo']}"
        if g.get("age_days") is not None:
            s += f" · {g['age_days']} days old"
        if g.get("push_days") is not None:
            s += f" · last change {g['push_days']} days ago"
        if g.get("commits") is not None:
            s += f" · {g['commits']} commits"
        s += f" · {g.get('stars', 0)} stars"
        if g.get("fork_of"):
            s = "! " + s + f" · a copy of {g['fork_of']}"
        z.append(s)
    else:
        z.append(f"{g.get('repo')} could not be read")
    dx = d.get("dex") or {}
    bits = []
    if dx.get("profile_paid"):
        bits.append(f"DEX profile paid {dx['profile_paid']}")
    if dx.get("cto"):
        bits.append(f"community takeover {dx['cto']}")
    if dx.get("ads"):
        bits.append(f"{dx['ads']} ads")
    z += ["", "Paid visibility: " + (", ".join(bits) if bits else "none on DexScreener")]
    if d.get("cost_usd"):
        z.append(f"Research cost ${d['cost_usd']:.3f}")
    return "\n".join(z)


def fuer_modell(d):
    if d.get("error") or not d.get("ts"):
        return d
    out = {k: v for k, v in d.items() if k not in ("links",)}
    out["shown_to_user"] = ("The research card is ALREADY shown to the user. Do not repeat it. Add at most two lines: "
                            "the single most important finding and what it means for them. No buy or sell advice.")
    return out
