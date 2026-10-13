"""Offline Cosmos ARM contract checks; no Azure sign-in or SDK required."""

import os
import unittest
from typing import Any, ClassVar
from unittest.mock import patch

from test_foundation import compile_bicep


class CosmosTests(unittest.TestCase):
    template: ClassVar[dict[str, Any]]
    module: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = compile_bicep("infra/cosmos.bicep")
        cls.module = cls.template["resources"][0]["properties"]["template"]

    def test_explicit_dev_environment_and_nonsecret_outputs(self) -> None:
        self.assertEqual(self.template["parameters"]["environment"]["allowedValues"], ["dev"])
        for name in ("environment", "location", "owner"):
            self.assertNotIn("defaultValue", self.template["parameters"][name])
        with patch.dict(os.environ, {"ASC_COSMOS_LOCATION": "approved-test-region"}):
            dev = compile_bicep("infra/environments/dev-cosmos.bicepparam", "build-params")
        self.assertEqual(dev["parameters"]["location"]["value"], "approved-test-region")
        for name in ("approvedIpRanges", "productWriterPrincipalIds"):
            self.assertEqual(dev["parameters"][name]["value"], [])
        self.assertFalse(dev["parameters"]["allowAllPublicNetwork"]["value"])
        self.assertFalse(self.template["parameters"]["allowAllPublicNetwork"]["defaultValue"])
        for output in self.template["outputs"].values():
            self.assertNotIn("listKeys", output["value"])

    def test_serverless_single_region_and_seven_day_backup(self) -> None:
        account = self.module["resources"][0]
        properties = account["properties"]
        self.assertEqual(properties["capabilities"], [{"name": "EnableServerless"}])
        self.assertEqual(properties["consistencyPolicy"]["defaultConsistencyLevel"], "Session")
        self.assertEqual(len(properties["locations"]), 1)
        self.assertEqual(
            properties["backupPolicy"]["continuousModeProperties"]["tier"], "Continuous7Days"
        )
        for resource in self.module["resources"]:
            self.assertNotIn("options", resource.get("properties", {}))

    def test_entra_only_firewall_with_no_azure_bypass(self) -> None:
        properties = self.module["resources"][0]["properties"]
        self.assertTrue(properties["disableLocalAuth"])
        self.assertTrue(properties["disableKeyBasedMetadataWriteAccess"])
        self.assertEqual(properties["networkAclBypass"], "None")
        self.assertIn("empty(parameters('approvedIpRanges'))", properties["publicNetworkAccess"])
        self.assertIn("parameters('allowAllPublicNetwork')", properties["publicNetworkAccess"])
        self.assertIn("'Disabled'", properties["publicNetworkAccess"])
        self.assertIn("parameters('approvedIpRanges')", str(properties["copy"]))

    def test_product_partition_and_lease_separation_with_no_ttl(self) -> None:
        resources = self.module["resources"]
        product = next(
            resource
            for resource in resources
            if resource.get("copy", {}).get("name") == "containers"
        )
        lease = next(
            resource
            for resource in resources
            if resource.get("properties", {}).get("resource", {}).get("id") == "leases"
        )
        for resource, path in ((product, "/userId"), (lease, "/id")):
            stored = resource["properties"]["resource"]
            self.assertEqual(
                stored["partitionKey"],
                {
                    "paths": [path],
                    "kind": "Hash",
                    "version": 2,
                },
            )
            self.assertNotIn("defaultTtl", stored)
        self.assertEqual(
            [container["name"] for container in self.module["variables"]["productContainers"]],
            ["Users", "ChatHistory", "RecipeHistory"],
        )

    def test_bounded_index_paths_and_required_orderings(self) -> None:
        containers = {row["name"]: row for row in self.module["variables"]["productContainers"]}
        for row in containers.values():
            self.assertNotIn("/id/?", row["paths"])
        chat = containers["ChatHistory"]
        recipe = containers["RecipeHistory"]
        for row in (chat, recipe):
            self.assertIn("/recordKind/?", row["paths"])
            self.assertIn("/createdAt/?", row["paths"])
            self.assertEqual(
                row["composites"][0],
                [
                    {"path": "/recordKind", "order": "ascending"},
                    {"path": "/createdAt", "order": "descending"},
                    {"path": "/id", "order": "descending"},
                ],
            )
        self.assertEqual(
            chat["composites"][1],
            [
                {"path": path, "order": "ascending"}
                for path in ("/recordKind", "/conversationId", "/turnIndex", "/id")
            ],
        )
        for path in (
            "generatedRecipeId",
            "cookedAt",
            "rating",
            "sourceId",
            "feedbackRevision",
            "behaviorRevision",
            "policyVersion",
            "status",
        ):
            self.assertIn(f"/{path}/?", recipe["paths"])
        product = next(
            resource
            for resource in self.module["resources"]
            if resource.get("copy", {}).get("name") == "containers"
        )
        self.assertEqual(
            product["properties"]["resource"]["indexingPolicy"]["excludedPaths"], [{"path": "/*"}]
        )

    def test_incremental_and_deterministic_product_only_grants(self) -> None:
        deployment = self.template["resources"][0]
        self.assertEqual(deployment["properties"]["mode"], "Incremental")
        self.assertIn("-cosmos-resources", deployment["name"])
        self.assertIn(
            "uniqueString", deployment["properties"]["parameters"]["accountName"]["value"]
        )
        grant = next(
            resource
            for resource in self.module["resources"]
            if resource.get("copy", {}).get("name") == "writers"
        )
        self.assertIn("guid(", grant["name"])
        self.assertIn("containerName", grant["properties"]["scope"])
        self.assertIn("/dbs/", grant["properties"]["scope"])
        self.assertIn("/colls/", grant["properties"]["scope"])
        self.assertIn(
            "00000000-0000-0000-0000-000000000002", grant["properties"]["roleDefinitionId"]
        )
        metadata = next(
            resource
            for resource in self.module["resources"]
            if resource["type"].endswith("sqlRoleDefinitions")
        )
        self.assertEqual(
            metadata["properties"]["permissions"],
            [
                {
                    "dataActions": ["Microsoft.DocumentDB/databaseAccounts/readMetadata"],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
