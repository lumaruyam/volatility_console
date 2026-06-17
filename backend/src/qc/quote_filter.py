"""
Quote normalization and quality control — orchestration layer.

Individual check functions live in checks.py.  This module provides:
  - QuoteQCOutcome   — aggregated per-row result
  - run_quote_qc()   — run all applicable checks on one OptionRow
  - filter_chain()   — apply QC to a full chain; return accepted rows + full audit trail
  - store_rejected_outcomes() — persist rejected outcomes for auditability

Acceptance criterion (PLAN Step 7):
  Same quote consistently accepted/rejected under fixed threshold version.
  Guaranteed by pure functions with no hidden state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from src.qc.checks import (
    QCCheckResult,
    check_bid_positive,
    check_crossed_market,
    check_intrinsic_value,
    check_open_interest,
    check_parity_residual,
    check_parity_residual_population,
    check_quote_age,
    check_spread_pct,
    robust_zscore,
)
from src.snapshots.models import OptionRow

logger = logging.getLogger(__name__)

# Re-export so existing imports from this module keep working.
__all__ = [
    "QCCheckResult",
    "QuoteQCOutcome",
    "check_bid_positive",
    "check_crossed_market",
    "check_intrinsic_value",
    "check_open_interest",
    "check_parity_residual",
    "check_parity_residual_population",
    "check_quote_age",
    "check_spread_pct",
    "filter_chain",
    "robust_zscore",
    "run_quote_qc",
    "store_rejected_outcomes",
]


# ---------------------------------------------------------------------------
# Aggregated result
# ---------------------------------------------------------------------------


@dataclass
class QuoteQCOutcome:
    """Aggregated QC result for one option quote."""
    instrument_key: str
    snapshot_ts: float
    overall_status: str     # "usable" | "caution" | "reject"
    checks: list[QCCheckResult]

    @property
    def is_usable(self) -> bool:
        return self.overall_status == "usable"

    @property
    def rejection_reasons(self) -> list[str]:
        return [c.reason_code for c in self.checks if c.status == "reject"]

    @property
    def caution_reasons(self) -> list[str]:
        return [c.reason_code for c in self.checks if c.status == "caution"]


# ---------------------------------------------------------------------------
# Per-row orchestration
# ---------------------------------------------------------------------------


def run_quote_qc(
    row: OptionRow,
    config: dict,
    intrinsic_value: Optional[float] = None,
    parity_zscore: Optional[float] = None,
) -> QuoteQCOutcome:
    """Run all applicable QC checks on one option quote.

    Args:
        row:            The OptionRow to check.
        config:         Threshold config dict (versioned; same dict → same result).
        intrinsic_value: If provided, also runs check_intrinsic_value.
        parity_zscore:  Pre-computed robust z-score of this row's parity residual.
                        If provided, also runs check_parity_residual.

    Returns:
        QuoteQCOutcome with per-check details and aggregated status.
        Rejected quotes are returned (never silently dropped) — caller must persist.
    """
    checks: list[QCCheckResult] = [
        check_spread_pct(row, config),
        check_bid_positive(row, config),
        check_quote_age(row, config),
        check_open_interest(row, config),
        check_crossed_market(row, config),
    ]
    if intrinsic_value is not None:
        checks.append(check_intrinsic_value(row, config, intrinsic_value))
    if parity_zscore is not None:
        checks.append(check_parity_residual(parity_zscore, config,
                                             context={"instrument_key": row.instrument_key}))

    if any(c.status == "reject" for c in checks):
        overall = "reject"
    elif any(c.status == "caution" for c in checks):
        overall = "caution"
    else:
        overall = "usable"

    return QuoteQCOutcome(
        instrument_key=row.instrument_key,
        snapshot_ts=row.snapshot_ts,
        overall_status=overall,
        checks=checks,
    )


# ---------------------------------------------------------------------------
# Chain-level orchestration
# ---------------------------------------------------------------------------


def filter_chain(
    rows: list[OptionRow],
    config: dict,
    intrinsics: Optional[dict[str, float]] = None,
    parity_residuals: Optional[dict[str, float]] = None,
) -> tuple[list[OptionRow], list[QuoteQCOutcome]]:
    """Apply QC to an entire option chain.

    Args:
        rows:              All option rows for one maturity or full chain.
        config:            Threshold config dict.
        intrinsics:        instrument_key → intrinsic_value for intrinsic check.
        parity_residuals:  instrument_key → raw parity residual.
                           Robust z-scores are computed here across the population.

    Returns:
        (accepted_rows, all_qc_outcomes):
          accepted_rows     — rows whose overall_status is "usable".
          all_qc_outcomes   — full audit trail including rejected rows.
    """
    # Pre-compute robust z-scores from the parity residual population
    parity_zscores: dict[str, float] = {}
    if parity_residuals:
        keys = list(parity_residuals.keys())
        vals = [parity_residuals[k] for k in keys]
        zs = robust_zscore(vals)
        parity_zscores = dict(zip(keys, zs))

    accepted: list[OptionRow] = []
    outcomes: list[QuoteQCOutcome] = []
    for row in rows:
        intrinsic = intrinsics.get(row.instrument_key) if intrinsics else None
        pz = parity_zscores.get(row.instrument_key)
        outcome = run_quote_qc(row, config, intrinsic_value=intrinsic, parity_zscore=pz)
        outcomes.append(outcome)
        if outcome.overall_status in ("usable", "caution"):
            accepted.append(row)
        else:
            logger.debug(
                "qc.rejected key=%s reasons=%s",
                row.instrument_key, outcome.rejection_reasons,
            )

    return accepted, outcomes


# ---------------------------------------------------------------------------
# Persistence (auditability)
# ---------------------------------------------------------------------------


def store_rejected_outcomes(
    outcomes: list[QuoteQCOutcome],
    root: Path | str,
    trade_date: str,
    snapshot_ts: float,
) -> Path:
    """Persist rejected and caution outcomes to a JSONL audit file.

    Only non-usable outcomes are stored.  File path:
      <root>/qc_rejected/dt=<trade_date>/snapshot_ts=<ts>/rejected.jsonl

    Returns the path of the written file (or the would-be path when no rejections).
    """
    non_usable = [o for o in outcomes if not o.is_usable]
    root_path = Path(root)
    out_dir = (
        root_path
        / "qc_rejected"
        / f"dt={trade_date}"
        / f"snapshot_ts={snapshot_ts:.0f}"
    )
    out_path = out_dir / "rejected.jsonl"

    if not non_usable:
        return out_path

    out_dir.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for o in non_usable:
            record = {
                "instrument_key": o.instrument_key,
                "snapshot_ts": o.snapshot_ts,
                "overall_status": o.overall_status,
                "rejection_reasons": o.rejection_reasons,
                "caution_reasons": o.caution_reasons,
                "checks": [
                    {
                        "check_name": c.check_name,
                        "status": c.status,
                        "reason_code": c.reason_code,
                        "measured_value": c.measured_value,
                        "threshold": c.threshold,
                        "context": c.context,
                    }
                    for c in o.checks
                ],
            }
            fh.write(json.dumps(record) + "\n")

    logger.info("qc.stored_rejected count=%d path=%s", len(non_usable), out_path)
    return out_path
