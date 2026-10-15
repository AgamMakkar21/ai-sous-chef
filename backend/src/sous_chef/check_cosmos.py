"""Approved dev-only repository acceptance checks with exact synthetic cleanup."""

import asyncio
import os
from uuid import UUID, uuid4

from pydantic import JsonValue

from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    HistoryQuery,
    NewRecord,
    PageRequest,
    ProductContainer,
    RecordKind,
    RecordPatch,
)
from sous_chef.settings import ConfigurationError, Settings, load_settings
from sous_chef.telemetry import configure_safe_telemetry


class CosmosSmokeError(RuntimeError):
    """Only fixed diagnostic text and the synthetic run ID are safe to print."""


def _approve_target(settings: Settings) -> None:
    endpoint = settings.cosmos_endpoint
    if (
        os.environ.get("ASC_APPROVE_COSMOS_SMOKE") != "1"
        or settings.environment != "dev"
        or settings.adapter_mode != "live"
        or settings.cosmos_database != "sous-chef"
        or endpoint is None
        or not (endpoint.host or "").startswith("ai-sous-chef-dev-")
        or not (endpoint.host or "").endswith(".documents.azure.com")
        or endpoint.path not in (None, "/")
        or endpoint.port != 443
    ):
        raise ConfigurationError(
            "Use the approved dev workload, dev Cosmos account and sous-chef database "
            "with ASC_APPROVE_COSMOS_SMOKE=1."
        )


def _plan(run_id: UUID) -> list[tuple[AuthenticatedUser, ProductContainer, NewRecord]]:
    alice, bob = (
        AuthenticatedUser.from_verified_identity(
            "urn:ai-sous-chef:cosmos-smoke", f"{run_id}:{name}"
        )
        for name in ("a", "b")
    )
    ids = [UUID(int=run_id.int ^ index, version=4) for index in range(1, 7)]
    recipes = [
        (
            user,
            ProductContainer.RECIPE_HISTORY,
            NewRecord(
                kind=RecordKind.RECIPE,
                record_id=item_id,
                fields={"title": "Synthetic repository acceptance sentinel"},
            ),
        )
        for user, item_id in [(alice, item) for item in ids[:3]] + [(bob, ids[0])]
    ]
    return [
        (
            alice,
            ProductContainer.USERS,
            NewRecord(
                kind=RecordKind.PROFILE,
                fields={
                    "groceries": [],
                    "explicitPreferences": {"allergies": ["synthetic"]},
                    "learnedPreferences": [],
                    "onboardingCompleted": False,
                },
            ),
        ),
        *recipes,
        (
            alice,
            ProductContainer.CHAT_HISTORY,
            NewRecord(
                kind=RecordKind.CONVERSATION, record_id=ids[3], fields={"title": "Synthetic"}
            ),
        ),
        *[
            (
                alice,
                ProductContainer.CHAT_HISTORY,
                NewRecord(
                    kind=RecordKind.TURN,
                    record_id=item_id,
                    fields={"conversationId": str(ids[3]), "turnIndex": index},
                ),
            )
            for index, item_id in enumerate(ids[4:])
        ],
    ]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CosmosSmokeError(message)


async def cleanup_smoke(settings: Settings, run_id: UUID) -> None:
    """Recover the eight exact synthetic keys, including uncertain creates."""
    _approve_target(settings)
    failures = 0
    for user, container, request in reversed(_plan(run_id)):
        item_id = user.user_id if request.kind == RecordKind.PROFILE else str(request.record_id)
        try:
            async with open_cosmos(settings) as repository:
                current = await repository.read(user, container, item_id)
                if current is not None:
                    await repository.delete(user, container, item_id, current.etag)
        except (CosmosError, ConfigurationError, OwnedResourceNotFound):
            failures += 1
    if failures:
        raise CosmosSmokeError(
            f"Cleanup incomplete for {failures} exact synthetic keys; run_id={run_id}."
        )


