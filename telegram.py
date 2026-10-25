"""
Telegram.

THE LINE
--------
The past is free. The present is paid.

A delayed teaser was the wrong idea and got thrown out. These markets run 12
to 24 hours ahead, so ninety minutes of delay protects nothing, and naming the
block gives the trade away regardless: "Seoul, 27-31C" is everything a reader
needs, and the prices are sitting in the market for free. Withholding our own
prices while naming the range is theatre.

So:

  live signal   ->  TELEGRAM_PAID_CHANNEL_ID    subscribers only, the product
  once settled  ->  TELEGRAM_PUBLIC_CHANNEL_ID  what we said, what happened

A settled signal has no trading value left and all of the evidence value.
That is exactly the thing worth giving away, and it is what sells the rest.

MODES
-----
  observe      private chat, nobody sees it
  calibration  public channel, honest footer
  live         public channel, no footer

BILLING
-------
Telegram does it. create_subscription_link() makes an invite link with a price
in Stars; Telegram charges, adds the member, removes them when it lapses. No
payment code, no custody, no chargebacks.
"""

import requests

from config import cfg
from logger import log

API = "https://api.telegram.org/bot{token}/{method}"


def _ohne_striche(text):
    """Keine langen Striche in Nachrichten, sie lesen sich maschinell."""
    return (text.replace("———", "──────────────")
                .replace(" — ", ": ").replace(" – ", ": ")
                .replace("—", ",").replace("–", "-"))


# (chat, message_id) der zuletzt gesendeten Nachricht. Wer eine Antwort
# darauf setzen will (Ergebnis unter dem Signal), liest das direkt danach.
letzte = None


def send(text, buttons=None, chat=None, reply_to=None):
    """buttons: list of (label, url), rendered as a tappable inline keyboard.
    reply_to: message_id in DIESEM chat. Dann erscheint die Nachricht als
    Antwort darunter, wie beim FINNPUTER Trade Bot. Ist die Nachricht weg,
    geht sie trotzdem raus, nur ohne Verweis."""
    global letzte
    letzte = None
    if not cfg.tg_token:
        log.info("no TELEGRAM_BOT_TOKEN, printing instead\n" + text)
        for t, u in (buttons or []):
            log.info(f"   [ {t} ] -> {u}")
        return False
    chat = chat or cfg.chat_id()
    if not chat:
        log.warning(f"no chat id for SIGNAL_MODE={cfg.signal_mode}")
        return False

    if cfg.signal_mode == "observe":
        text = "🔬 OBSERVE, private test run, not published\n\n" + text
    elif cfg.signal_mode == "calibration":
        text = (text + "\n\n———\nCALIBRATION\nThe engine is being scored in "
                "public. This signal was published before the station "
                "reported, and we post the result either way, hit or miss.\n"
                "oddsengine.live")

    text = _ohne_striche(text)
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if reply_to:
        payload["reply_parameters"] = {"message_id": int(reply_to),
                                       "allow_sending_without_reply": True}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]
        }
    try:
        r = requests.post(API.format(token=cfg.tg_token, method="sendMessage"),
                          json=payload, timeout=20)
        if r.status_code != 200:
            log.error(f"telegram {r.status_code}: {r.text[:200]}")
            return False
        try:
            letzte = [str(chat), int(r.json()["result"]["message_id"])]
        except Exception:
            letzte = None
        return True
    except Exception as e:
        log.error(f"telegram send failed: {e}")
        return False


def send_paid(text, buttons=None):
    """The live signal. Subscribers only."""
    return send(text, buttons, chat=cfg.paid_chat_id() or cfg.chat_id())


def send_free(text):
    """The public channel. Only ever things that have already settled."""
    return send(text, chat=cfg.tg_public or cfg.chat_id())


