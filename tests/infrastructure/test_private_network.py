"""Private Cosmos topology must reuse owner networks without relaxing public access."""

import json
import unittest
from typing import Any, ClassVar

from test_foundation import compile_bicep


class PrivateNetworkTests(unittest.TestCase):
    template: ClassVar[dict[str, Any]]
    integration: ClassVar[dict[str, Any]]
    worker: ClassVar[dict[str, Any]]
    web: ClassVar[dict[str, Any]]
    foundation: ClassVar[dict[str, Any]]
    host: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = compile_bicep("infra/cosmos-private-network.bicep")
        cls.integration = compile_bicep("infra/modules/site-vnet.bicep")
        cls.worker = compile_bicep("infra/modules/functions.bicep")
        cls.web = compile_bicep("infra/modules/app-service.bicep")
        cls.foundation = compile_bicep("infra/main.bicep")
        cls.host = compile_bicep("infra/functions.bicep")

    def test_only_endpoint_dns_group_and_two_site_integrations_are_created(self) -> None:
        kinds = [resource["type"] for resource in self.template["resources"]]
        self.assertCountEqual(
            kinds,
            [
                "Microsoft.Network/privateEndpoints",
                "Microsoft.Network/privateEndpoints/privateDnsZoneGroups",
                "Microsoft.Resources/deployments",
                "Microsoft.Resources/deployments",
            ],
        )
        for forbidden in (
            "publicNetworkAccess",
            "allowAllPublicNetwork",
            "ipRules",
            "addressPrefixes",
            "delegations",
            "Microsoft.Authorization/roleAssignments",
            "Microsoft.ManagedIdentity/userAssignedIdentities",
        ):
            self.assertNotIn(forbidden, json.dumps(self.template))

    def test_endpoint_and_dns_use_owner_supplied_resources(self) -> None:
        endpoint = next(
            resource
            for resource in self.template["resources"]
            if resource["type"] == "Microsoft.Network/privateEndpoints"
        )
        self.assertEqual(endpoint["location"], "[parameters('location')]")
        self.assertEqual(
            endpoint["properties"]["subnet"]["id"],
            "[parameters('privateEndpointSubnetResourceId')]",
        )
        connection = endpoint["properties"]["privateLinkServiceConnections"][0]["properties"]
        self.assertEqual(connection["groupIds"], ["Sql"])
        self.assertIn("Microsoft.DocumentDB/databaseAccounts", connection["privateLinkServiceId"])
        self.assertIn("parameters('cosmosAccountName')", connection["privateLinkServiceId"])
        self.assertNotIn("privateLinkServiceConnectionState", connection)
        zone = next(
            resource
            for resource in self.template["resources"]
            if resource["type"].endswith("/privateDnsZoneGroups")
        )
        self.assertEqual(
            zone["properties"]["privateDnsZoneConfigs"][0]["properties"]["privateDnsZoneId"],
            "[parameters('privateDnsZoneResourceId')]",
        )
        self.assertIn("dependsOn", zone)

    def test_subnets_and_zone_have_no_speculative_defaults(self) -> None:
        for name in (
            "location",
            "workerIntegrationSubnetResourceId",
            "webIntegrationSubnetResourceId",
            "privateEndpointSubnetResourceId",
            "privateDnsZoneResourceId",
        ):
            parameter = self.template["parameters"][name]
            self.assertEqual(parameter["minLength"], 1)
            self.assertNotIn("defaultValue", parameter)

    def test_existing_apps_are_updated_only_through_network_config(self) -> None:
        self.assertEqual(len(self.integration["resources"]), 1)
        resource = self.integration["resources"][0]
        self.assertEqual(resource["type"], "Microsoft.Web/sites/networkConfig")
        self.assertIn("parameters('appName')", resource["name"])
        self.assertEqual(
            resource["properties"],
            {
                "subnetResourceId": "[parameters('integrationSubnetResourceId')]",
                "swiftSupported": True,
            },
        )
        deployments = [
            resource
            for resource in self.template["resources"]
            if resource["type"] == "Microsoft.Resources/deployments"
        ]
        parameters = [resource["properties"]["parameters"] for resource in deployments]
        self.assertEqual(parameters[0]["appName"]["value"], "[parameters('functionAppName')]")
        self.assertEqual(parameters[1]["appName"]["value"], "[parameters('webAppName')]")
        self.assertEqual(
            [p["integrationSubnetResourceId"]["value"] for p in parameters],
            [
                "[parameters('workerIntegrationSubnetResourceId')]",
                "[parameters('webIntegrationSubnetResourceId')]",
            ],
        )
        for deployment in deployments:
            self.assertEqual(deployment["properties"]["mode"], "Incremental")
            self.assertEqual(
                deployment["properties"]["template"]["resources"],
                self.integration["resources"],
            )

    def test_full_host_and_web_deployments_can_retain_owner_integrations(self) -> None:
        for template in (self.worker, self.web):
            self.assertEqual(
                template["parameters"]["integrationSubnetResourceId"]["defaultValue"], ""
            )
            integration = next(
                resource
                for resource in template["resources"]
                if resource["type"] == "Microsoft.Resources/deployments"
                and "integrationSubnetResourceId" in resource["properties"].get("parameters", {})
            )
            self.assertEqual(
                integration["condition"],
                "[not(empty(parameters('integrationSubnetResourceId')))]",
            )
            self.assertEqual(
                integration["properties"]["parameters"]["integrationSubnetResourceId"]["value"],
                "[parameters('integrationSubnetResourceId')]",
            )
            self.assertIn("dependsOn", integration)
        for template, parameter in (
            (self.foundation, "appServiceIntegrationSubnetResourceId"),
            (self.host, "integrationSubnetResourceId"),
        ):
            self.assertEqual(template["parameters"][parameter]["defaultValue"], "")
            deployments = [
                resource
                for resource in template["resources"]
                if "integrationSubnetResourceId" in resource["properties"].get("parameters", {})
            ]
            self.assertEqual(len(deployments), 1)
            self.assertEqual(
                deployments[0]["properties"]["parameters"]["integrationSubnetResourceId"]["value"],
                f"[parameters('{parameter}')]",
            )
