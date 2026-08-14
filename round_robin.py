"""Compatibility aliases for the active round-robin implementation.

Per-date weekday/weekend and no-duty exceptions are implemented in
``date_exceptions``. Importing this module keeps older imports working without
replacing those date-aware rules.
"""

import core
from date_exceptions import next_picker, selectable_dates

core.selectable_dates = selectable_dates
core.next_picker = next_picker
