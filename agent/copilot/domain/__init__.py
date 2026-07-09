"""Domain primitives + Pydantic v2 contracts.

Every value that crosses a service boundary is a typed object here.  See
`ARCHITECTURE.md` §"Interfaces & contracts" — these are the source of truth.
"""

from copilot.domain.contracts import (
    Claim,
    LabResult,
    MedicationList,
    MedListItem,
    MemoryFileSummary,
    PatientCard,
    VerificationClaimResult,
    VerificationDomainFlag,
    VerificationResult,
)
from copilot.domain.primitives import (
    ClinicianId,
    CorrelationId,
    FhirReference,
    PatientId,
    ResourceType,
    utcnow,
)

__all__ = [
    "Claim",
    "ClinicianId",
    "CorrelationId",
    "FhirReference",
    "LabResult",
    "MedListItem",
    "MedicationList",
    "MemoryFileSummary",
    "PatientCard",
    "PatientId",
    "ResourceType",
    "VerificationClaimResult",
    "VerificationDomainFlag",
    "VerificationResult",
    "utcnow",
]
