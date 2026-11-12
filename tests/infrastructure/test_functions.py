"""Offline worker wiring and delivery checks; no Azure credentials."""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import azure.functions as func
from azure.functions.decorators.retry_policy import RETRY_POLICY
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

    def test_cosmos_writes_are_scoped_to_learning_profiles_sources_and_leases(self) -> None:
        grants = resources(self.access, "Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments")
        self.assertEqual(len(grants), 4)
        source, profile, lease = grants[1:]
        self.assertIn("/dbs/sous-chef/colls/RecipeHistory", source["properties"]["scope"])
        self.assertIn("00000000-0000-0000-0000-000000000002", str(source))
        self.assertIn("/dbs/sous-chef/colls/Users", profile["properties"]["scope"])
        self.assertIn("00000000-0000-0000-0000-000000000002", str(profile))
        self.assertIn("/dbs/sous-chef/colls/leases", lease["properties"]["scope"])
        self.assertIn("00000000-0000-0000-0000-000000000002", str(lease))
        role = resources(self.access, "Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions")[0]
        self.assertEqual(
            role["properties"]["permissions"],
            [{"dataActions": ["Microsoft.DocumentDB/databaseAccounts/readMetadata"]}],
        )
        for forbidden in ("Owner", "listKeys", "ChatHistory"):
            self.assertNotIn(forbidden, json.dumps(self.access))

    def test_monitoring_is_connected_and_does_not_collect_payloads(self) -> None:
        diagnostic = resources(self.template, "Microsoft.Insights/diagnosticSettings")[0]
        self.assertIn("logAnalyticsResourceId", diagnostic["properties"]["workspaceId"])
        self.assertEqual(
            diagnostic["properties"]["logs"], [{"category": "FunctionAppLogs", "enabled": True}]
        )
        config = json.loads((ROOT / "worker/host.json").read_text())
        self.assertEqual(config["extensionBundle"]["version"], "[4.39.1, 4.39.2)")
        telemetry = config["logging"]["applicationInsights"]
        self.assertFalse(telemetry["enableDependencyTracking"])
        self.assertFalse(
            telemetry["httpAutoCollectionOptions"]["enableHttpTriggerExtendedInfoCollection"]
        )
        self.assertFalse(telemetry["httpAutoCollectionOptions"]["enableQueryStringTracing"])

    def test_private_host_storage_adds_only_blob_network_access_not_public_access_or_roles(
        self,
    ) -> None:
        template = compile_bicep("infra/functions-storage-private-network.bicep")
        endpoint = resources(template, "Microsoft.Network/privateEndpoints")[0]
        connection = endpoint["properties"]["privateLinkServiceConnections"][0]["properties"]
        self.assertEqual(connection["groupIds"], ["blob"])
        self.assertIn("storageAccountName", connection["privateLinkServiceId"])
        self.assertEqual(
            endpoint["properties"]["subnet"]["id"],
            "[parameters('privateEndpointSubnetResourceId')]",
        )
        link = resources(template, "Microsoft.Network/privateDnsZones/virtualNetworkLinks")[0]
        self.assertFalse(link["properties"]["registrationEnabled"])
        self.assertEqual(
            link["properties"]["virtualNetwork"]["id"],
            "[parameters('virtualNetworkResourceId')]",
        )
        dns = resources(template, "Microsoft.Network/privateEndpoints/privateDnsZoneGroups")[0]
        self.assertEqual(len(dns["properties"]["privateDnsZoneConfigs"]), 1)
        self.assertEqual(
            {resource["type"] for resource in template["resources"]},
            {
                "Microsoft.Network/privateEndpoints",
                "Microsoft.Network/privateDnsZones",
                "Microsoft.Network/privateDnsZones/virtualNetworkLinks",
                "Microsoft.Network/privateEndpoints/privateDnsZoneGroups",
            },
        )


