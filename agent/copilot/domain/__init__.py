"""Domain primitives + Pydantic v2 contracts.

Every value that crosses a service boundary is a typed object here.  See
`ARCHITECTURE.md` §"Interfaces & contracts" — these are the source of truth.
"""

from copilot.domain.primitives import (
    ClinicianId,
    CorrelationId,
    FhirReference,
    PatientId,
    ResourceType,
    utcnow,
)
from copilot.domain.contracts import (
    Claim,
    LabResult,
    MedListItem,
    MedicationList,
    MemoryFileSummary,
    PatientCard,
    VerificationClaimResult,
    VerificationDomainFlag,
    VerificationResult,
)

__all__ = [
    "ClinicianId",
    "CorrelationId",
    "FhirReference",
    "PatientId",
    "ResourceType",
    "utcnow",
    "Claim",
    "LabResult",
    "MedListItem",
    "MedicationList",
    "MemoryFileSummary",
    "PatientCard",
    "VerificationClaimResult",
    "VerificationDomainFlag",
    "VerificationResult",
]
