"""Independent student learning portal (built beside the existing system).

Nothing in this package is imported by ``app.py``, ``src/`` or the daily
check. The dependency edge points one way only:

    portal/  ->  src/  (read-only)

Two modules cross that edge, each for one purpose and both read-only:

* :mod:`portal.grading.adapter` - the existing grading engine;
* :mod:`portal.ingestion.classroom_source` - Classroom/Drive/Docs reads
  (plus :mod:`portal.ingestion.google_auth`, which loads the existing OAuth
  client configuration without starting a login flow in a web request).
"""

__version__ = "0.7.0"
