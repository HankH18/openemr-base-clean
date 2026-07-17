"""Rounding-session feature — sickest-first, one grounded card at a time.

The clinician-facing round loop lives here: a deterministic acuity ranking
(``ranking``) and the serve-time orchestration (``service``) that fetches,
scores, synthesizes, persists, and hands out :class:`PatientCard` objects.
The HTTP surface is ``copilot.api.routes.rounds``.
"""

from copilot.rounds.ranking import AcuityAssessment, assess_patient, rank
from copilot.rounds.service import NoActiveRoundError, RoundsService, RoundView

__all__ = [
    "AcuityAssessment",
    "NoActiveRoundError",
    "RoundView",
    "RoundsService",
    "assess_patient",
    "rank",
]
