# /// script
# requires-python = ">=3.12"
# dependencies = ["azure-cosmos==4.17.1", "azure-identity==1.26.0"]
# ///
"""Opt-in dev-only, synthetic Cosmos query checks; never creates cloud resources."""

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4


class Container(Protocol):
    def read(self) -> dict[str, Any]: ...
    def create_item(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def read_item(self, item: str, partition_key: str) -> dict[str, Any]: ...
    def delete_item(self, item: str, partition_key: str) -> None: ...
    def query_items(self, **kwargs: Any) -> Iterable[dict[str, Any]]: ...


QUERY_CASES = [
    (
        "ChatHistory",
        "conversations",
        "SELECT c.id FROM c WHERE c.recordKind = @kind "
        "ORDER BY c.recordKind ASC, c.createdAt DESC, c.id DESC",
        [{"name": "@kind", "value": "conversation"}],
        ["conversation-b", "conversation-a", "conversation-z"],
    ),
    (
        "ChatHistory",
        "turns",
        "SELECT c.id FROM c WHERE c.recordKind = @kind AND c.conversationId = @conversation "
        "ORDER BY c.recordKind ASC, c.conversationId ASC, c.turnIndex ASC, c.id ASC",
        [
            {"name": "@kind", "value": "turn"},
            {"name": "@conversation", "value": "conversation-a"},
        ],
        ["turn-a", "turn-b", "turn-c"],
    ),
    (
        "RecipeHistory",
        "recipes",
        "SELECT c.id FROM c WHERE c.recordKind = @kind "
        "ORDER BY c.recordKind ASC, c.createdAt DESC, c.id DESC",
        [{"name": "@kind", "value": "recipe"}],
        ["recipe-b", "recipe-a", "recipe-z"],
    ),
    (
        "RecipeHistory",
        "awaiting-feedback",
        "SELECT c.id FROM c WHERE c.recordKind = @kind "
        "AND NOT IS_NULL(c.cookedAt) AND IS_NULL(c.rating) "
        "ORDER BY c.recordKind ASC, c.createdAt DESC, c.id DESC",
        [{"name": "@kind", "value": "recipe"}],
        ["recipe-b"],
    ),
    (
        "RecipeHistory",
        "liked-recipes",
        "SELECT c.id FROM c WHERE c.recordKind = @kind AND c.rating = @rating "
        "ORDER BY c.recordKind ASC, c.createdAt DESC, c.id DESC",
        [{"name": "@kind", "value": "recipe"}, {"name": "@rating", "value": "Liked"}],
        ["recipe-a"],
    ),
    (
        "RecipeHistory",
        "generated-recipe",
        "SELECT c.id FROM c WHERE c.recordKind = @kind AND c.generatedRecipeId = @generated",
        [{"name": "@kind", "value": "recipe"}, {"name": "@generated", "value": "generated-a"}],
        ["recipe-a"],
    ),
    (
        "RecipeHistory",
        "feedback-revision",
        "SELECT c.id FROM c WHERE c.recordKind = @kind "
        "AND c.sourceId = @source AND c.feedbackRevision = @revision",
        [
            {"name": "@kind", "value": "feedbackRevision"},
            {"name": "@source", "value": "recipe-a"},
            {"name": "@revision", "value": 1},
        ],
        ["feedback-a"],
    ),
    (
        "RecipeHistory",
        "processing-receipt",
        "SELECT c.id FROM c WHERE c.recordKind = @kind "
        "AND c.sourceId = @source AND c.feedbackRevision = @revision AND c.policyVersion = @policy",
        [
            {"name": "@kind", "value": "processing"},
            {"name": "@source", "value": "recipe-a"},
            {"name": "@revision", "value": 1},
            {"name": "@policy", "value": "v1"},
        ],
        ["processing-a"],
    ),
]


def fixtures(user_id: str) -> dict[str, list[dict[str, Any]]]:
    """Storage/query sentinels, not complete application API payloads."""
    common = {
        "userId": user_id,
        "schemaVersion": 1,
        "createdAt": "2026-01-01T00:00:00Z",
    }
    rows: dict[str, list[dict[str, Any]]] = {
        "Users": [{"id": user_id, "recordKind": "profile", "groceries": []}],
        "ChatHistory": [
            {"id": "conversation-a", "recordKind": "conversation"},
            {"id": "conversation-b", "recordKind": "conversation"},
            {
                "id": "conversation-z",
                "recordKind": "conversation",
                "createdAt": "2025-12-31T00:00:00Z",
            },
            {
                "id": "turn-a",
                "recordKind": "turn",
                "conversationId": "conversation-a",
                "turnIndex": 1,
            },
            {
                "id": "turn-b",
                "recordKind": "turn",
                "conversationId": "conversation-a",
                "turnIndex": 2,
            },
            {
                "id": "turn-c",
                "recordKind": "turn",
                "conversationId": "conversation-a",
                "turnIndex": 2,
            },
            {
                "id": "other-conversation-turn",
                "recordKind": "turn",
                "conversationId": "conversation-b",
                "turnIndex": 0,
            },
            {"id": "operation-a", "recordKind": "sendOperation"},
        ],
        "RecipeHistory": [
            {
                "id": "recipe-a",
                "recordKind": "recipe",
                "generatedRecipeId": "generated-a",
                "cookedAt": "2026-01-01T00:00:00Z",
                "rating": "Liked",
                "accepted": True,
            },
            {
                "id": "recipe-b",
                "recordKind": "recipe",
                "generatedRecipeId": "generated-b",
                "cookedAt": "2026-01-01T00:00:00Z",
                "rating": None,
                "accepted": True,
            },
            {
                "id": "recipe-z",
                "recordKind": "recipe",
                "generatedRecipeId": "generated-z",
                "createdAt": "2025-12-31T00:00:00Z",
                "cookedAt": None,
                "rating": None,
                "accepted": True,
            },
            {
                "id": "feedback-a",
                "recordKind": "feedbackRevision",
                "sourceId": "recipe-a",
                "feedbackRevision": 1,
            },
            {
                "id": "processing-a",
                "recordKind": "processing",
                "sourceId": "recipe-a",
                "feedbackRevision": 1,
                "policyVersion": "v1",
            },
        ],
    }
    return {name: [{**common, **row} for row in documents] for name, documents in rows.items()}


def check_containers(containers: Mapping[str, Container]) -> None:
    for name, container in containers.items():
        properties = container.read()
        if properties["partitionKey"]["paths"] != ["/userId"]:
            raise RuntimeError(f"{name}: incompatible immutable partition key")
        if properties.get("defaultTtl") is not None:
            raise RuntimeError(f"{name}: unexpected automatic retention/TTL configuration")
        if properties["indexingPolicy"]["indexingMode"] != "consistent":
            raise RuntimeError(f"{name}: required consistent indexes are missing")


def run_smoke(containers: Mapping[str, Container]) -> list[dict[str, Any]]:
    check_containers(containers)
    run_id = str(uuid4())
    users = [f"asc-smoke-{run_id}-a", f"asc-smoke-{run_id}-b"]
    created: list[tuple[Container, str, str]] = []
    results: list[dict[str, Any]] = []
    try:
        for user in users:
            for name, documents in fixtures(user).items():
                for document in documents:
                    created.append((containers[name], document["id"], user))
                    containers[name].create_item(document)
            profile = containers["Users"].read_item(item=user, partition_key=user)
            if profile["id"] != user or profile["userId"] != user:
                raise RuntimeError("Profile point read returned unexpected ownership")
        # A mismatched record ID and partition must not discover another user's profile.
        if list(
            containers["Users"].query_items(
                query="SELECT c.id FROM c WHERE c.id = @id",
                parameters=[{"name": "@id", "value": users[0]}],
                partition_key=users[1],
                enable_cross_partition_query=False,
            )
        ):
            raise RuntimeError("Profile query crossed the user partition")
        for user in users:
            for name, label, query, parameters, expected in QUERY_CASES:
                charges: list[float] = []

                def response_hook(
                    headers: Mapping[str, str], _: Any, charges: list[float] = charges
                ) -> None:
                    charges.append(float(headers["x-ms-request-charge"]))

                items = list(
                    containers[name].query_items(
                        query=query,
                        parameters=parameters,
                        partition_key=user,
                        enable_cross_partition_query=False,
                        max_item_count=1,
                        response_hook=response_hook,
                    )
                )
                if [item["id"] for item in items] != expected:
                    raise RuntimeError(f"{name}/{label}: unexpected query results/order")
                if not charges or sum(charges) <= 0:
                    raise RuntimeError(f"{name}/{label}: missing request-charge evidence")
                results.append(
                    {
                        "container": name,
                        "query": label,
                        "pages": len(charges),
                        "ru": round(sum(charges), 3),
                    }
                )
    finally:
        cleanup_errors = []
        for container, item_id, user in reversed(created):
            try:
                container.delete_item(item=item_id, partition_key=user)
            except Exception as error:
                if getattr(error, "status_code", None) == 404:
                    continue
                # Attempt every exact synthetic ID even after one cleanup failure.
                cleanup_errors.append(type(error).__name__)
        if cleanup_errors:
            raise RuntimeError(
                f"Synthetic cleanup failed for run {run_id}: {', '.join(cleanup_errors)}"
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--database", required=True)
    authentication = parser.add_mutually_exclusive_group(required=True)
    authentication.add_argument("--subscription")
    authentication.add_argument("--managed-identity-client-id")
    parser.add_argument("--allow-dev-writes", required=True, action="store_true")
    args = parser.parse_args()
    url = urlparse(args.endpoint)
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.startswith("ai-sous-chef-dev-")
        or not url.hostname.endswith(".documents.azure.com")
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or url.port not in (None, 443)
        or args.database != "sous-chef"
    ):
        parser.error("Use the approved dev Cosmos endpoint and sous-chef database only.")
    from azure.cosmos import CosmosClient
    from azure.identity import AzureCliCredential, ManagedIdentityCredential

    credential = (
        ManagedIdentityCredential(client_id=args.managed_identity_client_id)
        if args.managed_identity_client_id
        else AzureCliCredential(subscription=args.subscription)
    )
    with (
        credential,
        CosmosClient(args.endpoint, credential=credential) as client,
    ):
        database = client.get_database_client(args.database)
        containers = {
            name: database.get_container_client(name)
            for name in ("Users", "ChatHistory", "RecipeHistory")
        }
        results = run_smoke(containers)
    print(json.dumps({"status": "passed", "queries": results}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Cosmos smoke failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