def send_summary(scanned, signals, skipped, resolved=None):
    """
    Silence is the message.

    A channel announcing "0 signals" every three hours teaches people to scroll
    past it, and then they scroll past the one that mattered. Quiet scans say
    nothing at all. Most days producing nothing is the pitch, not a fault.

    When we DO speak, the rejection count is the product, not an aside: the
    reader should see that almost everything was turned down before one thing
    was not. That is the whole difference between a signal and a tip.
    """
    if not signals:
        return False
    D = "──────────────"
    pct = (skipped / scanned * 100) if scanned else 0.0
    # Seit 2.17.2: der Text passt zum Holder-Modell. Frueher stand hier, das
    # Signal gehe "in den Kanal", was seit 2.15 nicht mehr stimmt.
    wo = ("The full call is live in the app for $ODDS holders."
          if cfg.odds_mint else "The full call is live in the app.")
    L = ["ODDSENGINE · SCAN", "", D, "",
         f"{signals} new call{'s' if signals != 1 else ''}",
         f"{scanned} markets checked · {skipped} turned down ({pct:.0f}%)",
         "", D, "",
         "Most markets are priced about right. The engine only calls the few "
         "that are not.",
         "",
         wo + " One call a day is posted here for free. Every result gets "
         "posted, hit or miss.",
         "", "oddsengine.live"]
    return send("\n".join(L))


def scored_post(res, join_url=None):
    """
    What the free channel gets: a signal after it settled.

    No trading value left, all of the evidence value. Misses go out exactly
    like hits, which is the entire reason anyone should believe the rest.
    """
    hit = res.get("outcome_hit")
    legs = res.get("said_legs") or []
    said = res.get("said_coverage")

    lines = [f"{'HIT' if hit else 'MISS'}   {(res.get('city') or '').upper()}   "
             f"{res.get('date')}", "",
             f"We said: {' · '.join(legs)}"]
    if said is not None:
        lines.append(f"We gave it: {said * 100:.0f} out of 100")
    lines += [f"It came in at: {res.get('outcome_label')}", ""]
    lines.append("Landed inside the range." if hit
                 else "Landed outside. The stake was gone.")

    # Ein Markt kann waehrend des Tages mehrfach veroeffentlicht worden sein.
    # Gewertet wird die letzte Fassung -- aber wer frueh gelesen und gekauft
    # hat, hielt die erste. Gehen die beiden auseinander, muss das hier stehen,
    # sonst schreiben wir einem Abonnenten ein Ergebnis zu, das er nie hatte.
    ersteb = res.get("first_legs") or []
    ersteh = res.get("first_hit")
    fassungen = res.get("versions") or 1
    if ersteb and ersteh is not None and ersteh != hit and ersteb != legs:
        lines += ["",
                  f"Our first call that day was: {' · '.join(ersteb)}",
                  f"That one {'landed' if ersteh else 'did not land'}. "
                  f"We updated {fassungen - 1} time"
                  f"{'s' if fassungen > 2 else ''} before the close, and the "
                  f"{'update' if hit else 'last update'} "
                  f"{'saved it' if hit else 'moved away from the answer'}.",
                  "Both are in the record. We score the last one."]

    # Die Zahl, auf die es ankommt. Eine Trefferquote ohne den Preis sagt
    # nichts: ein Block fuer 90 Cent, der zu 90 Prozent trifft, verdient
    # nichts. Weil der Einsatz nach Preis verteilt wird, zahlt jedes Bein
    # dasselbe zurueck — die Rendite haengt also nur am Blockpreis und gilt
    # fuer jede Einsatzhoehe gleichermassen.
    kosten = res.get("cost")
    rendite = res.get("pnl_pct")
    if rendite is None and kosten:
        rendite = (1.0 / kosten - 1.0) * 100 if hit else -100.0
    if rendite is not None:
        einsatz = res.get("budget")
        zurueck = res.get("payout")
        if einsatz and zurueck is not None:
            lines += ["", f"Return: {rendite:+.0f}%  "
                          f"(${zurueck:.2f} back on ${einsatz:.2f} staked)"]
        else:
            lines += ["", f"Return: {rendite:+.0f}% on the stake"]
        if kosten:
            lines.append(f"The block cost {kosten * 100:.0f}c, so it had to hit "
                         f"{kosten * 100:.0f}% of the time just to break even.")

    lines += ["",
              "$ODDS holders had this in the app before the station reported.",
              "", join_url or "oddsengine.live"]
    return "\n".join(lines)


