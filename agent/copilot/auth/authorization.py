"""The serve-time authorization check for chat (UC-6).

Small and testable: given a clinician and a patient, decide whether the
clinician is allowed to converse about that patient.  The rule is the
rounding-cursor membership described in ``ARCHITECTURE`` — a clinician's
authorized set is the ``ordered_patient_ids`` they established via
``POST /v1/rounds/start``.

Fail-closed: no cursor (the clinician never opened a round) means *no*
authorized patients, so the request is refused.
"""

from __future__ import annotations

from copilot.domain.primitives import ClinicianId, PatientId
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository


async def is_authorized(clinician_id: ClinicianId, patient_id: PatientId) -> bool:
    """True iff ``clinician_id`` may converse about ``patient_id``.

    Authorized ⇔ the clinician has a persisted rounding cursor whose
    ``ordered_patient_ids`` contains ``patient_id``.  A clinician with no
    cursor has an empty authorized set and is therefore never authorized.
    """
    async with session_scope() as session:
        cursor = await MemoryRepository(session).get_rounding_cursor(clinician_id)

    if cursor is None:
        return False
    return patient_id.value in cursor.ordered_patient_ids
