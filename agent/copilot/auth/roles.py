"""Role-based access for leading a round (the physician/resident/nurse model).

A request carries the clinician's role in the ``X-Clinician-Role`` header.
*Leading* a round (``POST /v1/rounds/start``) is a rounding activity, so it is
restricted to clinicians who round — ``physician``, ``resident``, or the
senior ``attending``. A ``nurse`` is a view/assist role and may not lead;
an unrecognized role is refused outright.

Backward-compatibility: an *absent* (or empty) header defaults to
``physician`` so the pre-existing, role-unaware flows keep working unchanged.

The values are exchanged with an external system (the HTTP header), so
``ClinicalRole`` is a backed :class:`~enum.StrEnum`. The HTTP surface enforces
the gate in ``copilot.api.routes.rounds``.
"""

from __future__ import annotations

from enum import StrEnum

#: The request header that carries the clinician's role.
ROLE_HEADER = "X-Clinician-Role"


class ClinicalRole(StrEnum):
    """A clinician's role. Closed set — extend deliberately.

    The backing string is exactly the wire value accepted in ``X-Clinician-Role``.
    """

    physician = "physician"
    resident = "resident"
    attending = "attending"
    nurse = "nurse"


class UnknownClinicalRoleError(ValueError):
    """A supplied role string matched no known :class:`ClinicalRole`."""

    def __init__(self, raw: str) -> None:
        super().__init__(f"unrecognized clinical role: {raw!r}")
        self.raw = raw


def parse_role(raw: str | None) -> ClinicalRole:
    """Parse a raw ``X-Clinician-Role`` value into a :class:`ClinicalRole`.

    An absent header (``None``) — or an empty/whitespace-only value, which is
    effectively absent — defaults to :attr:`ClinicalRole.physician`, keeping
    role-unaware callers backward-compatible. A present-but-unrecognized value
    raises :class:`UnknownClinicalRoleError` so the boundary can refuse it.
    """
    if raw is None:
        return ClinicalRole.physician

    normalized = raw.strip().lower()
    if not normalized:
        return ClinicalRole.physician

    try:
        return ClinicalRole(normalized)
    except ValueError:
        raise UnknownClinicalRoleError(raw) from None


def may_lead_round(role: ClinicalRole) -> bool:
    """True iff ``role`` is permitted to lead (start) a round.

    Rounding clinicians — physicians, residents, and attendings — may lead;
    a nurse may not.
    """
    match role:
        case ClinicalRole.physician | ClinicalRole.resident | ClinicalRole.attending:
            return True
        case ClinicalRole.nurse:
            return False