def send_record(st):
    """
    The one post that is the product: what we said, what happened.

    Not a claim. A count of markets that already settled, next to the number
    we published before they did.
    """
    if not st or not st.get("signals_resolved"):
        return False
    # Doppelte Sicherung: ohne ausdrueckliches RECORD_POST_EVERY > 0 nie posten.
    if int(getattr(cfg, "record_post_every", 0) or 0) <= 0:
        return False
    n = st["signals_resolved"]
    hit = st.get("hit_rate")
    if hit is None:
        return False
    # avg_said wird bewusst nicht mehr gebraucht. Frueher blockierte ein
    # fehlender Wert den ganzen Post; ausserdem stand die Soll-Ist-Rechnung
    # ("wir sagten 77, es kamen 68") direkt im Kanal und der Leser hat die
    # Luecke selbst gebildet. Was gezaehlt wird, sind Treffer und Fehlschlaege.
    treffer = st.get("hits")
    if treffer is None:
        treffer = round(hit * n)
    daneben = n - treffer

    lines = ["ODDSENGINE, THE RECORD", "",
             f"{n} signal{'s' if n != 1 else ''} published and settled.", "",
             f"Hit    {treffer}",
             f"Miss   {daneben}", "",
             f"{hit * 100:.0f} out of 100.", ""]

    # Tage mit weniger als drei Aufloesungen sind Rauschen -- vor allem der
    # laufende Tag, der sonst als 0 oder 100 in der Reihe stuende.
    tage = [t for t in (st.get("by_day") or []) if t.get("n", 0) >= 3]
    if len(tage) >= 3:
        lines += ["Hit rate by day:  "
                  + "  ".join(str(t["pct"]) for t in tage[-7:]), ""]

    net = st.get("net_pct")
    if net is not None:
        lines += [f"Every 10 staked returned {10 * (1 + net / 100):.2f}.", ""]
    streak = st.get("longest_losing_streak")
    if streak:
        lines += [f"Longest run of misses: {streak} in a row.", ""]
    lines += ["Every one of these was posted before the station reported.",
              "The misses are in the count.",
              "", "oddsengine.live"]
    return send_free("\n".join(lines))