class FunctionsHostTests(unittest.TestCase):
    def test_health_and_identity_based_feedback_trigger_are_registered(self) -> None:
        functions = host.app.get_functions()
        indexed = {function.get_function_name(): function for function in functions}
        self.assertEqual(set(indexed), {"host_health", "feedback_change_feed"})
        bindings = json.loads(indexed["host_health"].get_function_json())["bindings"]
        trigger = next(binding for binding in bindings if binding["type"] == "httpTrigger")
        self.assertEqual(trigger["route"], "health")
        self.assertEqual(trigger["methods"], ["GET"])
        definition = json.loads(indexed["feedback_change_feed"].get_function_json())
        trigger = next(b for b in definition["bindings"] if b["type"] == "cosmosDBTrigger")
        self.assertEqual(trigger["connection"], "FeedbackCosmos")
        self.assertEqual(trigger["databaseName"], "%ASC_COSMOS_DATABASE%")
        self.assertEqual(trigger["containerName"], "%ASC_FEEDBACK_CONTAINER%")
        self.assertEqual(trigger["leaseConnection"], "FeedbackLeases")
        self.assertEqual(trigger["leaseDatabaseName"], "%ASC_COSMOS_DATABASE%")
        self.assertEqual(trigger["leaseContainerName"], "%ASC_LEASE_CONTAINER%")
        self.assertFalse(trigger["createLeaseContainerIfNotExists"])
        self.assertEqual(trigger["leaseContainerPrefix"], "feedback-v1")
        self.assertTrue(trigger["startFromBeginning"])
        self.assertEqual(trigger["maxItemsPerInvocation"], 50)
        self.assertEqual(
            indexed["feedback_change_feed"].get_settings_dict(RETRY_POLICY),
            {
                "setting_name": "retry_policy",
                "strategy": "fixed_delay",
                "max_retry_count": "-1",
                "delay_interval": "00:01:00",
            },
        )

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
            {"ASC_ENVIRONMENT": "local"},
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
            "no_health",
            "wrong_health",
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
            elif mode == "no_health":
                del config["healthCheckPath"]
            elif mode == "wrong_health":
                config["healthCheckPath"] = "/health"
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

    def test_dev_delivery_applies_only_declared_host_settings_and_reads_them_back(self) -> None:
        for changes in (
            {},
            {"linuxFxVersion": "Python|3.11"},
            {"alwaysOn": False},
            {"alwaysOn": 1},
            {"healthCheckPath": None},
            {"linuxFxVersion": "", "alwaysOn": False, "healthCheckPath": ""},
        ):
            worker, web, config = self.fixtures()
            responses = [
                worker,
                web,
                [{"name": key, "value": value} for key, value in SETTINGS.items()],
                config | changes,
                None,
                config,
            ]
            with (
                self.subTest(changes=changes),
                patch.dict(os.environ, ENV, clear=True),
                patch.object(delivery, "az", side_effect=responses) as az,
            ):
                self.assertEqual(delivery.worker_target(configure_host=True), worker)
                self.assertEqual(az.call_count, 6 if changes else 4)
                if changes:
                    update = az.call_args_list[4]
                    self.assertEqual(update.args[:3], ("webapp", "config", "set"))
                    self.assertEqual(update.args[update.args.index("-n") + 1], "synthetic-worker")
                    payload = update.args[update.args.index("--generic-configurations") + 1]
                    self.assertEqual(json.loads(payload), config)
                    self.assertEqual(update.kwargs, {"expect_json": False})
                    self.assertEqual(az.call_args_list[5].args[:3], ("webapp", "config", "show"))

    def test_host_reconciliation_cannot_continue_after_a_failed_update_or_readback(self) -> None:
        for failure in ("update", "readback"):
            worker, web, config = self.fixtures()
            drift = config | {"healthCheckPath": None}
            with (
                self.subTest(failure=failure),
                patch.dict(os.environ, ENV, clear=True),
                patch.object(
                    delivery,
                    "az",
                    side_effect=[
                        worker,
                        web,
                        [{"name": key, "value": value} for key, value in SETTINGS.items()],
                        drift,
                        delivery.DeliveryError("Update rejected") if failure == "update" else None,
                        drift,
                    ],
                ) as az,
            ):
                with self.assertRaises(delivery.DeliveryError) as caught:
                    delivery.worker_target(configure_host=True)
                self.assertEqual(az.call_count, 5 if failure == "update" else 6)
                if failure == "readback":
                    self.assertIn("healthCheckPath", str(caught.exception))

    def test_host_reconciliation_still_rejects_unsafe_targets_before_writing(self) -> None:
        for mode in ("prod", "same_identity", "connection_string", "no_build"):
            worker, web, config = self.fixtures()
            settings = dict(SETTINGS)
            if mode == "prod":
                worker["tags"]["environment"] = "prod"
            elif mode == "same_identity":
                worker["identity"] = web["identity"]
            elif mode == "connection_string":
                settings["AzureWebJobsStorage"] = "private-key"
            else:
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
                        config | {"healthCheckPath": None},
                    ],
                ) as az,
            ):
                with self.assertRaises(delivery.DeliveryError) as caught:
                    delivery.worker_target(configure_host=True)
                self.assertNotIn("private-key", str(caught.exception))
                self.assertFalse(
                    any(call.args[:3] == ("webapp", "config", "set") for call in az.call_args_list)
                )

    def test_production_and_dispatch_checks_cannot_reconcile_host_settings(self) -> None:
        with patch.object(delivery, "az") as az:
            with self.assertRaisesRegex(delivery.DeliveryError, "reviewed dev push"):
                delivery.worker_target("prod", configure_host=True)
            with self.assertRaisesRegex(delivery.DeliveryError, "reviewed dev push"):
                delivery.worker_target(allow_dispatch=True, configure_host=True)
            az.assert_not_called()

    def test_package_uses_committed_worker_and_has_correct_zip_root(self) -> None:
        source = io.BytesIO()
        with zipfile.ZipFile(source, "w") as archive:
            for name in (
                "host.json",
                "function_app.py",
                "extraction.py",
                "replay.py",
                "requirements.txt",
            ):
                archive.writestr(f"worker/{name}", name)
            for name in (
                "__init__.py",
                "domain/extraction.py",
                "domain/food_safety.py",
                "adapters/cosmos.py",
                "services/learning.py",
                "services/learning_recovery.py",
                "settings.py",
            ):
                archive.writestr(f"backend/src/sous_chef/{name}", name)
            archive.writestr("worker/release.json", "stale")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.zip"
            with patch(
                "scripts.deploy_dev.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=source.getvalue()),
            ) as run:
                delivery.worker_package(SHA, path)
            self.assertEqual(
                run.call_args.args[0],
                [
                    "git",
                    "archive",
                    "--format=zip",
                    SHA,
                    "worker",
                    "backend/src/sous_chef",
                ],
            )
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "host.json",
                        "function_app.py",
                        "worker/extraction.py",
                        "worker/replay.py",
                        "worker/__init__.py",
                        "requirements.txt",
                        "release.json",
                        "sous_chef/__init__.py",
                        "sous_chef/domain/extraction.py",
                        "sous_chef/domain/food_safety.py",
                        "sous_chef/adapters/cosmos.py",
                        "sous_chef/services/learning.py",
                        "sous_chef/services/learning_recovery.py",
                        "sous_chef/settings.py",
                    },
                )
                self.assertEqual(json.loads(archive.read("release.json")), {"commit": SHA})
            with zipfile.ZipFile(path) as archive:
                archive.extractall(Path(directory) / "app")
            runtime = Path(directory) / "runtime"
            runtime.mkdir()
            (runtime / "worker.py").write_text("raise RuntimeError('runtime module collision')")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import sys; sys.path[:0] = sys.argv[1:]; "
                    "import worker; import importlib.util; "
                    "assert importlib.util.find_spec('worker.extraction') is not None",
                    str(Path(directory) / "app"),
                    str(runtime),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_deploy_requires_current_commit_and_successful_build(self) -> None:
        for mode in (
            "valid",
            "stale",
            "checkout",
            "unpublished_bundle",
            "failed_build",
            "cli_failure",
        ):
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
                    patch.object(delivery, "worker_target") as target,
                    patch.object(
                        delivery, "worker_package", side_effect=lambda *args: args[1].touch()
                    ) as package,
                    patch.object(
                        delivery,
                        "validate_worker_bundle",
                        side_effect=delivery.DeliveryError("Bundle not published")
                        if mode == "unpublished_bundle"
                        else None,
                    ) as validate,
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
                        target.assert_called_once_with(configure_host=True)
                        validate.assert_called_once_with(archive)
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
                        elif mode == "unpublished_bundle":
                            az.assert_not_called()
                        else:
                            self.assertEqual(az.call_count, 1)
                    self.assertFalse(archive.exists())

    def test_worker_bundle_must_be_published_on_functions_cdn_not_only_github(self) -> None:
        from urllib.error import URLError

        config = json.loads((ROOT / "worker/host.json").read_text())
        cases: tuple[tuple[dict[str, Any], object, Exception | None, str | None], ...] = (
            (config, ["4.39.1"], None, None),
            (config, ["4.38.1"], None, "not published"),
            (config, {}, None, "not published"),
            (config, [], URLError("private upstream error"), "Cannot verify"),
            (config, [], ValueError("private malformed response"), "Cannot verify"),
            (
                config
                | {
                    "extensionBundle": config["extensionBundle"]
                    | {
                        "version": "[4.41.0, 4.41.1)",
                    }
                },
                ["4.39.1"],
                None,
                "4.41.0 is not published",
            ),
            (
                config
                | {
                    "extensionBundle": config["extensionBundle"]
                    | {
                        "version": "[4.0.0, 5.0.0)",
                    }
                },
                ["4.39.1"],
                None,
                "exactly one patch",
            ),
        )
        for config, versions, error, expected in cases:
            with self.subTest(config=config, versions=versions, expected=expected):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "worker.zip"
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("host.json", json.dumps(config))
                    with patch(
                        "urllib.request.urlopen",
                        side_effect=error,
                        return_value=io.BytesIO(json.dumps(versions).encode()),
                    ) as request:
                        if expected is None:
                            delivery.validate_worker_bundle(path)
                        else:
                            with self.assertRaisesRegex(
                                delivery.DeliveryError, expected
                            ) as failure:
                                delivery.validate_worker_bundle(path)
                            self.assertNotIn("private", str(failure.exception))
                        if request.called:
                            self.assertEqual(
                                request.call_args.args[0],
                                "https://cdn.functions.azure.com/public/ExtensionBundles/"
                                "Microsoft.Azure.Functions.ExtensionBundle/index.json",
                            )
                            self.assertEqual(request.call_args.kwargs, {"timeout": 30})

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
                        "does not verify feedback processing",
                        (Path(directory) / "summary").read_text(),
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
