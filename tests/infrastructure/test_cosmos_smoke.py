"""Exercise live-smoke orchestration offline without importing the Azure SDK."""

import unittest
from io import StringIO
from typing import Any
from unittest.mock import MagicMock, patch

from scripts.smoke_cosmos import QUERY_CASES, Container, fixtures, main, run_smoke


def fake_containers() -> dict[str, Any]:
    containers = {}
    for name in ("Users", "ChatHistory", "RecipeHistory"):
        container = MagicMock(spec=Container)
        container.read.return_value = {
            "partitionKey": {"paths": ["/userId"]},
            "indexingPolicy": {"indexingMode": "consistent"},
        }
        containers[name] = container

    def point_read(item: str, partition_key: str) -> dict[str, str]:
        return {"id": item, "userId": partition_key}

    containers["Users"].read_item.side_effect = point_read
    containers["Users"].query_items.return_value = []

    def query(**kwargs: Any) -> list[dict[str, str]]:
        for _, _, sql, parameters, expected in QUERY_CASES:
            if sql == kwargs["query"] and parameters == kwargs["parameters"]:
                kwargs["response_hook"]({"x-ms-request-charge": "2.5"}, None)
                kwargs["response_hook"]({"x-ms-request-charge": "1.5"}, None)
                return [{"id": item} for item in expected]
        raise AssertionError("Unexpected query")

    for name in ("ChatHistory", "RecipeHistory"):
        containers[name].query_items.side_effect = query
    return containers


class CosmosSmokeTests(unittest.TestCase):
    def test_cli_credential_selects_subscription_without_tenant_override(self) -> None:
        identity = MagicMock()
        cosmos = MagicMock()
        with (
            patch.dict("sys.modules", {"azure.identity": identity, "azure.cosmos": cosmos}),
            patch(
                "sys.argv",
                [
                    "smoke_cosmos.py",
                    "--endpoint",
                    "https://ai-sous-chef-dev-test.documents.azure.com/",
                    "--database",
                    "sous-chef",
                    "--subscription",
                    "approved-subscription",
                    "--allow-dev-writes",
                ],
            ),
            patch("scripts.smoke_cosmos.run_smoke", return_value=[]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            self.assertEqual(main(), 0)
        identity.AzureCliCredential.assert_called_once_with(subscription="approved-subscription")
        identity.ManagedIdentityCredential.assert_not_called()

    def test_hosted_credential_selects_only_explicit_managed_identity(self) -> None:
        identity = MagicMock()
        cosmos = MagicMock()
        with (
            patch.dict("sys.modules", {"azure.identity": identity, "azure.cosmos": cosmos}),
            patch(
                "sys.argv",
                [
                    "smoke_cosmos.py",
                    "--endpoint",
                    "https://ai-sous-chef-dev-test.documents.azure.com/",
                    "--database",
                    "sous-chef",
                    "--managed-identity-client-id",
                    "approved-client",
                    "--allow-dev-writes",
                ],
            ),
            patch("scripts.smoke_cosmos.run_smoke", return_value=[]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            self.assertEqual(main(), 0)
        identity.ManagedIdentityCredential.assert_called_once_with(client_id="approved-client")
        identity.AzureCliCredential.assert_not_called()

    def test_queries_are_partition_scoped_paged_and_report_ru(self) -> None:
        containers = fake_containers()
        results = run_smoke(containers)
        self.assertEqual(len(results), 2 * len(QUERY_CASES))
        self.assertTrue(all(row["ru"] == 4 and row["pages"] == 2 for row in results))
        users = {
            call.kwargs["partition_key"] for call in containers["Users"].read_item.call_args_list
        }
        self.assertEqual(len(users), 2)
        for container in containers.values():
            for call in container.query_items.call_args_list:
                self.assertIn(call.kwargs["partition_key"], users)
                self.assertFalse(call.kwargs["enable_cross_partition_query"])
                if container is not containers["Users"]:
                    self.assertEqual(call.kwargs["max_item_count"], 1)
            self.assertEqual(container.create_item.call_count, container.delete_item.call_count)
            for call in container.delete_item.call_args_list:
                self.assertIn(call.kwargs["partition_key"], users)
        self.assertEqual(
            containers["Users"].query_items.call_args.kwargs["parameters"][0]["value"],
            containers["Users"].read_item.call_args_list[0].kwargs["item"],
        )

    def test_bad_partition_or_retention_prevents_writes(self) -> None:
        for invalid in ({"partitionKey": {"paths": ["/wrong"]}}, {"defaultTtl": -1}):
            with self.subTest(invalid=invalid):
                containers = fake_containers()
                containers["Users"].read.return_value.update(invalid)
                with self.assertRaises(RuntimeError):
                    run_smoke(containers)
                self.assertFalse(containers["Users"].create_item.called)

    def test_query_failure_cleans_every_fixture(self) -> None:
        containers = fake_containers()
        containers["ChatHistory"].query_items.side_effect = RuntimeError("query failed")
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            run_smoke(containers)
        for container in containers.values():
            self.assertEqual(container.create_item.call_count, container.delete_item.call_count)

    def test_cross_user_results_are_rejected(self) -> None:
        containers = fake_containers()
        containers["Users"].query_items.return_value = [{"id": "another-user"}]
        with self.assertRaisesRegex(RuntimeError, "crossed the user partition"):
            run_smoke(containers)

    def test_missing_ru_evidence_fails(self) -> None:
        containers = fake_containers()
        containers["ChatHistory"].query_items.side_effect = None
        containers["ChatHistory"].query_items.return_value = [
            {"id": "conversation-b"},
            {"id": "conversation-a"},
            {"id": "conversation-z"},
        ]
        with self.assertRaisesRegex(RuntimeError, "missing request-charge"):
            run_smoke(containers)

    def test_cleanup_failure_does_not_stop_other_deletions(self) -> None:
        containers = fake_containers()
        containers["RecipeHistory"].delete_item.side_effect = RuntimeError("denied")
        with self.assertRaisesRegex(RuntimeError, "Synthetic cleanup failed"):
            run_smoke(containers)
        for container in containers.values():
            self.assertEqual(container.create_item.call_count, container.delete_item.call_count)

    def test_create_failure_attempts_cleanup_without_masking_not_found(self) -> None:
        containers = fake_containers()
        containers["Users"].create_item.side_effect = RuntimeError("write failed")

        class MissingItemError(RuntimeError):
            status_code = 404

        missing = MissingItemError("not found")
        containers["Users"].delete_item.side_effect = missing
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            run_smoke(containers)
        self.assertEqual(containers["Users"].delete_item.call_count, 1)

    def test_fixtures_are_separate_bounded_records(self) -> None:
        rows = fixtures("synthetic-user")
        self.assertTrue(
            all(
                item["userId"] == "synthetic-user"
                for documents in rows.values()
                for item in documents
            )
        )
        self.assertNotIn("messages", rows["Users"][0])
        self.assertNotIn("recipes", rows["Users"][0])


if __name__ == "__main__":
    unittest.main()
