"""Compatibility aliases for the active round-robin implementation.

Per-date weekday/weekend and no-duty exceptions are implemented in
``date_exceptions``. Session-wide picking pause support is layered on after
those date-aware rules so older imports continue to use the active behavior.
"""

import core
from date_exceptions import next_picker, selectable_dates

core.selectable_dates = selectable_dates
core.next_picker = next_picker

# This additive module installs the session-wide pause migration and replaces
# legacy participant-deferral turn helpers with the active session-level model.
import session_pause  # noqa: E402,F401

next_picker = core.next_picker
