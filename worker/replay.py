"""Operator-only recovery through explicit Azure CLI credentials and Cosmos RBAC."""

import argparse
import asyncio
import json
import re
from collections.abc import Sequence
from uuid import UUID

from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.cosmos import ProductContainer
from sous_chef.services.learning_recovery import LearningRecovery
from sous_chef.settings import Settings

from worker.extraction import private_sdk_diagnostics


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{64}", args.owner):
        raise ValueError
    feedback_id = UUID(args.feedback_id)
    revision = int(args.revision)
    if feedback_id.version != 4 or revision < 1:
        raise ValueError
    request_id = UUID(args.request_id) if args.request_id else None
    if args.replay and (request_id is None or request_id.version != 4):
        raise ValueError
    if not re.fullmatch(
        r"https://[a-z0-9-]+\.documents\.azure\.com(?::443)?/?", args.cosmos_endpoint
    ):
        raise ValueError
    settings = Settings(
        environment="local",
        adapter_mode="live",
        agents_enabled=False,
        cosmos_endpoint=args.cosmos_endpoint,
        cosmos_database="sous-chef",
    )
    user = AuthenticatedUser(args.owner)
    async with open_cosmos(
        settings, containers=(ProductContainer.RECIPE_HISTORY,), lifetime_seconds=30
    ) as repository:
        recovery = LearningRecovery(repository)
        if args.replay:
            assert request_id is not None
            status = await recovery.replay(user, feedback_id, revision, request_id)
            return {"status": status}
        found = await recovery.inspect(user, feedback_id, revision)
        if found is None:
            return {"status": "not_found"}
        _, attempt = found
        return {
            "status": attempt.state,
            "generation": attempt.generation,
            "attempts": attempt.attempts,
            "maxAttempts": attempt.max_attempts,
            "availableAt": attempt.available_at.isoformat(),
            "errorCategory": attempt.error_category,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--replay", action="store_true")
    parser.add_argument("--cosmos-endpoint", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--feedback-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--request-id")
    args = parser.parse_args(argv)
    with private_sdk_diagnostics():
        try:
            result = asyncio.run(_run(args))
        except Exception:
            print(
                '{"status":"rejected","message":"Check arguments, Cosmos RBAC and recovery state."}'
            )
            return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
