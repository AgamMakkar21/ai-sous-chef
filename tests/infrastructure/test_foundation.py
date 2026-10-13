"""Offline checks against compiled ARM templates; no Azure credentials required."""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def compile_bicep(path: str, command: str = "build") -> dict[str, Any]:
    compiler = os.environ.get("BICEP_CLI") or shutil.which("bicep")
    if not compiler:
        raise RuntimeError("Install Bicep CLI or set BICEP_CLI to its executable path.")
    result = subprocess.run(
        [compiler, command, str(ROOT / path), "--stdout"],
        check=True,
        capture_output=True,
        text=True,
    )
    compiled: dict[str, Any] = json.loads(result.stdout)
    if command == "build-params":
        parameters: dict[str, Any] = json.loads(compiled["parametersJson"])
        return parameters
    return compiled


class FoundationTests(unittest.TestCase):
    template: ClassVar[dict[str, Any]]
    group: ClassVar[dict[str, Any]]
    dev: ClassVar[dict[str, Any]]
    prod: ClassVar[dict[str, Any]]
    monitoring: ClassVar[dict[str, Any]]
    foundry: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = compile_bicep("infra/main.bicep")
        cls.group = compile_bicep("infra/resource-group.bicep")
        cls.dev = compile_bicep("infra/environments/dev.bicepparam", "build-params")
        with patch.dict(
            os.environ,
            {
                "ASC_AZURE_LOCATION": "offline-test-region",
                "ASC_RESOURCE_OWNER": "offline-test-owner",
            },
        ):
            cls.prod = compile_bicep("infra/environments/prod.bicepparam", "build-params")
        modules = {
            ("foundry" if "foundry" in resource["name"] else "monitoring"): resource["properties"][
                "template"
            ]
            for resource in cls.template["resources"]
        }
        cls.monitoring = modules["monitoring"]
        cls.foundry = modules["foundry"]

    def test_only_monitoring_retention_and_foundry_are_deployed(self) -> None:
        self.assertEqual(len(self.template["resources"]), 2)
        for resource in self.template["resources"]:
            self.assertEqual(resource["type"], "Microsoft.Resources/deployments")
            self.assertEqual(resource["properties"]["mode"], "Incremental")
        self.assertEqual(
            {resource["type"] for resource in self.monitoring["resources"]},
            {
                "Microsoft.OperationalInsights/workspaces",
                "Microsoft.Insights/components",
                "Microsoft.OperationalInsights/workspaces/tables",
            },
        )

    def test_environment_selection_is_explicit_and_names_are_isolated(self) -> None:
        parameter = self.template["parameters"]["environment"]
        self.assertEqual(parameter["allowedValues"], ["dev", "prod"])
        self.assertNotIn("defaultValue", parameter)
        self.assertIn("parameters('environment')", self.template["variables"]["namePrefix"])
        for resource in self.monitoring["resources"]:
            self.assertIn("parameters('namePrefix')", resource["name"])
        for parameters, environment in ((self.dev, "dev"), (self.prod, "prod")):
            self.assertEqual(parameters["parameters"]["environment"]["value"], environment)

    def test_region_and_owner_are_required(self) -> None:
        for template in (self.template, self.group):
            for name in ("location", "owner"):
                self.assertNotIn("defaultValue", template["parameters"][name])
                self.assertEqual(template["parameters"][name]["minLength"], 1)
        self.assertEqual(self.dev["parameters"]["location"]["value"], "eastus2")

    def test_resource_group_creation_is_separate(self) -> None:
        resources = self.group["resources"]
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]["type"], "Microsoft.Resources/resourceGroups")
        self.assertIn("parameters('environment')", resources[0]["name"])

    def test_workspace_link_retention_and_entra_access(self) -> None:
        resources = {resource["type"]: resource for resource in self.monitoring["resources"]}
        workspace = resources["Microsoft.OperationalInsights/workspaces"]
        insights = resources["Microsoft.Insights/components"]
        tables = resources["Microsoft.OperationalInsights/workspaces/tables"]
        self.assertIn(
            "Microsoft.OperationalInsights/workspaces",
            insights["properties"]["WorkspaceResourceId"],
        )
        self.assertEqual(workspace["properties"]["retentionInDays"], 30)
        self.assertEqual(workspace["properties"]["sku"]["name"], "PerGB2018")
        self.assertTrue(workspace["properties"]["features"]["disableLocalAuth"])
        self.assertTrue(workspace["properties"]["features"]["immediatePurgeDataOn30Days"])
        self.assertFalse(
            workspace["properties"]["features"]["enableLogAccessUsingOnlyResourcePermissions"]
        )
        self.assertTrue(insights["properties"]["DisableLocalAuth"])
        self.assertFalse(insights["properties"]["DisableIpMasking"])
        self.assertEqual(tables["properties"]["retentionInDays"], 30)
        self.assertEqual(tables["properties"]["totalRetentionInDays"], 30)
        self.assertIn("AppRequests", self.monitoring["variables"]["applicationTables"])
        self.assertIn("AppTraces", self.monitoring["variables"]["applicationTables"])

    def test_foundry_uses_keyless_auth_and_the_three_agreed_deployments(self) -> None:
        resources = {resource["type"]: resource for resource in self.foundry["resources"]}
        account = resources["Microsoft.CognitiveServices/accounts"]
        self.assertEqual(account["kind"], "AIServices")
        self.assertTrue(account["properties"]["allowProjectManagement"])
        self.assertTrue(account["properties"]["disableLocalAuth"])
        self.assertEqual(account["identity"]["type"], "SystemAssigned")
        self.assertIn("Microsoft.CognitiveServices/accounts/projects", resources)
        self.assertIn("Microsoft.CognitiveServices/accounts/deployments", resources)
        defaults = self.template["parameters"]["modelDeployments"]["defaultValue"]
        self.assertEqual(
            {item["name"]: item["model"] for item in defaults},
            {
                "orchestrator": "gpt-5.4-mini",
                "recipe-nutrition": "gpt-5.4",
                "extraction": "gpt-5.4-nano",
            },
        )

    def test_tags_and_outputs_have_no_secrets(self) -> None:
        self.assertEqual(
            set(self.template["variables"]["tags"]),
            {"project", "environment", "owner", "managedBy"},
        )
        nonsecret = {
            "resourceGroupId",
            "foundryProjectEndpoint",
            "foundryProjectPrincipalId",
            "projectEndpoint",
            "projectPrincipalId",
        }
        for template in (self.template, self.monitoring, self.foundry, self.group):
            for name, output in template["outputs"].items():
                self.assertTrue(name.endswith("ResourceId") or name in nonsecret)
                self.assertEqual(output["type"], "string")
                self.assertNotIn("listKeys", output["value"])
                self.assertNotIn("ConnectionString", output["value"])


if __name__ == "__main__":
    unittest.main()