async def run_smoke(settings: Settings) -> UUID:
    _approve_target(settings)
    run_id = uuid4()
    print(f"Cosmos repository smoke started; run_id={run_id}.")
    plan = _plan(run_id)
    alice = plan[0][0]
    bob = plan[4][0]
    try:
        async with open_cosmos(settings) as repository:
            for user, _container, request in plan:
                await repository.create(user, request)
        async with open_cosmos(settings) as repository:
            recipe_id = str(plan[1][2].record_id)
            _require(
                await repository.read(bob, ProductContainer.RECIPE_HISTORY, recipe_id) is not None,
                "The colliding synthetic ID was not found in its own partition.",
            )
            _require(
                await repository.read(
                    bob, ProductContainer.RECIPE_HISTORY, str(plan[2][2].record_id)
                )
                is None,
                "A synthetic cross-user lookup was not hidden.",
            )
            _require(
                await repository.read(alice, ProductContainer.RECIPE_HISTORY, str(uuid4())) is None,
                "A missing synthetic record did not return absence.",
            )
            for query in (
                HistoryQuery(kind="recipe"),
                HistoryQuery(kind="turn", conversation_id=plan[5][2].record_id),
            ):
                page = await repository.query(alice, query, PageRequest(limit=1))
                seen: list[str] = []
                for _ in range(5):
                    seen.extend(str(item.body["id"]) for item in page.items)
                    if page.continuation_token is None:
                        break
                    page = await repository.query(
                        alice,
                        query,
                        PageRequest(limit=1, continuation_token=page.continuation_token),
                    )
                expected = plan[1:4] if query.kind == "recipe" else plan[6:]
                _require(
                    len(seen) == len(expected)
                    and set(seen) == {str(item[2].record_id) for item in expected}
                    and page.continuation_token is None,
                    "Synthetic pagination lost, duplicated or crossed records.",
                )
        async with open_cosmos(settings) as repository:
            original = await repository.read(alice, ProductContainer.USERS, alice.user_id)
            if original is None:
                raise CosmosSmokeError("The synthetic profile is missing.")
            changes: tuple[dict[str, JsonValue], ...] = (
                {"groceries": [{"name": "synthetic"}]},
                {"learnedPreferences": [{"insight": "synthetic"}]},
            )
            results = await asyncio.gather(
                *[
                    repository.patch(
                        alice,
                        ProductContainer.USERS,
                        alice.user_id,
                        RecordPatch(etag=original.etag, fields=fields),
                    )
                    for fields in changes
                ],
                return_exceptions=True,
            )
            conflicts = [
                result
                for result in results
                if isinstance(result, CosmosError) and result.code == CosmosFailure.CONFLICT
            ]
            _require(
                len(conflicts) == 1
                and sum(isinstance(result, BaseException) for result in results) == 1,
                "Concurrent synthetic writes did not yield one success and one conflict.",
            )
            latest = await repository.read(alice, ProductContainer.USERS, alice.user_id)
            _require(
                latest is not None
                and latest.body["explicitPreferences"] == original.body["explicitPreferences"],
                "Concurrent synthetic writes changed unrelated explicit preferences.",
            )
            if latest is None:
                raise CosmosSmokeError("The synthetic profile is missing.")
            remaining = changes[0] if latest.body["groceries"] == [] else changes[1]
            completed = await repository.patch(
                alice,
                ProductContainer.USERS,
                alice.user_id,
                RecordPatch(etag=latest.etag, fields=remaining),
            )
            _require(
                all(
                    completed.body[name] == value
                    for change in changes
                    for name, value in change.items()
                )
                and completed.body["explicitPreferences"] == original.body["explicitPreferences"],
                "Reloading after a synthetic conflict lost another profile section.",
            )
    finally:
        await cleanup_smoke(settings, run_id)
    return run_id


async def check() -> int:
    try:
        run_id = await run_smoke(load_settings())
    except OwnedResourceNotFound:
        print("FAILED: synthetic repository ownership check failed.")
        return 1
    except (ConfigurationError, CosmosError, CosmosSmokeError) as error:
        print(f"FAILED: {error}")
        return 1
    print(
        f"OK repository pagination, isolation, missing items, ETag conflict and cleanup; "
        f"run_id={run_id}."
    )
    return 0


def main() -> int:
    configure_safe_telemetry()
    return asyncio.run(check())


if __name__ == "__main__":
    raise SystemExit(main())
