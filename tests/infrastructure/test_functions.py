"""Host-only acceptance checks; no Azure credentials or business events."""

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import azure.functions as func
from test_foundation import compile_bicep

from scripts import deploy_dev as delivery
from worker import function_app as host

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
CLIENT = "11111111-1111-1111-1111-111111111111"
SETTINGS = {
    "ASC_ENVIRONMENT": "dev",
    "FUNCTIONS_EXTENSION_VERSION": "~4",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AZURE_CLIENT_ID": CLIENT,
    "AzureWebJobsStorage__accountName": "synthetichost",
    "FeedbackCosmos__accountEndpoint": "https://synthetic.documents.azure.com:443/",
    "FeedbackLeases__accountEndpoint": "https://synthetic.documents.azure.com:443/",
    "ASC_COSMOS_DATABASE": "sous-chef",
    "ASC_FEEDBACK_CONTAINER": "RecipeHistory",
    "ASC_LEASE_CONTAINER": "leases",
    "SCM_DO_BUILD_DURING_DEPLOYMENT": "true",
    "ENABLE_ORYX_BUILD": "true",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=synthetic",
    "APPLICATIONINSIGHTS_AUTHENTICATION_STRING": f"ClientId={CLIENT};Authorization=AAD",
}
for prefix in ("AzureWebJobsStorage", "FeedbackCosmos", "FeedbackLeases"):
    SETTINGS[f"{prefix}__credential"] = "managedidentity"
    SETTINGS[f"{prefix}__clientId"] = CLIENT
ENV = {
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_REF": "refs/heads/dev",
    "GITHUB_SHA": SHA,
    "AZURE_SUBSCRIPTION_ID": "synthetic",
    "ASC_RESOURCE_GROUP": "synthetic-dev",
    "ASC_APP_NAME": "synthetic-web",
    "ASC_FUNCTION_APP_NAME": "synthetic-worker",
}


