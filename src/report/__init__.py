"""Everything that turns pipeline data into report text.

Four modules, split by what they answer to:

    figures.py         normalising a candidate row into displayable figures
    price.py           the live quote and the '## Price' block
    sections.py        the reader-facing sections and the agent-facing figure block
    reconciliation.py  the gate that checks agent prose against those figures

Nothing here imports `main`. That is the boundary that makes the package worth
having: report text is built from a candidate dict and a price dict and nothing
else, so it can be exercised — as `tools/snapshot_report_surface.py` does — without
standing up a pipeline run.

Re-exported flat, under the names the rest of the codebase already calls, so
`main._format_price_section` keeps resolving. See any submodule for why the
underscores stayed.
"""
from .figures import (
    _RATIO_TO_PCT_FIELD,
    _eps_window_phrase,
    _growth_window,
    _no_peg_reason,
    _normalize_candidate,
    _present,
    _verified_figures,
)
from .price import _format_price_section, _price_snapshot
from .provenance import _with_run_header
from .reconciliation import (
    _BACKWARD_GAP_MAX,
    _COMPARISON_ROW_MIN_AMOUNTS,
    _CONCEPT_LOOKBEHIND,
    _CONNECTOR_GAP_MAX,
    _DELTA_LOOKBEHIND,
    _FIELD_TOLERANCE,
    _FORWARD_SCAN,
    _MARKET_DERIVED_FIELDS,
    _MULTIPLIER,
    _PERIOD_PROXIMITY,
    _RECONCILED_FIELDS,
    _RECONCILIATION_HEADING,
    _THRESHOLD_PROXIMITY,
    _amount_for_label,
    _finding_basis,
    _format_reconciliation_section,
    _line_bounds,
    _reconcile_agent_figures,
    RECONCILE_TOLERANCE,
    reconciliation_warning,
)
from .sections import (
    _format_magic_formula_section,
    _format_screen_context,
    _format_verified_figures,
)

__all__ = [
    "RECONCILE_TOLERANCE",
    "_BACKWARD_GAP_MAX",
    "_COMPARISON_ROW_MIN_AMOUNTS",
    "_CONCEPT_LOOKBEHIND",
    "_CONNECTOR_GAP_MAX",
    "_DELTA_LOOKBEHIND",
    "_FIELD_TOLERANCE",
    "_FORWARD_SCAN",
    "_MARKET_DERIVED_FIELDS",
    "_MULTIPLIER",
    "_PERIOD_PROXIMITY",
    "_RATIO_TO_PCT_FIELD",
    "_RECONCILED_FIELDS",
    "_RECONCILIATION_HEADING",
    "_THRESHOLD_PROXIMITY",
    "_amount_for_label",
    "_eps_window_phrase",
    "_finding_basis",
    "_format_magic_formula_section",
    "_format_price_section",
    "_format_reconciliation_section",
    "_format_screen_context",
    "_format_verified_figures",
    "_growth_window",
    "_line_bounds",
    "_no_peg_reason",
    "_normalize_candidate",
    "_present",
    "_price_snapshot",
    "_reconcile_agent_figures",
    "_verified_figures",
    "_with_run_header",
    "reconciliation_warning",
]
