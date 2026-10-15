"""Explicit, opt-in managed-agent smoke check; never an automatic readiness probe."""

import asyncio
import os

from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.foundry import FoundryGatewayError, FoundryRequest
from sous_chef.settings import ConfigurationError, load_settings
from sous_chef.telemetry import configure_safe_telemetry


async def check() -> int:
    try:
        settings = load_settings()
        if settings.environment != "dev" or os.environ.get("ASC_APPROVE_FOUNDRY_SMOKE") != "1":
            raise ConfigurationError(
                "Run only from the approved dev workload with ASC_APPROVE_FOUNDRY_SMOKE=1."
            )
        gateway = FoundryGateway(settings)
        for role in AgentRole:
            result = await gateway.invoke(
                FoundryRequest(role=role, prompt="Reply with the single word: ready.")
            )
            print(f"OK {role.value}; operation_id={result.operation_id}")
    except (ConfigurationError, FoundryGatewayError) as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0


def main() -> int:
    configure_safe_telemetry()
    return asyncio.run(check())


if __name__ == "__main__":
    raise SystemExit(main())
