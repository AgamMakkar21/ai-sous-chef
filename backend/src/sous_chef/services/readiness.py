import asyncio

from sous_chef.domain.readiness import DependencyUnavailable, ReadinessProbe
from sous_chef.telemetry import ErrorCategory, Event, operational_event


class ReadinessService:
    def __init__(self, probe: ReadinessProbe, timeout_seconds: float) -> None:
        self._probe = probe
        self._timeout_seconds = timeout_seconds

    async def is_ready(self) -> bool:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._probe.check()
        except DependencyUnavailable:
            operational_event(
                Event.READINESS_UNAVAILABLE, category=ErrorCategory.DEPENDENCY_UNAVAILABLE
            )
            return False
        except TimeoutError:
            operational_event(
                Event.READINESS_TIMEOUT, category=ErrorCategory.DEPENDENCY_UNAVAILABLE
            )
            return False
        return True
