"""ROTD — Runner of the Day.

Surfaces the few sub-$10 US stocks most likely to be the day's explosive runner
(+100% or more), as early as the evidence allows — and stays silent otherwise.

Decision support only: no orders, no shorts, no promises. Package layout:

- ``config``      — the settings registry (every threshold is a named setting).
- ``util``        — time, calendar, math, logging helpers (UTC internally).
- ``sources``     — owned data adapters, built against saved fixtures.
- ``calendar_us`` — the session/mode engine.
- ``store``       — SQLite persistence.
- ``normalize``   — canonical records, reconciliation, snapshots (DATA-01..12).
"""

__version__ = "0.1.0"
