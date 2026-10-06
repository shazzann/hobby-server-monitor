"""Independent metrics collector.

Modules:
- ``source``         LXD reads (one bulk instance listing per cycle, slow host metadata)
- ``normalize``      per-container sample normalization and rate baselines
- ``inventory``      reconcile the LXD listing into the ``containers`` table
- ``cycle``          one collection cycle: read -> reconcile -> normalize -> SQLite -> TinyFlux
- ``store``          the single TinyFlux owner (thread, rollups, retention, queries)
- ``history_server`` private Unix socket answering bounded history/usage requests

Entry point: ``python -m hsm.collector``.
"""
