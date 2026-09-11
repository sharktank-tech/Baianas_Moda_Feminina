"""FASE 8.0.11V — independent final execution hard-off for fourth canary."""

from __future__ import annotations

import os
from dataclasses import dataclass

from loja import db

from .fourth_diagnostic_canary import submit_fourth_diagnostic_canary_order
from .submission import SubmissionHardOffError


FOURTH_DIAGNOSTIC_EXECUTION_ENABLED_ENV = (
    "ALIEXPRESS_FOURTH_DIAGNOSTIC_EXECUTION_ENABLED"
)


class AliExpressFourthDiagnosticExecutionHardOff(SubmissionHardOffError):
    """Independent final authorization for fourth execution is OFF."""


@dataclass(frozen=True)
class FourthDiagnosticExecutionStatus:
    enabled: bool
    reason: str


def _execution_enabled() -> bool:
    return os.environ.get(FOURTH_DIAGNOSTIC_EXECUTION_ENABLED_ENV) == "1"


def get_fourth_diagnostic_execution_status() -> FourthDiagnosticExecutionStatus:
    enabled = _execution_enabled()
    return FourthDiagnosticExecutionStatus(
        enabled=enabled,
        reason=(
            "fourth_diagnostic_execution_authorized"
            if enabled
            else "fourth_diagnostic_execution_hard_off"
        ),
    )


def execute_fourth_diagnostic_canary_order(ali_order, *, session=None):
    if not _execution_enabled():
        raise AliExpressFourthDiagnosticExecutionHardOff(
            "fourth_diagnostic_execution_hard_off"
        )
    session = session or db.session
    return submit_fourth_diagnostic_canary_order(ali_order, session=session)


__all__ = [
    "FOURTH_DIAGNOSTIC_EXECUTION_ENABLED_ENV",
    "AliExpressFourthDiagnosticExecutionHardOff",
    "FourthDiagnosticExecutionStatus",
    "get_fourth_diagnostic_execution_status",
    "execute_fourth_diagnostic_canary_order",
]