def create_subscription_link(months=1, stars=0):
    """
    Telegram bills it. We never touch a card, a wallet or an invoice.

    createChatInviteLink with subscription_period and subscription_price:
    Telegram charges the Stars, adds the member, removes them when it lapses.

        python -c "import telegram; print(telegram.create_subscription_link(1, 250))"
    """
    if not (cfg.tg_token and cfg.tg_paid):
        log.error("need TELEGRAM_BOT_TOKEN and TELEGRAM_PAID_CHANNEL_ID")
        return None
    try:
        r = requests.post(
            API.format(token=cfg.tg_token, method="createChatInviteLink"),
            json={"chat_id": cfg.tg_paid,
                  "subscription_period": 2592000 * months,
                  "subscription_price": stars,
                  "name": f"OddsEngine {months}m"},
            timeout=20)
        d = r.json()
        if not d.get("ok"):
            log.error(f"telegram: {d}")
            return None
        return d["result"]["invite_link"]
    except Exception as e:
        log.error(f"invite link failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────
# Angeheftete Live-Tafel
#
# Ein Kanal laesst sich nicht sortieren. Themen und Unterordner gibt es nur
# in Gruppen, und der Feed bleibt chronologisch. Also drehen wir es um: EINE
# Nachricht ganz oben, die bei jedem Scan ueberschrieben wird. Wer den Kanal
# oeffnet, sieht sofort, was laeuft — statt durch dreissig Nachrichten zu
# scrollen.
#
# Bearbeiten loest keine Benachrichtigung aus, die Abonnenten werden also
# nicht gestoert.
# ─────────────────────────────────────────────────────────────────────────

def _tafel_text(offen, bilanz=None):
    """Die Tafel als fertiger Text. offen ist eine Liste von Schnappschuessen."""
    from datetime import datetime, timezone
    L = ["\U0001F4CC  LIVE \u00b7 " +
         datetime.now(timezone.utc).strftime("%d %b %H:%M") + " UTC", ""]
    if not offen:
        L.append("Nothing open right now.")
        L.append("Most days there is nothing worth posting.")
    else:
        for s in offen:
            legs = " \u00b7 ".join(str(x) for x in (s.get("legs") or []))
            cov = s.get("coverage")
            lead = s.get("lead_h")
            L.append(f"{(s.get('city') or '').title():<11}"
                     f"{(s.get('date') or '')[5:]:<7}{legs}")
            L.append(f"{'':<11}we say {cov*100:.0f}%"
                     + (f"   {lead:.0f}h left" if lead is not None else ""))
            L.append("")
    if bilanz:
        L.append(bilanz)
        L.append("")
    L.append("Every call and every miss: public.oddsengine.live")
    return "\n".join(L)


_TAFEL = {"mid": None}


def live_board(offen, bilanz=None):
    """Tafel anlegen oder aktualisieren. Gibt True zurueck, wenn es geklappt hat.

    Die Nachrichten-ID liegt im Schluessel-Wert-Speicher. Ist die Nachricht
    weg (geloescht, Kanal gewechselt), wird eine neue angelegt und angeheftet.
    """
    import storage
    if not cfg.tg_token or not cfg.tg_paid:
        return False
    text = _tafel_text(offen, bilanz)
    mid = storage.kv_get("live_board_msg_id") or _TAFEL["mid"]
    # Ohne Datenbank keine neue Tafel anlegen: sonst entsteht bei jedem Scan
    # eine neue, angeheftete Nachricht im Kanal (passiert beim Railway-Ausfall).
    if not mid and not storage.kv_alive():
        log.warning("live board: Datenbank nicht erreichbar, keine neue Tafel")
        return False

    if mid:
        try:
            r = requests.post(
                API.format(token=cfg.tg_token, method="editMessageText"),
                json={"chat_id": cfg.tg_paid, "message_id": int(mid),
                      "text": text, "disable_web_page_preview": True},
                timeout=15)
            if r.ok and r.json().get("ok"):
                return True
            beschreibung = (r.json() or {}).get("description", "")
            # "message is not modified" ist kein Fehler, es gab nur nichts Neues
            if "not modified" in beschreibung:
                return True
            log.info(f"live board: edit fehlgeschlagen ({beschreibung[:80]}), "
                     f"lege eine neue an")
        except Exception as e:
            log.debug(f"live board edit: {e}")

    try:
        r = requests.post(
            API.format(token=cfg.tg_token, method="sendMessage"),
            json={"chat_id": cfg.tg_paid, "text": text,
                  "disable_web_page_preview": True,
                  "disable_notification": True},
            timeout=15)
        d = r.json()
        if not d.get("ok"):
            log.warning(f"live board: {str(d)[:120]}")
            return False
        neu_id = d["result"]["message_id"]
        _TAFEL["mid"] = neu_id
        storage.kv_set("live_board_msg_id", neu_id)
        try:
            requests.post(API.format(token=cfg.tg_token, method="pinChatMessage"),
                          json={"chat_id": cfg.tg_paid,
                                "message_id": neu_id,
                                "disable_notification": True}, timeout=15)
        except Exception:
            pass
        log.info(f"live board angelegt und angeheftet (id {neu_id})")
        return True
    except Exception as e:
        log.warning(f"live board: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────
#  X-Entwuerfe
# ─────────────────────────────────────────────────────────────────────
#
# X erlaubt 280 Zeichen und die API kostet Geld. Deshalb schreibt der
# Worker zu jedem Signal und jedem Ergebnis eine fertige Kurzfassung in den
# privaten Chat. Ohne Vorspann, ohne Knoepfe: lange druecken, kopieren,
# posten. Das Ergebnis wird auf X als Antwort unter das Signal gesetzt.

_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _tag(datum):
    try:
        j, m, t = str(datum).split("-")
        return f"{_MON[int(m) - 1]} {int(t)}"
    except Exception:
        return str(datum)


def _kurz(legs):
    """27°C · 28°C · 29°C  ->  27 / 28 / 29°C"""
    legs = [str(x) for x in (legs or [])]
    if not legs:
        return ""
    einheit = "°F" if legs[0].endswith("°F") else ("°C" if legs[0].endswith("°C") else "")
    if einheit and all(x.endswith(einheit) for x in legs):
        return " / ".join(x[: -len(einheit)] for x in legs) + einheit
    return " / ".join(legs)


def _stadt_tag(city):
    return "#" + "".join(w.capitalize() for w in str(city or "").split())


def x_signal(snap):
    cost = snap.get("cost") or 0
    zeilen = [
        f"🌡️ {str(snap.get('city') or '').upper()} · {_tag(snap.get('date'))} · daily high",
        "",
        f"Block: {_kurz(snap.get('legs'))}",
    ]
    if snap.get("grade", "bet") == "bet":
        zeilen.append(f"Price {cost * 100:.0f}c · honest odds {(snap.get('coverage') or 0) * 100:.0f}%")
        if cost:
            zeilen.append(f"Pays {1 / cost:.2f}x if the high lands in the block.")
    else:
        zeilen += [f"Engine read, no bet. Price {cost * 100:.0f}c · model "
                   f"{(snap.get('coverage_engine') or 0) * 100:.0f}%",
                   "The market already has it. We post it anyway and score it."]
    zeilen += ["", "Result under this post once the station reports.",
               "", f"{_stadt_tag(snap.get('city'))} #Polymarket"]
    return _ohne_striche("\n".join(zeilen))


def _wann(ts):
    """2026-10-06T08:43:11+00:00 -> Oct 6, 08:43 UTC"""
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
        return f"{_MON[t.month - 1]} {t.day}, {t:%H:%M} UTC"
    except Exception:
        return None


def x_result(res, bilanz=None, link=False, holder=False):
    """Das Ergebnis, kurz. Seit 2.17.3 fuer X UND fuer den Telegram-Kanal,
    damit ueberall dasselbe steht. Die Zeile "Called ..." sagt, wann der Call
    rausging und wo er stand: sonst wirkt ein Ergebnis zu einem Call, den im
    Kanal niemand gesehen hat, wie aus dem Nichts.
    link=True nur fuer Telegram: auf X kostet ein Post mit Link 0,20 $."""
    hit = res.get("outcome_hit")
    cost = res.get("cost")
    zeilen = [
        f"{'✅ HIT' if hit else '❌ MISS'} · {str(res.get('city') or '').upper()} · {_tag(res.get('date'))}",
        "",
        f"High came in at {res.get('outcome_label')}",
        f"Our block: {_kurz(res.get('said_legs'))}",
    ]
    if cost:
        zeilen.append(f"Entry {cost * 100:.0f}c · " +
                      (f"paid {1 / cost:.2f}x" if hit else "stake lost"))
    wann = _wann(res.get("published_ts"))
    if wann:
        if holder:
            # Seit 2.25: Holder-Calls kommen nach dem Ergebnis einzeln raus.
            wo = f"Holders had this call {wann}, before the station reported."
        elif res.get("free"):
            wo = f"Called {wann}. This was the free call of the day."
        elif cfg.odds_mint:
            wo = f"Called {wann}, live in the app for $ODDS holders."
        else:
            wo = f"Called {wann}, live in the app."
        zeilen += ["", wo]
    elif cfg.odds_mint:
        zeilen += ["", "Every call live for $ODDS holders."]
    if bilanz and bilanz.get("n"):
        zeilen += ["",
                   f"Since Sep 25: {bilanz['n']} calls · {bilanz['hits']} hits "
                   f"({bilanz['hits'] * 100 // bilanz['n']}%)"]
    if link:
        zeilen += ["", "oddsengine.live"]
    return _ohne_striche("\n".join(zeilen))


def send_x(text):
    """Entwurf pur in den privaten Chat, ohne OBSERVE-Vorspann."""
    if not (cfg.x_drafts and cfg.tg_token and cfg.tg_private):
        return False
    try:
        r = requests.post(API.format(token=cfg.tg_token, method="sendMessage"),
                          json={"chat_id": cfg.tg_private, "text": text,
                                "disable_web_page_preview": True}, timeout=20)
        return r.ok
    except Exception as e:
        log.debug(f"x draft: {e}")
        return False
