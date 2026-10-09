from typing import Protocol


class DependencyUnavailable(RuntimeError):
    """A dependency cannot currently serve requests."""


class ReadinessProbe(Protocol):
    async def check(self) -> None:
        """Raise DependencyUnavailable when a required dependency is unavailable."""
