"""Console logging. Railway captures stdout automatically."""
import logging
import os
import sys

log = logging.getLogger("oddsengine")
if not log.handlers:
    log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                     "%H:%M:%S"))
    log.addHandler(h)
