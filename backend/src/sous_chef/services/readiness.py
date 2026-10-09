import asyncio
import logging

from sous_chef.domain.readiness import DependencyUnavailable, ReadinessProbe

logger = logging.getLogger(__name__)


class ReadinessService:
    def __init__(self, probe: ReadinessProbe, timeout_seconds: float) -> None:
        self._probe = probe
        self._timeout_seconds = timeout_seconds

    async def is_ready(self) -> bool:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._probe.check()
        except DependencyUnavailable:
            logger.warning("readiness_dependency_unavailable")
            return False
        except TimeoutError:
            logger.warning("readiness_deadline_exceeded")
            return False
        return True
