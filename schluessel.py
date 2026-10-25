#!/usr/bin/env python3
"""schluessel.py — findet den privaten Schluessel, egal wo er liegt.

Wird von den Werkzeugen automatisch beim Start aufgerufen. Sucht in dieser
Reihenfolge:

    1. bereits gesetzte Umgebungsvariablen
    2. ~/Documents/OddsEngine/schluessel.sh   (im Werkzeugordner)
    3. ~/Documents/schluessel.sh              (eine Ebene hoeher, ueberlebt
                                               jedes Ersetzen des Ordners)
    4. ~/schluessel.sh

Empfehlung: Punkt 3. Dort bleibt die Datei liegen, auch wenn der
Werkzeugordner komplett neu aufgesetzt wird.
"""
import os
import re


def laden():
    if os.getenv("POLY_PRIVATE_KEY", "").strip():
        return True
    hier = os.path.dirname(os.path.abspath(__file__))
    orte = [os.path.join(hier, "schluessel.sh"),
            os.path.join(os.path.dirname(hier), "schluessel.sh"),
            os.path.expanduser("~/Documents/schluessel.sh"),
            os.path.expanduser("~/schluessel.sh")]
    for pfad in orte:
        if not os.path.exists(pfad):
            continue
        try:
            for zeile in open(pfad, encoding="utf-8"):
                m = re.match(r"""\s*export\s+(\w+)\s*=\s*["']?([^"'\s]+)""", zeile)
                if m and not os.getenv(m.group(1), "").strip():
                    os.environ[m.group(1)] = m.group(2)
        except Exception:
            continue
        if os.getenv("POLY_PRIVATE_KEY", "").strip():
            return True
    return False


def pflicht():
    """Wie laden(), bricht aber mit einer verstaendlichen Meldung ab."""
    import sys
    if laden():
        return
    sys.exit(
        "\n  Kein Schluessel gefunden.\n\n"
        "  Einmal anlegen -- am besten eine Ebene ueber dem Werkzeugordner,\n"
        "  dort ueberlebt die Datei jedes Update:\n\n"
        "      cat > ~/Documents/schluessel.sh <<'ENDE'\n"
        "      export POLY_PRIVATE_KEY=0xDEIN_SCHLUESSEL\n"
        "      export POLY_FUNDER_ADDRESS=0xe8f31cabd6f045f7e1fc3c3781f925a9f1a75659\n"
        "      ENDE\n"
        "      chmod 600 ~/Documents/schluessel.sh\n")


laden()
