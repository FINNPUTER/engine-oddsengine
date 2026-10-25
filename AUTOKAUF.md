# Autokauf als dritter Railway-Dienst

Damit läuft der Käufer rund um die Uhr, auch wenn der Mac aus ist.

## Einrichten

**1.** Diese fünf Dateien ins Repo `oddsengine-platform` laden:

```
autokauf.py  place.py  einzelkauf.py  redeem.py  schluessel.py
requirements.txt   (ersetzt die vorhandene)
```

**2.** In Railway, im Projekt `dependable-respect`:
**New Service → GitHub Repo → oddsengine-platform**

Es entsteht ein dritter Dienst neben `web` und `worker`.

**3.** Beim neuen Dienst unter **Settings → Start Command**:

```
python autokauf.py --stake 8 --tageslimit 150 --dauer 20
```

Ohne diesen Befehl würde er den Web-Dienst starten — dann laufen zwei
APIs und der Käufer gar nicht.

**4.** Unter **Variables** beim neuen Dienst:

| Name | Wert |
|---|---|
| `POLY_PRIVATE_KEY` | dein privater Schlüssel |
| `POLY_FUNDER_ADDRESS` | `0xe8f31cabd6f045f7e1fc3c3781f925a9f1a75659` |
| `POLY_SIGNATURE_TYPE` | `0` |
| `ODDSENGINE_KEY` | nur nötig, wenn `SIGNAL_KEYS` gesetzt ist |

**Nicht** bei `web` oder `worker` eintragen — die brauchen den Schlüssel
nicht und sollen ihn auch nicht haben.

## Was er tut

Alle 20 Minuten: einlösen was bereitliegt, jedes neue Signal kaufen,
fehlende Beine nachziehen. Stoppt bei Guthaben unter 6 Dollar oder
erreichtem Tageslimit. Um Mitternacht beginnt der Tag neu.

## Anhalten

Railway → der Dienst → **Settings → Remove Service**, oder das
Deployment pausieren. Schneller: Tageslimit auf 0 setzen und neu
starten, dann läuft er weiter, kauft aber nichts.

## Zur Sicherheit

Der private Schlüssel liegt als Variable auf Railway. Er kann Orders
setzen und einlösen — abheben geht nur an die eigene Adresse. Wer
Zugriff aufs Railway-Konto hat, hat aber Zugriff auf den Schlüssel.

Deshalb: Zwei-Faktor bei Railway einschalten, und auf der Handelswallet
nur so viel halten, wie der Käufer braucht.
