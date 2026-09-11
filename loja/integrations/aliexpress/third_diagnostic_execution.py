"""FASE 8.0.11S — final execution boundary for the third AliExpress canary.

This module prepares the admin POST execution surface without authorizing or
performing provider HTTP by default. It adds a final, independent hard-off gate
that is intentionally separate from every pre-existing canary/placeorder gate.

Safety contract:
- ``ALIEXPRESS_THIRD_DIAGNOSTIC_EXECUTION_ENABLED`` is OFF unless exactly ``1``;
- the final gate is checked before delegating to the third-canary boundary;
- this layer never mutates environment variables and never auto-selects orders;
- no retry, credential rotation, provider probing or HTTP is introduced here;
- enabling all older gates is still insufficient while this final gate is OFF.

A future real third call therefore requires a separate explicit authorization to
arm this final execution gate in addition to every existing fail-closed gate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from loja import db

from .submission import SubmissionHardOffError
from .third_diagnostic_canary import submit_third_diagnostic_canary_order


THIRD_DIAGNOSTIC_EXECUTION_ENABLED_ENV = (
    "ALIEXPRESS_THIRD_DIAGNOSTIC_EXECUTION_ENABLED"
)


class AliExpressThirdDiagnosticExecutionHardOff(SubmissionHardOffError):
    """The independent final execution authorization is not armed."""


@dataclass(frozen=True)
class ThirdDiagnosticExecutionStatus:
    enabled: bool
    reason: str


def _execution_enabled() -> bool:
    return os.environ.get(THIRD_DIAGNOSTIC_EXECUTION_ENABLED_ENV) == "1"


def get_third_diagnostic_execution_status() -> ThirdDiagnosticExecutionStatus:
    """Return sanitized state of the independent final execution layer."""

    enabled = _execution_enabled()
    return ThirdDiagnosticExecutionStatus(
        enabled=enabled,
        reason=(
            "third_diagnostic_execution_authorized"
            if enabled
            else "third_diagnostic_execution_hard_off"
        ),
    )


def execute_third_diagnostic_canary_order(ali_order, *, session=None):
    """Delegate only after the independent final authorization is armed.

    The production/admin surface intentionally exposes no submitter injection.
    Tests should monkeypatch the downstream boundary rather than providing a
    provider implementation through this layer.
    """

    if not _execution_enabled():
        raise AliExpressThirdDiagnosticExecutionHardOff(
            "third_diagnostic_execution_hard_off"
        )

    session = session or db.session
    return submit_third_diagnostic_canary_order(ali_order, session=session)


__all__ = [
    "THIRD_DIAGNOSTIC_EXECUTION_ENABLED_ENV",
    "AliExpressThirdDiagnosticExecutionHardOff",
    "ThirdDiagnosticExecutionStatus",
    "get_third_diagnostic_execution_status",
    "execute_third_diagnostic_canary_order",
]
