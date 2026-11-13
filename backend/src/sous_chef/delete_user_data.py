"""Delete one validated owner's primary data using explicit Azure CLI credentials."""

import argparse
import asyncio
import json
import re
from collections.abc import Sequence
from typing import NoReturn
from uuid import UUID

from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.cosmos import ProductContainer
from sous_chef.settings import Settings
from sous_chef.telemetry import configure_safe_telemetry


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if (
        not args.delete
        or not re.fullmatch(r"[0-9a-f]{64}", args.owner)
        or args.confirm_owner != args.owner
        or not re.fullmatch(
            r"https://[a-z0-9-]+\.documents\.azure\.com(?::443)?/?", args.cosmos_endpoint
        )
    ):
        raise ValueError
    request_id = UUID(args.request_id)
    if request_id.version != 4:
        raise ValueError
    settings = Settings(
        environment="local",
        adapter_mode="live",
        agents_enabled=False,
        chat_retention_days=None,
        cosmos_endpoint=args.cosmos_endpoint,
        cosmos_database="sous-chef",
    )
    removed = {container.value: 0 for container in ProductContainer}
    while True:
        async with open_cosmos(settings, lifetime_seconds=60) as repository:
            result = await repository.purge_user(AuthenticatedUser(args.owner), request_id)
        for container, count in result.removed.items():
            removed[container] += count
        if result.complete:
            return {
                "event": "user_data_deletion",
                "status": "primary_stores_completed",
                "requestId": str(result.request_id),
                "removedThisRun": removed,
            }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--delete", action="store_true", required=True)
    parser.add_argument("--cosmos-endpoint", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--confirm-owner", required=True)
    parser.add_argument("--request-id", required=True)
    try:
        args = parser.parse_args(argv)
        configure_safe_telemetry()
        result = asyncio.run(_run(args))
    except Exception:
        print(
            '{"event":"user_data_deletion","status":"incomplete",'
            '"message":"Verify arguments, approved Cosmos RBAC and network access; '
            'rerun the same request to resume. External erasure is not implied."}'
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