def resources(template: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [resource for resource in template["resources"] if resource["type"] == kind]


class FunctionsInfrastructureTests(unittest.TestCase):
    template: ClassVar[dict[str, Any]]
    access: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        compile_bicep("infra/functions.bicep")
        cls.template = compile_bicep("infra/modules/functions.bicep")
        cls.access = compile_bicep("infra/modules/functions-cosmos-access.bicep")

    def test_isolated_identity_plan_and_patched_runtime(self) -> None:
        identities = resources(self.template, "Microsoft.ManagedIdentity/userAssignedIdentities")
        self.assertEqual(len(identities), 1)
        self.assertIn("-feedback-identity", identities[0]["name"])
        site = resources(self.template, "Microsoft.Web/sites")[0]
        self.assertEqual(site["kind"], "functionapp,linux")
        self.assertEqual(site["identity"]["type"], "UserAssigned")
        self.assertTrue(site["properties"]["httpsOnly"])
        config = site["properties"]["siteConfig"]
        self.assertEqual(config["linuxFxVersion"], "Python|3.12")
        self.assertTrue(config["alwaysOn"])
        self.assertEqual(config["healthCheckPath"], "/api/health")
        self.assertEqual(config["ftpsState"], "Disabled")
        plan = resources(self.template, "Microsoft.Web/serverfarms")[0]
        self.assertEqual(plan["sku"], {"name": "B1", "capacity": 1})
        settings = {s["name"]: s["value"] for s in config["appSettings"]}
        self.assertEqual(settings["FUNCTIONS_EXTENSION_VERSION"], "~4")
        for prefix in ("AzureWebJobsStorage", "FeedbackCosmos", "FeedbackLeases"):
            self.assertEqual(settings[f"{prefix}__credential"], "managedidentity")
            self.assertEqual(settings[f"{prefix}__clientId"], settings["AZURE_CLIENT_ID"])
            self.assertNotIn(prefix, settings)
        self.assertNotIn("WEBSITE_CONTENTAZUREFILECONNECTIONSTRING", settings)
        self.assertNotIn("WEBSITE_RUN_FROM_PACKAGE", settings)

    def test_keyless_storage_and_precisely_scoped_monitoring_permissions(self) -> None:
        storage = resources(self.template, "Microsoft.Storage/storageAccounts")[0]
        self.assertFalse(storage["properties"]["allowSharedKeyAccess"])
        self.assertFalse(storage["properties"]["allowBlobPublicAccess"])
        self.assertEqual(storage["properties"]["minimumTlsVersion"], "TLS1_2")
        grants = resources(self.template, "Microsoft.Authorization/roleAssignments")
        self.assertEqual(len(grants), 2)
        self.assertIn("Microsoft.Storage/storageAccounts", grants[0]["scope"])
        self.assertIn("b7e6dc6d-f1e8-4753-8033-0f276bb0955b", str(grants[0]))
        self.assertIn("Microsoft.Insights/components", grants[1]["scope"])
        self.assertIn("3913510d-42f4-4e42-8a64-420c390055eb", str(grants[1]))
        policies = resources(
            self.template, "Microsoft.Web/sites/basicPublishingCredentialsPolicies"
        )
        self.assertEqual(len(policies), 2)
        self.assertTrue(all(not policy["properties"]["allow"] for policy in policies))

    def test_cosmos_is_source_reader_lease_writer_and_metadata_only(self) -> None:
        grants = resources(self.access, "Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments")
        self.assertEqual(len(grants), 3)
        source, lease = grants[1:]
        self.assertIn("/dbs/sous-chef/colls/RecipeHistory", source["properties"]["scope"])
        self.assertIn("00000000-0000-0000-0000-000000000001", str(source))
        self.assertIn("/dbs/sous-chef/colls/leases", lease["properties"]["scope"])
        self.assertIn("00000000-0000-0000-0000-000000000002", str(lease))
        role = resources(self.access, "Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions")[0]
        self.assertEqual(
            role["properties"]["permissions"],
            [{"dataActions": ["Microsoft.DocumentDB/databaseAccounts/readMetadata"]}],
        )
        for forbidden in ("Owner", "listKeys", "ChatHistory", "/colls/Users"):
            self.assertNotIn(forbidden, json.dumps(self.access))

    def test_monitoring_is_connected_and_does_not_collect_payloads(self) -> None:
        diagnostic = resources(self.template, "Microsoft.Insights/diagnosticSettings")[0]
        self.assertIn("logAnalyticsResourceId", diagnostic["properties"]["workspaceId"])
        self.assertEqual(
            diagnostic["properties"]["logs"], [{"category": "FunctionAppLogs", "enabled": True}]
        )
        config = json.loads((ROOT / "worker/host.json").read_text())
        self.assertEqual(config["extensionBundle"]["version"], "[4.41.0, 4.41.1)")
        telemetry = config["logging"]["applicationInsights"]
        self.assertFalse(telemetry["enableDependencyTracking"])
        self.assertFalse(
            telemetry["httpAutoCollectionOptions"]["enableHttpTriggerExtendedInfoCollection"]
        )
        self.assertFalse(telemetry["httpAutoCollectionOptions"]["enableQueryStringTracing"])


class FunctionsHostTests(unittest.TestCase):
    def test_only_health_is_registered_not_a_feedback_trigger(self) -> None:
        functions = host.app.get_functions()
        self.assertEqual(len(functions), 1)
        self.assertEqual(functions[0].get_function_name(), "host_health")
        bindings = json.loads(functions[0].get_function_json())["bindings"]
        trigger = next(binding for binding in bindings if binding["type"] == "httpTrigger")
        self.assertEqual(trigger["route"], "health")
        self.assertEqual(trigger["methods"], ["GET"])

    def test_health_is_fixed_and_matches_packaged_revision(self) -> None:
        with (
            patch.dict(os.environ, SETTINGS, clear=True),
            patch.object(Path, "read_text", return_value=json.dumps({"commit": SHA})),
        ):
            request = func.HttpRequest("GET", "https://synthetic/api/health", body=b"private")
            response = host.host_health(request)
        self.assertEqual(json.loads(response.get_body()), {"status": "ok", "scope": "host-only"})
        self.assertEqual(response.headers["X-ASC-Commit"], SHA)
        self.assertNotIn("private", response.get_body().decode())

    def test_invalid_configuration_fails_without_exposing_values(self) -> None:
        for changes in (
            {"AZURE_CLIENT_ID": ""},
            {"FeedbackCosmos": "private-key"},
            {"AzureWebJobsStorage": "private-key"},
            {"FeedbackLeases__clientId": "different"},
            {"FeedbackCosmos__credential": "developer"},
            {"FeedbackCosmos__accountEndpoint": "https://private-key@host.example"},
            {
                "FeedbackCosmos__accountEndpoint": "https://synthetic.documents.azure.com:private-key/"
            },
            {"FeedbackLeases__accountEndpoint": "https://other.documents.azure.com/"},
            {"AzureWebJobsStorage__accountName": ""},
            {"ASC_ENVIRONMENT": "prod"},
            {"ASC_LEASE_CONTAINER": ""},
            {"FUNCTIONS_WORKER_RUNTIME": "node"},
        ):
            with (
                self.subTest(changes=changes),
                patch.dict(os.environ, SETTINGS | changes, clear=True),
            ):
                with self.assertRaises(RuntimeError) as failure:
                    host.validate_configuration()
                self.assertNotIn("private-key", str(failure.exception))

    def test_release_missing_or_invalid_is_not_healthy(self) -> None:
        with patch.dict(os.environ, SETTINGS, clear=True):
            for value in ("{}", '{"commit":"not-a-commit"}', '{"commit":null}'):
                with patch.object(Path, "read_text", return_value=value):
                    with self.assertRaises(RuntimeError):
                        host.host_health(MagicMock())
            with patch.object(Path, "read_text", side_effect=FileNotFoundError):
                with self.assertRaises(FileNotFoundError):
                    host.host_health(MagicMock())


class FunctionsDeliveryTests(unittest.TestCase):
    def fixtures(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        worker: dict[str, Any] = {
            "id": "/sites/worker",
            "kind": "functionapp,linux",
            "httpsOnly": True,
            "defaultHostName": "synthetic-worker.azurewebsites.net",
            "tags": {"environment": "dev", "project": "ai-sous-chef"},
            "identity": {
                "type": "UserAssigned",
                "userAssignedIdentities": {
                    "/identities/worker": {"clientId": CLIENT, "principalId": "worker-principal"}
                },
            },
        }
        web = copy.deepcopy(worker)
        web["id"] = "/sites/web"
        web["identity"]["userAssignedIdentities"] = {
            "/identities/web": {"clientId": "web-client", "principalId": "web-principal"}
        }
        config = {
            "linuxFxVersion": "Python|3.12",
            "alwaysOn": True,
            "healthCheckPath": "/api/health",
        }
        return worker, web, config

    def test_target_rejects_production_shared_identity_and_config_drift(self) -> None:
        for mode in (
            "valid",
            "prod",
            "same_site",
            "same_identity",
            "same_principal",
            "same_client",
            "missing_identity",
            "runtime",
            "storage_key",
            "cosmos_key",
            "lease_identity",
            "wrong_endpoint",
            "no_monitoring_auth",
            "no_always_on",
            "no_build",
        ):
            worker, web, config = self.fixtures()
            settings = dict(SETTINGS)
            if mode == "prod":
                worker["tags"]["environment"] = "prod"
            elif mode == "same_site":
                worker["id"] = web["id"]
            elif mode == "same_identity":
                worker["identity"] = web["identity"]
            elif mode in ("same_principal", "same_client"):
                field = "principalId" if mode == "same_principal" else "clientId"
                worker["identity"]["userAssignedIdentities"]["/identities/worker"][field] = web[
                    "identity"
                ]["userAssignedIdentities"]["/identities/web"][field]
            elif mode == "missing_identity":
                worker["identity"] = {}
            elif mode == "runtime":
                config["linuxFxVersion"] = "Python|3.11"
            elif mode == "storage_key":
                settings["AzureWebJobsStorage"] = "private-key"
            elif mode == "cosmos_key":
                settings["FeedbackCosmos"] = "private-key"
            elif mode == "lease_identity":
                settings["FeedbackLeases__clientId"] = "other"
            elif mode == "wrong_endpoint":
                settings["FeedbackCosmos__accountEndpoint"] = "https://wrong.example"
            elif mode == "no_monitoring_auth":
                del settings["APPLICATIONINSIGHTS_AUTHENTICATION_STRING"]
            elif mode == "no_always_on":
                config["alwaysOn"] = False
            elif mode == "no_build":
                settings["SCM_DO_BUILD_DURING_DEPLOYMENT"] = "false"
            with (
                self.subTest(mode=mode),
                patch.dict(os.environ, ENV, clear=True),
                patch.object(
                    delivery,
                    "az",
                    side_effect=[
                        worker,
                        web,
                        [{"name": key, "value": value} for key, value in settings.items()],
                        config,
                    ],
                ),
            ):
                if mode == "valid":
                    self.assertEqual(delivery.worker_target(), worker)
                else:
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.worker_target()

    def test_package_uses_committed_worker_and_has_correct_zip_root(self) -> None:
        source = io.BytesIO()
        with zipfile.ZipFile(source, "w") as archive:
            for name in ("host.json", "function_app.py", "requirements.txt"):
                archive.writestr(f"worker/{name}", name)
            archive.writestr("worker/release.json", "stale")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.zip"
            with patch(
                "scripts.deploy_dev.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=source.getvalue()),
            ) as run:
                delivery.worker_package(SHA, path)
            self.assertEqual(
                run.call_args.args[0], ["git", "archive", "--format=zip", SHA, "worker"]
            )
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"host.json", "function_app.py", "requirements.txt", "release.json"},
                )
                self.assertEqual(json.loads(archive.read("release.json")), {"commit": SHA})

    def test_deploy_requires_current_commit_and_successful_build(self) -> None:
        for mode in ("valid", "stale", "checkout", "failed_build", "cli_failure"):
            with tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "asc-worker-dev.zip"
                with (
                    self.subTest(mode=mode),
                    patch.dict(os.environ, ENV | {"RUNNER_TEMP": directory}),
                    patch.object(
                        delivery, "command", return_value="b" * 40 if mode == "checkout" else SHA
                    ),
                    patch.object(
                        delivery,
                        "current_dev_commit",
                        return_value="b" * 40 if mode == "stale" else SHA,
                    ),
                    patch.object(delivery, "worker_target"),
                    patch.object(
                        delivery, "worker_package", side_effect=lambda *args: args[1].touch()
                    ) as package,
                    patch.object(
                        delivery,
                        "az",
                        side_effect=delivery.DeliveryError("ZIP failed")
                        if mode == "cli_failure"
                        else None,
                        return_value={"status": 3 if mode == "failed_build" else 4},
                    ) as az,
                ):
                    if mode == "valid":
                        delivery.worker_deploy()
                        self.assertEqual(az.call_count, 2)
                        self.assertEqual(az.call_args.kwargs, {"expect_json": False})
                        self.assertIn("synthetic-worker", az.call_args_list[0].args)
                        self.assertNotIn("synthetic-web", az.call_args_list[0].args)
                    else:
                        with self.assertRaises(delivery.DeliveryError):
                            delivery.worker_deploy()
                        if mode in ("stale", "checkout"):
                            package.assert_not_called()
                            az.assert_not_called()
                        else:
                            self.assertEqual(az.call_count, 1)
                    self.assertFalse(archive.exists())

    def test_smoke_checks_running_revision_and_fails_closed(self) -> None:
        class Response(io.BytesIO):
            def __init__(self, mode: str) -> None:
                super().__init__(
                    b"not-json"
                    if mode == "invalid"
                    else json.dumps({"status": "ok", "scope": "host-only"}).encode()
                )
                self.status = 302 if mode == "redirect" else 200
                self.headers = {"X-ASC-Commit": "b" * 40 if mode == "stale" else SHA}

        for mode in ("valid", "stale", "redirect", "invalid", "timeout"):
            worker, _, _ = self.fixtures()
            opener = MagicMock()
            opener.open.return_value = Response(mode)
            if mode == "timeout":
                opener.open.side_effect = TimeoutError
            with (
                tempfile.TemporaryDirectory() as directory,
                self.subTest(mode=mode),
                patch.dict(
                    os.environ, ENV | {"GITHUB_STEP_SUMMARY": str(Path(directory) / "summary")}
                ),
                patch.object(delivery, "worker_target", return_value=worker),
                patch("urllib.request.build_opener", return_value=opener),
                patch("scripts.deploy_dev.time.monotonic", side_effect=[0, 0, 301]),
                patch("scripts.deploy_dev.time.sleep"),
            ):
                if mode == "valid":
                    delivery.worker_smoke()
                    self.assertIn(
                        "No feedback event processing", (Path(directory) / "summary").read_text()
                    )
                else:
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.worker_smoke()

    def test_workflow_requires_worker_delivery_in_existing_reviewed_job(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        self.assertIn("ASC_FUNCTION_APP_NAME: ${{ vars.ASC_FUNCTION_APP_NAME }}", workflow)
        self.assertIn("python scripts/deploy_dev.py worker-deploy", workflow)
        self.assertIn("python scripts/deploy_dev.py worker-smoke", workflow)
        self.assertLess(workflow.index("needs: reviewed"), workflow.index("worker-deploy"))
