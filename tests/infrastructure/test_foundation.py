"""Offline checks against compiled ARM templates; no Azure credentials required."""

import json
import os
import re
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
    app: ClassVar[dict[str, Any]]
    access: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = compile_bicep("infra/main.bicep")
        cls.group = compile_bicep("infra/resource-group.bicep")
        with patch.dict(
            os.environ,
            {
                "ASC_AZURE_LOCATION": "offline-test-region",
                "ASC_APP_SERVICE_LOCATION": "offline-app-region",
                "ASC_RESOURCE_OWNER": "offline-test-owner",
                "ASC_COSMOS_ENDPOINT": "https://offline.example.test/",
                "ASC_COSMOS_DATABASE": "offline-db",
                "ASC_AUTH_ISSUER": "https://synthetic.ciamlogin.com/tenant/v2.0",
                "ASC_AUTH_JWKS_URI": "https://synthetic.ciamlogin.com/tenant/discovery/v2.0/keys",
                "ASC_AUTH_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
                "ASC_AUTH_ORIGIN": "https://synthetic.azurewebsites.net",
            },
        ):
            cls.dev = compile_bicep("infra/environments/dev.bicepparam", "build-params")
            cls.prod = compile_bicep("infra/environments/prod.bicepparam", "build-params")
        modules = {
            name: resource["properties"]["template"]
            for resource in cls.template["resources"]
            for name in ("foundry", "app-service", "monitoring", "foundry-access")
            if f"-{name}'" in resource["name"]
        }
        cls.monitoring = modules["monitoring"]
        cls.foundry = modules["foundry"]
        cls.app = modules["app-service"]
        cls.access = modules["foundry-access"]

    def test_app_identity_gets_foundry_user_on_the_project_only(self) -> None:
        resources = self.access["resources"]
        roles = [r for r in resources if r["type"] == "Microsoft.Authorization/roleAssignments"]
        self.assertEqual(len(roles), 1)
        self.assertEqual(
            self.access["variables"]["foundryUserRoleId"], "53ca6127-db72-4b80-b1b0-d745d6d5456d"
        )
        self.assertIn("projects", roles[0]["scope"])
        text = json.dumps(self.access)
        owner = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
        contributor = "b24988ac-6180-42a0-ab88-20f7382dd24c"
        self.assertNotIn(owner, text)
        self.assertNotIn(contributor, text)

    def test_only_monitoring_retention_foundry_and_app_service_are_deployed(self) -> None:
        self.assertEqual(len(self.template["resources"]), 4)
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

    def test_app_service_is_https_only_with_its_own_identity_and_no_keys(self) -> None:
        resources = {resource["type"]: resource for resource in self.app["resources"]}
        plan = resources["Microsoft.Web/serverfarms"]
        site = resources["Microsoft.Web/sites"]
        self.assertEqual(plan["kind"], "linux")
        self.assertTrue(plan["properties"]["reserved"])
        self.assertEqual(plan["sku"]["capacity"], 1)
        self.assertTrue(site["properties"]["httpsOnly"])
        config = site["properties"]["siteConfig"]
        self.assertEqual(config["minTlsVersion"], "1.2")
        self.assertEqual(config["ftpsState"], "Disabled")
        self.assertEqual(config["healthCheckPath"], "/health")
        self.assertIn("PYTHON|", config["linuxFxVersion"])
        self.assertIn("--no-access-log", self.app["variables"]["startupCommand"])
        self.assertIn("sous_chef.app:create_app --factory", self.app["variables"]["startupCommand"])
        self.assertEqual(site["identity"]["type"], "UserAssigned")
        self.assertIn("Microsoft.ManagedIdentity/userAssignedIdentities", resources)
        policies = [
            r
            for r in self.app["resources"]
            if r["type"].endswith("basicPublishingCredentialsPolicies")
        ]
        self.assertEqual({p["name"].split("'")[-2] for p in policies}, {"ftp", "scm"})
        self.assertTrue(all(p["properties"]["allow"] is False for p in policies))

    def test_authored_settings_preserve_secrets_and_disable_ai_content_capture(self) -> None:
        site = next(r for r in self.app["resources"] if r["type"] == "Microsoft.Web/sites")
        self.assertNotIn("appSettings", site["properties"]["siteConfig"])
        config = next(r for r in self.app["resources"] if "-settings" in r["name"])
        settings = config["properties"]["parameters"]["settings"]["value"]
        settings_template = config["properties"]["template"]
        self.assertEqual(settings_template["parameters"]["settings"]["type"], "secureObject")
        self.assertEqual(
            {r["type"] for r in settings_template["resources"]},
            {"Microsoft.Web/sites/config"},
        )
        self.assertIn("authSettings", settings)
        self.assertIn("list(format(", settings)
        self.assertIn("/config/appsettings", settings)
        self.assertNotIn("listApplicationSettings", settings)
        self.assertIn("union(", settings)
        self.assertNotIn("PROVIDER_AUTHENTICATION_SECRET", settings)
        names = re.findall(r"createObject\('name', '([^']+)'", settings)
        for name in ("KEY", "SECRET", "PASSWORD", "TOKEN"):
            self.assertFalse([k for k in names if name in k.upper()])
        self.assertNotIn("@Microsoft.KeyVault", json.dumps(self.app))
        self.assertNotIn("authSecretUri", self.template["parameters"])
        for name, value in (
            ("ASC_ADAPTER_MODE", "live"),
            ("OTEL_SDK_DISABLED", "true"),
            ("ApplicationInsightsAgent_EXTENSION_VERSION", "disabled"),
            ("AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED", "false"),
        ):
            self.assertIn(f"createObject('name', '{name}', 'value', '{value}')", settings)
        self.assertIn("AZURE_CLIENT_ID", names)
        deployments = {
            d["name"] for d in self.template["parameters"]["modelDeployments"]["defaultValue"]
        }
        self.assertEqual(
            set(self.template["variables"]["foundryDeploymentNames"].values()), deployments
        )

    def test_cosmos_settings_are_required_per_environment(self) -> None:
        for name in ("cosmosEndpoint", "cosmosDatabase", "appServiceLocation"):
            self.assertNotIn("defaultValue", self.template["parameters"][name])
            self.assertEqual(self.template["parameters"][name]["minLength"], 1)
            self.assertIn(name, self.dev["parameters"])
            self.assertIn(name, self.prod["parameters"])

    def test_consumer_auth_is_required_and_uses_no_external_redirects(self) -> None:
        module = next(r for r in self.app["resources"] if "-authentication" in r["name"])
        policy = module["properties"]["template"]
        auth = next(r for r in policy["resources"] if "authsettingsV2" in r["name"])
        config = auth["properties"]
        self.assertTrue(config["platform"]["enabled"])
        self.assertTrue(config["globalValidation"]["requireAuthentication"])
        self.assertEqual(config["globalValidation"]["unauthenticatedClientAction"], "Return401")
        excluded = config["globalValidation"]["excludedPaths"]
        for path in ("/health", "/ready", "/auth/login", "/auth/logout", "/auth/signed-out"):
            self.assertIn(f"'{path}'", excluded)
        self.assertIn("app_service", excluded)
        self.assertTrue(config["httpSettings"]["requireHttps"])
        providers = config["identityProviders"]
        self.assertIn("azureActiveDirectory", providers)
        self.assertIn("authIssuer", providers)
        self.assertIn("allowedAudiences", providers)
        self.assertIn("allowedApplications", providers)
        for unused in (
            "apple",
            "facebook",
            "twitter",
            "legacyMicrosoftAccount",
            "azureStaticWebApps",
        ):
            self.assertIn(f"'{unused}', createObject('enabled', false())", providers)
        self.assertIn("google", providers)
        self.assertIn("gitHub", providers)
        self.assertNotIn("common/v2.0", providers)
        self.assertIn("socialAuth').tenantId", providers)
        self.assertEqual(config["login"]["allowedExternalRedirectUrls"], [])
        self.assertTrue(config["login"]["tokenStore"]["enabled"])
        self.assertEqual(config["login"]["tokenStore"]["tokenRefreshExtensionHours"], 0)
        self.assertEqual(config["login"]["cookieExpiration"]["timeToExpiration"], "01:00:00")
        for name in ("authIssuer", "authJwksUri", "authClientId", "authOrigin"):
            if name not in {"authIssuer", "authJwksUri"}:
                self.assertNotIn("defaultValue", self.template["parameters"][name])
            self.assertIn(name, self.dev["parameters"])
            self.assertIn(name, self.prod["parameters"])

    def test_tags_and_outputs_have_no_secrets(self) -> None:
        self.assertEqual(
            set(self.template["variables"]["tags"]),
            {"project", "environment", "owner", "managedBy"},
        )
        nonsecret = {
            "resourceGroupId",
            "foundryProjectEndpoint",
            "accountName",
            "projectName",
            "foundryProjectPrincipalId",
            "projectEndpoint",
            "projectPrincipalId",
            "appServiceHostName",
            "appIdentityPrincipalId",
            "appIdentityClientId",
        }
        for template in (self.template, self.monitoring, self.foundry, self.app, self.group):
            for name, output in template["outputs"].items():
                self.assertTrue(name.endswith("ResourceId") or name in nonsecret)
                self.assertEqual(output["type"], "string")
                self.assertNotIn("listKeys", output["value"])
                self.assertNotIn("ConnectionString", output["value"])

    def test_existing_app_auth_template_reuses_policy_and_preserves_other_settings(self) -> None:
        template = compile_bicep("infra/app-service-auth.bicep")
        self.assertEqual(
            {resource["type"] for resource in template["resources"]},
            {"Microsoft.Resources/deployments"},
        )

        module = next(r for r in template["resources"] if "-authentication" in r["name"])
        foundation = next(r for r in self.app["resources"] if "-authentication" in r["name"])
        self.assertEqual(module["properties"]["template"], foundation["properties"]["template"])
        self.assertEqual(module["properties"]["parameters"]["authMode"]["value"], "app_service")
        text = json.dumps(template)
        self.assertIn("list(format(", text)
        self.assertIn("/config/appsettings", text)
        self.assertNotIn("listApplicationSettings", text)
        for name in ("ASC_AUTH_ISSUER", "ASC_AUTH_JWKS_URI", "ASC_AUTH_CLIENT_ID"):
            self.assertIn(name, text)
        self.assertIn("WEBSITE_AUTH_AAD_ALLOWED_TENANTS", text)
        settings_module = next(r for r in template["resources"] if "-settings" in r["name"])
        settings = settings_module["properties"]["parameters"]["settings"]["value"]
        settings_template = settings_module["properties"]["template"]
        self.assertEqual(settings_template["parameters"]["settings"]["type"], "secureObject")
        self.assertNotIn("list(", json.dumps(settings_template))
        self.assertIn("union(", settings)
        self.assertIn("'ASC_AUTH_MODE', 'app_service'", settings)
        self.assertIn("ASC_AUTH_MICROSOFT_TENANT_ID", settings)
        self.assertIn("WEBSITE_AUTH_AAD_ALLOWED_TENANTS", settings)
        self.assertIn("defaultHostName", settings)
        self.assertNotIn("PROVIDER_AUTHENTICATION_SECRET", settings)
        self.assertNotIn("authSecretUri", template["parameters"])
        example = json.loads(
            (ROOT / "infra/environments/dev-auth.parameters.example.json").read_text()
        )
        self.assertEqual(set(example["parameters"]), {"appName", "authClientId", "socialAuth"})
        self.assertEqual(set(template["parameters"]), set(example["parameters"]))
        self.assertEqual(
            set(example["parameters"]["socialAuth"]["value"]),
            {
                "tenantId",
                "googleClientId",
                "githubClientId",
            },
        )


if __name__ == "__main__":
    unittest.main()
