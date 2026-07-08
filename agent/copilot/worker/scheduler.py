"""APScheduler wrapper — thin.

Not the trust-bearing part of the system; the scheduler just fires the
`Poller.tick` on an interval for every patient the ``active_patients``
callable returns.  Verification + persistence run around the tick via
the `on_result` callback so the poller itself stays pure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from copilot.domain.primitives import PatientId
from copilot.worker.poller import Poller, PollerResult


class PollerScheduler:
    """Schedule + run `Poller.tick(patient_id)` for a set of patients."""

    def __init__(
        self,
        *,
        poller: Poller,
        active_patients: Callable[[], Awaitable[Iterable[PatientId]]],
        on_result: Callable[[PollerResult], Awaitable[None]],
        interval_seconds: int,
    ) -> None:
        self._poller = poller
        self._active_patients = active_patients
        self._on_result = on_result
        self._interval = interval_seconds
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        self._scheduler.add_job(
            self.tick_once, "interval", seconds=self._interval, id="poller_tick"
        )
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    async def tick_once(self) -> list[PollerResult]:
        """Run one tick for every active patient sequentially.

        Sequential (not gather) so a single slow patient doesn't blur
        which one caused the delay — matches the observability posture in
        ARCHITECTURE.
        """
        patients = list(await self._active_patients())
        results: list[PollerResult] = []
        for pid in patients:
            result = await self._poller.tick(pid)
            results.append(result)
            await self._on_result(result)
        return results
