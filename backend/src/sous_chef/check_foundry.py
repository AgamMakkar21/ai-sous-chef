"""Run one tiny call against each configured Foundry deployment.

Usage: ASC_ENVIRONMENT=dev python -m sous_chef.check_foundry (after `az login`).
"""

from sous_chef.adapters.foundry import FoundryInvocationError, FoundryModelClient
from sous_chef.settings import ConfigurationError, load_settings


def main() -> int:
    try:
        client = FoundryModelClient(load_settings())
        results = client.check_all()
    except (ConfigurationError, FoundryInvocationError) as exc:
        print(f"FAILED: {exc}")
        return 1
    for role, text in results.items():
        print(f"OK {role.value} ({client.deployments.for_role(role)}): {text.strip()[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
