"""
x_post.py: postet Signale und Ergebnisse direkt auf X.

X API v2, POST /2/tweets, angemeldet als der eigene Account (OAuth 1.0a,
User Context). Die vier Schluessel aus dem X Developer Portal liegen
ausschliesslich in Railway, nie im Repo:

    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET

Kosten (Stand Sept. 2026, Guthaben im Voraus): 0,015 $ je Post ohne Link,
0,20 $ mit Link. Deshalb enthalten die Posts bewusst KEINEN Link.
Ergebnisse gehen als Antwort unter das Signal, damit beides zusammensteht.
"""
import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse
from datetime import datetime, timezone

import requests

import storage
from config import cfg
from logger import log

URL = "https://api.twitter.com/2/tweets"


def bereit():
    return bool(cfg.x_post_enabled and cfg.x_api_key and cfg.x_api_secret
                and cfg.x_access_token and cfg.x_access_secret)


def status():
    """Ein Satz fuers Log: postet X, und wenn nicht, woran es haengt.
    Nennt nur die NAMEN fehlender Variablen, nie einen Wert."""
    if not cfg.x_post_enabled:
        return "aus (X_POST_ENABLED ist nicht true)"
    fehlt = [n for n, v in (("X_API_KEY", cfg.x_api_key), ("X_API_SECRET", cfg.x_api_secret),
                            ("X_ACCESS_TOKEN", cfg.x_access_token),
                            ("X_ACCESS_SECRET", cfg.x_access_secret)) if not v]
    if fehlt:
        return "aus (fehlt: " + ", ".join(fehlt) + ")"
    return f"an, hoechstens {cfg.x_max_per_day} Posts am Tag, heute bisher {_heute_gepostet()}"


def _q(s):
    return urllib.parse.quote(str(s), safe="~")


def _auth_header(method, url):
    o = {
        "oauth_consumer_key": cfg.x_api_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": cfg.x_access_token,
        "oauth_version": "1.0",
    }
    # JSON-Body zaehlt bei OAuth 1.0a nicht zur Signatur.
    param = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(o.items()))
    basis = "&".join([method.upper(), _q(url), _q(param)])
    schluessel = f"{_q(cfg.x_api_secret)}&{_q(cfg.x_access_secret)}"
    sig = base64.b64encode(hmac.new(schluessel.encode(), basis.encode(),
                                    hashlib.sha1).digest()).decode()
    o["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(o.items()))


def _heute_gepostet():
    heute = datetime.now(timezone.utc).date().isoformat()
    try:
        return int(storage.kv_get(f"x_posts_{heute}") or 0)
    except Exception:
        return 0


def _zaehlen():
    heute = datetime.now(timezone.utc).date().isoformat()
    try:
        storage.kv_set(f"x_posts_{heute}", _heute_gepostet() + 1)
    except Exception:
        pass


def post(text, reply_to=None, quote=None):
    """Postet auf X. Gibt die Post-ID zurueck, sonst None."""
    if not bereit():
        return None
    if _heute_gepostet() >= cfg.x_max_per_day:
        log.warning(f"x: Tageslimit {cfg.x_max_per_day} erreicht, nicht gepostet")
        return None
    body = {"text": text[:280]}
    if quote:
        # Zitat statt Antwort: steht im Profil unter "Posts", mit dem Call
        # sichtbar darunter. Antworten sieht man nur im Reiter "Replies".
        body["quote_tweet_id"] = str(quote)
    elif reply_to:
        body["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    try:
        r = requests.post(URL, json=body, timeout=20,
                          headers={"Authorization": _auth_header("POST", URL)})
        if r.status_code in (200, 201):
            tid = (r.json().get("data") or {}).get("id")
            _zaehlen()
            log.info(f"x: gepostet {tid}" + (f" (zitiert {quote})" if quote else f" (Antwort auf {reply_to})" if reply_to else ""))
            return tid
        log.error(f"x {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log.error(f"x: {e}")
    return None
