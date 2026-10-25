"""
stimme.py: Your Engine spricht. Seit 2.39.

  bereit()            ist ein Sprachdienst eingerichtet?
  sprechen(text)      -> (mp3_bytes, zeichen) oder (None, fehler)
  ogg(mp3)            -> OGG/Opus fuer Telegram-Sprachnachrichten (ffmpeg aus imageio-ffmpeg)
  vorlesbar(text)     Text fuer die Stimme aufbereiten: keine Links, keine langen Adressen
  kosten(zeichen)     was es den Nutzer kostet

Anbieter, in dieser Reihenfolge:
  ELEVENLABS_API_KEY  (Stimme ELEVENLABS_VOICE_ID, Modell ELEVENLABS_MODEL, Standard eleven_flash_v2_5)
  OPENAI_API_KEY      (Modell OPENAI_TTS_MODEL, Standard gpt-4o-mini-tts, Stimme OPENAI_TTS_VOICE, Standard onyx)
Preis fuer den Nutzer: VOICE_USD_PER_1K Zeichen (Standard 0.12), vom Guthaben wie jede Antwort.
"""
import os
import re
import subprocess
import tempfile

import requests

from logger import log


def _f(k, d):
    try:
        return float(os.getenv(k, "") or d)
    except Exception:
        return float(d)


def anbieter():
    if os.getenv("ELEVENLABS_API_KEY", "").strip():
        return "elevenlabs"
    if os.getenv("OPENAI_API_KEY", "").strip():
        return "openai"
    return None


def bereit():
    return anbieter() is not None


def kosten(zeichen):
    return round(max(1, int(zeichen)) / 1000 * _f("VOICE_USD_PER_1K", 0.12), 5)


def vorlesbar(text, n=1400):
    t = str(text or "")
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\b0x[a-fA-F0-9]{40,64}\b", "the address", t)
    t = re.sub(r"\b[1-9A-HJ-NP-Za-km-z]{32,90}\b", "the address", t)
    t = re.sub(r"[▰▱•*_`#>|]+", " ", t)
    t = re.sub(r"[🟢🟠🔴⚠️✅❌]", "", t)
    t = re.sub(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b", r"\1", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    if len(t) > n:
        cut = max(t.rfind(". ", 0, n), t.rfind("\n", 0, n))
        t = t[:cut + 1 if cut > n // 2 else n].strip()
    return t


def sprechen(text):
    """Text -> MP3. (bytes, zeichen) oder (None, fehlertext)."""
    t = vorlesbar(text)
    if not t:
        return None, "nothing to say"
    a = anbieter()
    try:
        if a == "elevenlabs":
            vid = os.getenv("ELEVENLABS_VOICE_ID", "").strip() or "JBFqnCBsd6RMkjVDRZzb"
            r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
                              params={"output_format": "mp3_44100_128"},
                              headers={"xi-api-key": os.getenv("ELEVENLABS_API_KEY").strip(), "Content-Type": "application/json",
                                       "Accept": "audio/mpeg"},
                              json={"text": t, "model_id": os.getenv("ELEVENLABS_MODEL", "").strip() or "eleven_flash_v2_5",
                                    "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.15}},
                              timeout=60)
        elif a == "openai":
            r = requests.post("https://api.openai.com/v1/audio/speech",
                              headers={"Authorization": "Bearer " + os.getenv("OPENAI_API_KEY").strip()},
                              json={"model": os.getenv("OPENAI_TTS_MODEL", "").strip() or "gpt-4o-mini-tts",
                                    "voice": os.getenv("OPENAI_TTS_VOICE", "").strip() or "onyx", "input": t,
                                    "response_format": "mp3",
                                    "instructions": "Calm, confident, friendly. Like a sharp personal assistant."},
                              timeout=60)
        else:
            return None, "voice is not set up"
    except Exception as e:
        log.warning(f"stimme: {e}")
        return None, "the voice service did not answer"
    if r.status_code != 200 or not r.content or r.headers.get("content-type", "").startswith("application/json"):
        log.warning(f"stimme {a}: {r.status_code} {r.text[:200]}")
        return None, "the voice service refused"
    return r.content, len(t)


def ogg(mp3):
    """MP3 -> OGG/Opus (Telegram zeigt das als Sprachnachricht). None, wenn kein ffmpeg da ist."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = "ffmpeg"
    try:
        with tempfile.TemporaryDirectory() as d:
            ein, aus = os.path.join(d, "a.mp3"), os.path.join(d, "a.ogg")
            open(ein, "wb").write(mp3)
            subprocess.run([exe, "-y", "-loglevel", "error", "-i", ein, "-ac", "1", "-c:a", "libopus", "-b:a", "48k", aus],
                           check=True, timeout=60)
            return open(aus, "rb").read()
    except Exception as e:
        log.warning(f"stimme ogg: {e}")
        return None
