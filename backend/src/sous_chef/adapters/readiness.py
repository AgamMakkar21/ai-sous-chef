from sous_chef.domain.readiness import DependencyUnavailable


class LocalReadinessProbe:
    async def check(self) -> None:
        return None


class UnconfiguredReadinessProbe:
    async def check(self) -> None:
        raise DependencyUnavailable("Live service adapters have not been implemented.")
