"""Offline deployment acceptance: no Azure SDK imports or credentials required."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError

from scripts import deploy_dev as delivery

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
HEAD = "b" * 40
ENV = {
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_REF": "refs/heads/dev",
    "GITHUB_SHA": SHA,
    "GITHUB_REPOSITORY": "AgamMakkar21/ai-sous-chef",
    "AZURE_SUBSCRIPTION_ID": "synthetic-subscription",
    "ASC_RESOURCE_GROUP": "test-2",
    "ASC_APP_NAME": "ai-sous-chef-dev-web",
    "ASC_FOUNDRY_ACCOUNT_NAME": "ai-sous-chef-dev-foundry",
    "ASC_FOUNDRY_PROJECT_NAME": "ai-sous-chef-dev-project",
}
AGENT_PINS = {
    "orchestrator": {"name": "orchestrator", "version": "1"},
    "recipe_nutrition": {
        "name": "recipe-nutrition",
        "version": "1",
        "retrieval_version": "2",
        "web_version": "3",
    },
}
ENABLED_MANIFEST = {
    "schema_version": 3,
    "agents_enabled": True,
    "agents": AGENT_PINS,
}


class DeliveryTests(unittest.TestCase):
    def test_combined_release_keeps_both_tools_and_isolated_calorie_phase(self) -> None:
        from azure.ai.projects.models import AgentVersionDetails
        from sous_chef.recipe_definition import (
            RecipePhase,
            build_recipe_definition,
            recipe_knowledge_tool,
            recipe_source_metadata,
        )

        from scripts.release import validate_runtime

        data: dict[str, Any] = {
            "schema_version": 4,
            "agents_enabled": True,
            "agents": {
                "orchestrator": {"name": "orchestrator", "version": "1"},
                "recipe_nutrition": {"name": "recipe-nutrition", "version": "4"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            (root / "deploy" / "agents.dev.json").write_text(json.dumps(data))
            with patch.object(delivery, "ROOT", root):
                agents = delivery.manifest()
                self.assertEqual(agents["recipe_nutrition"]["mode"], "combined")
                data["agents"]["recipe_nutrition"]["web_version"] = "5"
                (root / "deploy" / "agents.dev.json").write_text(json.dumps(data))
                agents = delivery.manifest()
                self.assertEqual(agents["recipe_nutrition"]["web_version"], "5")
        combined = build_recipe_definition(
            "recipe-model",
            "approved-policy",
            phase=RecipePhase.COMBINED,
            knowledge_tool=recipe_knowledge_tool(
                "https://synthetic.search.windows.net/knowledgebases/fixture/mcp",
                "fixture-mcp",
            ),
        )
        web = build_recipe_definition("recipe-model", "approved-policy", phase=RecipePhase.WEB)
        project = MagicMock()
        project.agents.get.side_effect = [
            SimpleNamespace(
                state="enabled",
                instance_identity=SimpleNamespace(
                    principal_id=principal,
                    client_id=principal + "-client",
                ),
            )
            for principal in (
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            )
        ]
        project.agents.get_version.side_effect = [
            AgentVersionDetails(
                {
                    "name": "orchestrator",
                    "version": "1",
                    "status": "active",
                    "draft": False,
                    "definition": {
                        "kind": "prompt",
                        "model": "orchestrator",
                        "instructions": "approved",
                    },
                }
            ),
            AgentVersionDetails(
                {
                    "name": "recipe-nutrition",
                    "version": "4",
                    "status": "active",
                    "draft": False,
                    "definition": combined.as_dict(),
                    "metadata": recipe_source_metadata(combined, RecipePhase.COMBINED),
                }
            ),
            AgentVersionDetails(
                {
                    "name": "recipe-nutrition",
                    "version": "5",
                    "status": "active",
                    "draft": False,
                    "definition": web.as_dict(),
                    "metadata": recipe_source_metadata(web, RecipePhase.WEB),
                }
            ),
        ]
        project.agents.create_version.side_effect = [
            SimpleNamespace(version="8"),
            SimpleNamespace(version="9"),
            SimpleNamespace(version="10"),
        ]
        with patch.dict(os.environ, ENV):
            pins = delivery.release_agents(
                project,
                agents,
                "11111111-1111-1111-1111-111111111111",
            )
        self.assertEqual(project.agents.get_version.call_count, 3)
        self.assertEqual(project.agents.create_version.call_count, 3)
        self.assertEqual(pins["ASC_FOUNDRY_RECIPE_NUTRITION_MODE"], "combined")
        self.assertEqual(pins["ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_VERSION"], "9")
        self.assertEqual(pins["ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION"], "10")
        published = project.agents.create_version.call_args_list
        self.assertEqual(published[1].kwargs["definition"], combined)
        self.assertEqual(published[2].kwargs["definition"], web)
        self.assertEqual(published[2].kwargs["metadata"]["asc_recipe_phase"], "web")
        validate_runtime(
            pins
            | {
                "ASC_AGENTS_ENABLED": "true",
                "ASC_APP_WORKLOAD_PRINCIPAL_ID": "11111111-1111-1111-1111-111111111111",
                "ASC_READINESS_TIMEOUT_SECONDS": "10",
            }
        )

    def test_azure_output_contract_is_explicit_and_json_reads_remain_strict(self) -> None:
        with patch.dict(os.environ, ENV):
            with patch.object(delivery, "command", return_value="") as run:
                self.assertIsNone(delivery.az("webapp", "restart", expect_json=False))
                self.assertEqual(run.call_args.args[-2:], ("-o", "none"))
            with patch.object(delivery, "command", return_value='{"status":4}') as run:
                self.assertEqual(delivery.az("webapp", "deploy"), {"status": 4})
                self.assertEqual(run.call_args.args[-2:], ("-o", "json"))
            for invalid in ("", "not-json"):
                with patch.object(delivery, "command", return_value=invalid):
                    with self.assertRaises(json.JSONDecodeError):
                        delivery.az("webapp", "show")
            with patch.object(
                delivery, "command", side_effect=delivery.DeliveryError("restart failed")
            ):
                with self.assertRaises(delivery.DeliveryError):
                    delivery.az("webapp", "restart", expect_json=False)

    def test_command_failure_identifies_operation_without_credentials_or_output(self) -> None:
        with patch(
            "scripts.deploy_dev.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 1, stdout="private-stdout", stderr="private-stderr"
            ),
        ):
            with self.assertRaises(delivery.DeliveryError) as failure:
                delivery.command(
                    "az", "webapp", "config", "appsettings", "set", "--settings", "secret=value"
                )
        self.assertIn("az webapp config appsettings set failed", str(failure.exception))
        for private in ("private-stdout", "private-stderr", "secret=value"):
            self.assertNotIn(private, str(failure.exception))

    def test_workflow_is_separate_from_untrusted_pr_validation(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        self.assertIn("branches: [dev]", workflow)
        for forbidden in ("pull_request:", "pull_request_target:", "workflow_run:", "secrets."):
            self.assertNotIn(forbidden, workflow)
        self.assertEqual(workflow.count("id-token: write"), 1)
        self.assertIn("needs: reviewed", workflow)
        self.assertIn("github.ref == 'refs/heads/dev'", workflow)
        self.assertIn("name: dev", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("id-token", workflow.split("\n  deploy:")[0])
        self.assertIn("python scripts/deploy_dev.py smoke", workflow)
        self.assertIn("python -m scripts.release check-dev-ci", workflow.split("\n  deploy:")[0])

    def test_workflow_variables_are_unique_and_available_to_deployment(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        reviewed, deploy = workflow.split("\n  deploy:", maxsplit=1)
        token = "GH_TOKEN: ${{ github.token }}"
        self.assertEqual(reviewed.count(token), 1)
        self.assertNotIn("PYTHONPATH:", reviewed)
        job_configuration, _ = deploy.split("\n    steps:", maxsplit=1)
        self.assertEqual(deploy.count(token), 1)
        self.assertIn(token, job_configuration)
        self.assertIn("PYTHONPATH: backend/src", job_configuration)

    def test_only_dev_push_events_are_accepted(self) -> None:
        for event, ref in (
            ("pull_request", "refs/heads/dev"),
            ("push", "refs/heads/main"),
            ("workflow_dispatch", "refs/heads/dev"),
        ):
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": event, "GITHUB_REF": ref}):
                with self.assertRaises(delivery.DeliveryError):
                    delivery.dev_event()

    def test_merge_requires_peer_approval_of_final_head(self) -> None:
        pr = {
            "number": 99,
            "base": {"ref": "dev", "repo": {"full_name": ENV["GITHUB_REPOSITORY"]}},
            "merged_at": "2026-10-15T12:00:00Z",
            "merge_commit_sha": SHA,
            "head": {"sha": HEAD},
            "user": {"login": "author"},
        }
        approval = {
            "user": {"login": "peer", "type": "User"},
            "state": "APPROVED",
            "commit_id": HEAD,
            "author_association": "COLLABORATOR",
        }
        for change in (
            {},
            {"commit_id": SHA},
            {"user": {"login": "author", "type": "User"}},
            {"author_association": "CONTRIBUTOR"},
            {"user": {"login": "peer", "type": "Bot"}},
        ):
            review = approval | change
            with (
                patch.dict(os.environ, ENV),
                patch("scripts.deploy_dev.time.sleep"),
                patch.object(
                    delivery,
                    "command",
                    side_effect=[
                        json.dumps([pr]),
                        json.dumps([[review]]),
                    ]
                    * 6,
                ),
            ):
                if not change:
                    delivery.reviewed()
                else:
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.reviewed()
        dismissed = approval | {"state": "DISMISSED"}
        with (
            patch.dict(os.environ, ENV),
            patch("scripts.deploy_dev.time.sleep"),
            patch.object(
                delivery,
                "command",
                side_effect=[
                    json.dumps([pr]),
                    json.dumps([[approval], [dismissed]]),
                ]
                * 6,
            ),
        ):
            with self.assertRaises(delivery.DeliveryError):
                delivery.reviewed()

    def test_direct_push_does_not_deploy(self) -> None:
        with (
            patch.dict(os.environ, ENV),
            patch.object(delivery, "command", return_value="[]") as command,
            patch("scripts.deploy_dev.time.sleep") as sleep,
        ):
            with self.assertRaises(delivery.DeliveryError):
                delivery.reviewed()
        self.assertEqual(command.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    def test_merge_and_review_evidence_can_become_visible_after_the_push(self) -> None:
        pr = {
            "number": 99,
            "base": {"ref": "dev", "repo": {"full_name": ENV["GITHUB_REPOSITORY"]}},
            "merged_at": "2026-10-15T12:00:00Z",
            "merge_commit_sha": SHA,
            "head": {"sha": HEAD},
            "user": {"login": "author"},
        }
        approval = {
            "user": {"login": "peer", "type": "User"},
            "state": "APPROVED",
            "commit_id": HEAD,
            "author_association": "COLLABORATOR",
        }
        with (
            patch.dict(os.environ, ENV),
            patch("scripts.deploy_dev.time.sleep") as sleep,
            patch.object(
                delivery,
                "command",
                side_effect=[
                    "[]",
                    json.dumps([pr]),
                    "[[]]",
                    json.dumps([pr]),
                    json.dumps([[approval]]),
                ],
            ) as command,
        ):
            delivery.reviewed()
        self.assertEqual(command.call_count, 5)
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(5), unittest.mock.call(5)])

    def test_review_api_failures_remain_explicit_without_retrying(self) -> None:
        with (
            patch.dict(os.environ, ENV),
            patch("scripts.deploy_dev.time.sleep") as sleep,
            patch.object(
                delivery, "command", side_effect=delivery.DeliveryError("gh api failed")
            ) as command,
        ):
            with self.assertRaisesRegex(delivery.DeliveryError, "gh api failed"):
                delivery.reviewed()
        command.assert_called_once()
        sleep.assert_not_called()

    def test_manifest_rejects_latest_shared_names_missing_roles_and_bad_shapes(self) -> None:
        original = ENABLED_MANIFEST
        variants = [
            [],
            {"schema_version": 1, "agents": {}},
            {"schema_version": 2, "agents": {}},
            original | {"schema_version": 2.0},
            original | {"agents_enabled": "false"},
            original | {"agents_enabled": 0},
            original | {"agents_enabled": False},
            original | {"agents": {}},
            original | {"schema_version": 2},
            original | {"agents": {"orchestrator": {}, "recipe_nutrition": []}},
        ]
        for field, value in (("version", "latest"), ("name", "recipe-nutrition")):
            data = json.loads(json.dumps(original))
            data["agents"]["orchestrator"][field] = value
            variants.append(data)
        for field in ("retrieval_version", "web_version"):
            for value in ("latest", "1"):
                data = json.loads(json.dumps(original))
                data["agents"]["recipe_nutrition"][field] = value
                variants.append(data)
        for data in variants:
            with patch.object(Path, "read_text", return_value=json.dumps(data)):
                with self.assertRaises(delivery.DeliveryError):
                    delivery.manifest()

    def test_manifest_requires_explicit_disabled_or_fully_pinned_enabled_release(self) -> None:
        self.assertIsInstance(delivery.manifest(), dict)
        disabled = {"schema_version": 2, "agents_enabled": False, "agents": {}}
        with patch.object(Path, "read_text", return_value=json.dumps(disabled)):
            self.assertEqual(delivery.manifest(), {})
        with patch.object(Path, "read_text", return_value=json.dumps(ENABLED_MANIFEST)):
            self.assertEqual(delivery.manifest(), ENABLED_MANIFEST["agents"])

    def test_target_checks_tags_endpoint_identity_and_easy_auth_before_writes(self) -> None:
        tags = {"environment": "dev", "project": "ai-sous-chef"}
        app = {
            "id": "/subscriptions/test/resourceGroups/test/providers/Microsoft.Web/sites/test",
            "tags": tags,
            "identity": {"userAssignedIdentities": {"identity": {}}},
        }
        settings = {
            "ASC_ENVIRONMENT": "dev",
            "ASC_ADAPTER_MODE": "live",
            "AZURE_CLIENT_ID": "workload-client",
            "ASC_FOUNDRY_PROJECT_ENDPOINT": (
                "https://ai-sous-chef-dev-foundry.services.ai.azure.com"
                "/api/projects/ai-sous-chef-dev-project"
            ),
        }
        auth = {
            "platform": {"enabled": True},
            "globalValidation": {
                "requireAuthentication": True,
                "unauthenticatedClientAction": "Return401",
            },
        }
        for bad in ("none", "prod", "endpoint", "identity", "auth"):
            candidate = json.loads(json.dumps(app))
            config = settings.copy()
            authentication = json.loads(json.dumps(auth))
            if bad == "prod":
                candidate["tags"]["environment"] = "prod"
            elif bad == "endpoint":
                config["ASC_FOUNDRY_PROJECT_ENDPOINT"] = "https://wrong.example/"
            elif bad == "identity":
                candidate["identity"] = {}
            elif bad == "auth":
                authentication["platform"]["enabled"] = False
            with (
                patch.dict(os.environ, ENV),
                patch.object(
                    delivery,
                    "az",
                    side_effect=[
                        candidate,
                        {"tags": tags},
                        [{"name": key, "value": value} for key, value in config.items()],
                        {"properties": authentication},
                    ],
                ) as cli,
            ):
                if bad == "none":
                    delivery.target()
                else:
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.target()
                self.assertTrue(all("set" not in call.args for call in cli.call_args_list))
                if bad == "none":
                    self.assertEqual(cli.call_args.args[:3], ("rest", "--method", "GET"))
                    self.assertIn(
                        f"https://management.azure.com{app['id']}/config/authsettingsV2/list"
                        "?api-version=2024-04-01",
                        cli.call_args.args,
                    )

    def test_multi_provider_policy_is_validated_before_publication(self) -> None:
        origin = "https://synthetic.azurewebsites.net"
        client = "22222222-2222-2222-2222-222222222222"
        tenant = "11111111-1111-1111-1111-111111111111"
        issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
        settings = {
            "ASC_AUTH_MODE": "app_service",
            "ASC_AUTH_ORIGIN": origin,
            "ASC_AUTH_MICROSOFT_TENANT_ID": tenant,
            "WEBSITE_AUTH_AAD_ALLOWED_TENANTS": tenant,
        }
        providers: dict[str, dict[str, object]] = {}
        for name, prefix in (
            ("azureActiveDirectory", "MICROSOFT"),
            ("google", "GOOGLE"),
            ("gitHub", "GITHUB"),
        ):
            secret = f"{prefix}_PROVIDER_AUTHENTICATION_SECRET"
            settings[secret] = f"synthetic-{prefix}-credential"
            providers[name] = {
                "enabled": True,
                "registration": {"clientId": client, "clientSecretSettingName": secret},
            }
        providers["azureActiveDirectory"] = {
            "enabled": True,
            "registration": {
                "clientId": client,
                "clientSecretSettingName": "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET",
                "openIdIssuer": issuer,
            },
            "validation": {
                "allowedAudiences": [client],
                "defaultAuthorizationPolicy": {"allowedApplications": [client]},
            },
        }
        policy = {
            "platform": {"enabled": True},
            "globalValidation": {
                "requireAuthentication": True,
                "unauthenticatedClientAction": "Return401",
                "excludedPaths": [
                    "/health",
                    "/ready",
                    "/auth/login",
                    "/auth/logout",
                    "/auth/signed-out",
                    "/auth/login/aad",
                    "/auth/login/google",
                    "/auth/login/github",
                ],
            },
            "httpSettings": {"requireHttps": True},
            "identityProviders": providers,
            "login": {
                "allowedExternalRedirectUrls": [],
                "cookieExpiration": {"convention": "FixedTime", "timeToExpiration": "01:00:00"},
                "tokenStore": {"enabled": True, "tokenRefreshExtensionHours": 0},
            },
        }
        delivery.validate_authentication(policy, settings, "synthetic.azurewebsites.net")
        for prefix in ("MICROSOFT", "GOOGLE", "GITHUB"):
            for value in (
                None,
                "",
                " \t\r\n",
                "@Microsoft.KeyVault(SecretUri=https://synthetic.vault.azure.net/secrets/login)",
                " @microsoft.keyvault(VaultName=synthetic;SecretName=login)",
            ):
                with self.subTest(provider=prefix, invalid_secret=value):
                    config = settings.copy()
                    key = f"{prefix}_PROVIDER_AUTHENTICATION_SECRET"
                    if value is None:
                        config.pop(key)
                    else:
                        config[key] = value
                    with self.assertRaises(delivery.DeliveryError) as failure:
                        delivery.validate_authentication(
                            policy, config, "synthetic.azurewebsites.net"
                        )
                    for credential_value in (settings[key], value):
                        if credential_value and credential_value.strip():
                            self.assertNotIn(credential_value, str(failure.exception))
        for bad in (
            "origin",
            "token_store",
            "refresh",
            "expiry",
            "https",
            "exclusion",
            "redirect",
            "disabled",
            "extra",
            "registration",
            "tenant",
            "organizations",
            "consumers",
            "tenant_allowlist",
            "missing_tenant",
            "invalid_tenant",
            "missing_tenant_allowlist",
            "identity_allowlist",
            "client_allowlist",
            "audience",
            "mixed",
            "mode",
        ):
            with self.subTest(bad=bad):
                candidate = json.loads(json.dumps(policy))
                config = settings.copy()
                if bad == "origin":
                    config["ASC_AUTH_ORIGIN"] = "https://attacker.invalid"
                elif bad == "token_store":
                    candidate["login"]["tokenStore"]["enabled"] = False
                elif bad == "refresh":
                    candidate["login"]["tokenStore"]["tokenRefreshExtensionHours"] = 72
                elif bad == "expiry":
                    candidate["login"]["cookieExpiration"]["timeToExpiration"] = "24:00:00"
                elif bad == "https":
                    candidate["httpSettings"]["requireHttps"] = False
                elif bad == "exclusion":
                    candidate["globalValidation"]["excludedPaths"].append("/api/*")
                elif bad == "redirect":
                    candidate["login"]["allowedExternalRedirectUrls"] = ["https://attacker.invalid"]
                elif bad == "disabled":
                    candidate["identityProviders"]["gitHub"]["enabled"] = False
                elif bad == "extra":
                    candidate["identityProviders"]["facebook"] = {"enabled": True}
                elif bad == "registration":
                    candidate["identityProviders"]["google"]["registration"]["clientId"] = ""
                elif bad == "tenant":
                    candidate["identityProviders"]["azureActiveDirectory"]["registration"][
                        "openIdIssuer"
                    ] = "https://login.microsoftonline.com/common/v2.0"
                elif bad in {"organizations", "consumers"}:
                    candidate["identityProviders"]["azureActiveDirectory"]["registration"][
                        "openIdIssuer"
                    ] = f"https://login.microsoftonline.com/{bad}/v2.0"
                elif bad == "tenant_allowlist":
                    config["WEBSITE_AUTH_AAD_ALLOWED_TENANTS"] = (
                        "22222222-2222-2222-2222-222222222222"
                    )
                elif bad == "missing_tenant":
                    config.pop("ASC_AUTH_MICROSOFT_TENANT_ID")
                elif bad == "invalid_tenant":
                    config["ASC_AUTH_MICROSOFT_TENANT_ID"] = "common"
                elif bad == "missing_tenant_allowlist":
                    config.pop("WEBSITE_AUTH_AAD_ALLOWED_TENANTS")
                elif bad == "identity_allowlist":
                    candidate["identityProviders"]["azureActiveDirectory"]["validation"][
                        "defaultAuthorizationPolicy"
                    ]["allowedPrincipals"] = {"identities": ["only-one-user"]}
                elif bad == "client_allowlist":
                    candidate["identityProviders"]["azureActiveDirectory"]["validation"][
                        "defaultAuthorizationPolicy"
                    ] = {"allowedApplications": ["unapproved-client"]}
                elif bad == "audience":
                    candidate["identityProviders"]["azureActiveDirectory"]["validation"][
                        "allowedAudiences"
                    ] = ["other"]
                elif bad == "mixed":
                    config["ASC_AUTH_ISSUER"] = "https://synthetic.ciamlogin.com/"
                elif bad == "mode":
                    config["ASC_AUTH_MODE"] = "bypass"
                with self.assertRaises(delivery.DeliveryError) as failure:
                    delivery.validate_authentication(
                        candidate, config, "synthetic.azurewebsites.net"
                    )
                for value in settings.values():
                    if value.startswith("synthetic-"):
                        self.assertNotIn(value, str(failure.exception))

    def test_archive_contains_exact_git_commit_and_no_environment_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reviewed"
            backend = root / "backend" / "src" / "sous_chef"
            backend.mkdir(parents=True)
            (backend / "app.py").write_bytes(b"# reviewed backend fixture\n")
            (backend / "release.json").write_text('{"commit":"old"}')
            (root / "requirements.txt").write_text("# locked requirement fixture\n")
            (root / ".gitignore").write_text(".env*\nbackend/src/sous_chef/static/\n")
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Verification",
                    "-c",
                    "user.email=verification@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    f"core.hooksPath={root / 'no-hooks'}",
                    "commit",
                    "--quiet",
                    "-m",
                    "Synthetic reviewed source",
                ],
                cwd=root,
                check=True,
            )
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            (backend / "app.py").write_text("# unreviewed working-tree fixture\n")
            (root / ".env.local").write_text("SYNTHETIC_SECRET=must-not-be-packaged\n")
            (backend / "static").mkdir()
            frontend = b"<html><body>compiled frontend fixture</body></html>"
            (backend / "static" / "index.html").write_bytes(frontend)
            archive = Path(directory) / "app.zip"
            with patch.object(delivery, "ROOT", root):
                delivery.package(sha, archive)
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
                self.assertIn("requirements.txt", names)
                self.assertIn("backend/src/sous_chef/app.py", names)
                self.assertEqual(
                    package.read("backend/src/sous_chef/app.py"), b"# reviewed backend fixture\n"
                )
                self.assertEqual(package.read("backend/src/sous_chef/static/index.html"), frontend)
                self.assertNotIn("backend/src/sous_chef/_usda/main.py", names)
                self.assertEqual(
                    json.loads(package.read("backend/src/sous_chef/release.json")), {"commit": sha}
                )
                self.assertFalse(any(".env" in name or ".git/" in name for name in names))
                self.assertTrue(
                    all(name == "requirements.txt" or name.startswith("backend/") for name in names)
                )

    def test_archive_rejects_a_missing_frontend_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(delivery, "ROOT", root),
                patch("scripts.deploy_dev.subprocess.run") as archive,
            ):
                with self.assertRaisesRegex(delivery.DeliveryError, "Build the frontend"):
                    delivery.package(SHA, root / "app.zip")
                archive.assert_not_called()
                self.assertFalse((root / "app.zip").exists())

    def test_agent_release_preserves_approved_definition_and_separate_identities(self) -> None:
        from azure.ai.projects.models import AgentVersionDetails
        from sous_chef.recipe_definition import (
            RecipePhase,
            build_recipe_definition,
            recipe_knowledge_tool,
            recipe_source_metadata,
        )

        for mode in ("ok", "shared", "workload", "legacy", "disabled", "phase"):
            sources = [
                AgentVersionDetails(
                    {
                        "name": "orchestrator",
                        "version": "1",
                        "status": "active",
                        "draft": False,
                        "definition": {
                            "kind": "prompt",
                            "model": "orchestrator-model",
                            "instructions": "approved",
                        },
                        "metadata": {},
                    }
                )
            ]
            for index, phase in enumerate(
                (
                    RecipePhase.GENERATION,
                    RecipePhase.RETRIEVAL,
                    RecipePhase.WEB,
                ),
                1,
            ):
                definition = build_recipe_definition(
                    "recipe-model",
                    "approved-policy",
                    phase=phase,
                    knowledge_tool=recipe_knowledge_tool(
                        "https://synthetic.search.windows.net/knowledgebases/fixture/mcp",
                        "fixture-mcp",
                    )
                    if phase == RecipePhase.RETRIEVAL
                    else None,
                )
                metadata = recipe_source_metadata(definition, phase)
                if mode == "phase" and phase == RecipePhase.WEB:
                    metadata["asc_recipe_phase"] = "generation"
                sources.append(
                    AgentVersionDetails(
                        {
                            "name": "recipe-nutrition",
                            "version": str(index),
                            "status": "active",
                            "draft": False,
                            "definition": definition.as_dict(),
                            "metadata": metadata,
                        }
                    )
                )
            project = MagicMock()
            project.agents.get.side_effect = [
                SimpleNamespace(
                    state="disabled" if mode == "disabled" else "enabled",
                    instance_identity=None
                    if mode == "legacy"
                    else SimpleNamespace(
                        principal_id="app" if mode == "workload" else "orchestrator-id",
                        client_id="orchestrator-client",
                    ),
                ),
                SimpleNamespace(
                    state="enabled",
                    instance_identity=SimpleNamespace(
                        principal_id="orchestrator-id" if mode == "shared" else "recipe-id",
                        client_id="recipe-client",
                    ),
                ),
            ]
            project.agents.get_version.side_effect = sources
            project.agents.create_version.side_effect = [
                SimpleNamespace(version=str(number)) for number in range(8, 12)
            ]
            with (
                patch.dict(os.environ, ENV),
                patch("sys.path", [str(ROOT / "backend" / "src"), *sys.path]),
            ):
                if mode != "ok":
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.release_agents(project, AGENT_PINS, "app")
                    project.agents.create_version.assert_not_called()
                    continue
                pins = delivery.release_agents(project, AGENT_PINS, "app")
            self.assertEqual(pins["ASC_FOUNDRY_ORCHESTRATOR_AGENT_VERSION"], "8")
            self.assertEqual(pins["ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_VERSION"], "9")
            self.assertEqual(pins["ASC_FOUNDRY_RECIPE_NUTRITION_RETRIEVAL_AGENT_VERSION"], "10")
            self.assertEqual(pins["ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION"], "11")
            for call, source in zip(
                project.agents.create_version.call_args_list, sources, strict=True
            ):
                self.assertIs(call.kwargs["definition"], source.definition)
                self.assertEqual(call.kwargs["metadata"]["asc_commit"], SHA)
                self.assertEqual(call.kwargs["metadata"]["asc_source_version"], source.version)
                for key, value in source.metadata.items():
                    self.assertEqual(call.kwargs["metadata"][key], value)

    def test_deploy_supports_app_only_and_strict_agent_release_without_fallback(self) -> None:
        class SyntheticAzureError(Exception):
            pass

        app = {
            "defaultHostName": "synthetic.azurewebsites.net",
            "identity": {
                "userAssignedIdentities": {
                    "identity": {"clientId": "workload-client", "principalId": "app-principal"}
                }
            },
        }
        settings = {
            "AZURE_CLIENT_ID": "workload-client",
            "ASC_FOUNDRY_PROJECT_ENDPOINT": "https://synthetic.services.ai.azure.com/api/projects/dev",
            "ASC_FOUNDRY_PROJECT_RESOURCE_ID": "/synthetic/project",
            "ASC_FOUNDRY_IQ_SEARCH_ENDPOINT": "https://synthetic.search.windows.net",
            "ASC_FOUNDRY_IQ_INDEX_NAME": "recipes",
            "ASC_FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME": "recipes-source",
            "ASC_FOUNDRY_IQ_KNOWLEDGE_BASE_NAME": "recipes-knowledge",
            "ASC_FOUNDRY_IQ_CONNECTION_NAME": "recipes-mcp",
            "SCM_DO_BUILD_DURING_DEPLOYMENT": "true",
            "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET": "private-token",
        }
        recipe_failures = {
            f"missing_{name}": {name: ""} for name in settings if name.startswith("ASC_FOUNDRY_")
        }
        recipe_failures["blank_recipe_setting"] = {"ASC_FOUNDRY_PROJECT_RESOURCE_ID": " "}
        failures: dict[str, list[object]] = {
            "build_error": [delivery.DeliveryError("synthetic build error")],
            "build_pending": [{"status": 1}],
            "build_failed": [{"status": 3}],
            "build_response_missing": [{"id": "synthetic-deployment"}],
            "build_non_object": [None],
            "settings_error": [
                {"status": 4},
                delivery.DeliveryError("synthetic settings error"),
            ],
            "restart_error": [
                {"status": 4},
                {},
                delivery.DeliveryError("synthetic restart error"),
            ],
        }
        for mode in (
            "disabled",
            "enabled",
            "agent_failure",
            "no_build",
            "package_mount",
            *recipe_failures,
            *failures,
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                client = MagicMock()
                credential = MagicMock()
                telemetry = MagicMock()
                modules = {
                    "azure.ai.projects": SimpleNamespace(AIProjectClient=client),
                    "azure.core.exceptions": SimpleNamespace(AzureError=SyntheticAzureError),
                    "azure.identity": SimpleNamespace(AzureCliCredential=credential),
                    "sous_chef.telemetry": SimpleNamespace(configure_safe_telemetry=telemetry),
                }
                release_pins = {
                    "ASC_FOUNDRY_ORCHESTRATOR_AGENT_NAME": "orchestrator",
                    "ASC_FOUNDRY_ORCHESTRATOR_AGENT_VERSION": "8",
                    "ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_NAME": "recipe-nutrition",
                    "ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_VERSION": "9",
                }
                with (
                    patch.dict(
                        os.environ,
                        ENV
                        | {
                            "RUNNER_TEMP": directory,
                            "GITHUB_OUTPUT": str(Path(directory) / "output"),
                            "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary"),
                        },
                    ),
                    patch.dict(sys.modules, modules),
                    patch.object(delivery, "command", return_value=SHA),
                    patch.object(delivery, "current_dev_commit", return_value=SHA),
                    patch.object(
                        delivery,
                        "target",
                        return_value=(
                            app,
                            settings
                            | (
                                {"SCM_DO_BUILD_DURING_DEPLOYMENT": "false"}
                                if mode == "no_build"
                                else {"WEBSITE_RUN_FROM_PACKAGE": "1"}
                                if mode == "package_mount"
                                else recipe_failures.get(mode, {})
                            ),
                        ),
                    ),
                    patch.object(
                        delivery,
                        "manifest",
                        return_value={} if mode == "disabled" else ENABLED_MANIFEST["agents"],
                    ),
                    patch.object(delivery, "az", return_value={"status": 4}) as cli,
                    patch.object(
                        delivery,
                        "package",
                        side_effect=lambda sha, path: path.write_bytes(b"synthetic-zip"),
                    ) as package,
                    patch.object(
                        delivery,
                        "release_agents",
                        return_value=release_pins,
                        side_effect=SyntheticAzureError("private-token")
                        if mode == "agent_failure"
                        else None,
                    ) as release,
                ):
                    if (
                        mode
                        in {
                            "agent_failure",
                            "no_build",
                            "package_mount",
                        }
                        or mode in recipe_failures
                    ):
                        with self.assertRaises(delivery.DeliveryError) as failure:
                            delivery.deploy()
                        self.assertNotIn("private-token", str(failure.exception))
                        cli.assert_not_called()
                        package.assert_not_called()
                        if mode in recipe_failures:
                            release.assert_not_called()
                            credential.assert_not_called()
                            self.assertIn("recipe knowledge settings", str(failure.exception))
                        continue
                    if mode in failures:
                        cli.side_effect = failures[mode]
                        with self.assertRaises(delivery.DeliveryError):
                            delivery.deploy()
                        self.assertEqual(cli.call_count, len(failures[mode]))
                        self.assertEqual(cli.call_args_list[0].args[:2], ("webapp", "deploy"))
                        if len(failures[mode]) > 1:
                            self.assertEqual(
                                cli.call_args_list[1].args[:4],
                                ("webapp", "config", "appsettings", "set"),
                            )
                        if len(failures[mode]) > 2:
                            self.assertEqual(cli.call_args_list[2].args[:2], ("webapp", "restart"))
                            self.assertEqual(cli.call_args_list[2].kwargs, {"expect_json": False})
                        self.assertFalse((Path(directory) / "asc-dev.zip").exists())
                        self.assertFalse((Path(directory) / "output").exists())
                        self.assertFalse((Path(directory) / "summary").exists())
                        continue
                    delivery.deploy()
                    self.assertEqual(cli.call_count, 3)
                    deployment = cli.call_args_list[0].args
                    self.assertEqual(deployment[:2], ("webapp", "deploy"))
                    for flag, value in (
                        ("--restart", "false"),
                        ("--async", "true"),
                        ("--track-status", "false"),
                    ):
                        self.assertEqual(deployment[deployment.index(flag) + 1], value)
                    updates = cli.call_args_list[1].args
                    self.assertEqual(updates[:4], ("webapp", "config", "appsettings", "set"))
                    expected_flag = "false" if mode == "disabled" else "true"
                    self.assertIn(f"ASC_AGENTS_ENABLED={expected_flag}", updates)
                    self.assertIn("ASC_APP_WORKLOAD_PRINCIPAL_ID=app-principal", updates)
                    self.assertNotIn("private-token", " ".join(updates))
                    self.assertEqual(cli.call_args_list[2].args[:2], ("webapp", "restart"))
                    self.assertEqual(cli.call_args_list[2].kwargs, {"expect_json": False})
                    package.assert_called_once_with(SHA, Path(directory) / "asc-dev.zip")
                    self.assertFalse((Path(directory) / "asc-dev.zip").exists())
                    summary = (Path(directory) / "summary").read_text()
                    if mode == "disabled":
                        client.assert_not_called()
                        credential.assert_not_called()
                        telemetry.assert_not_called()
                        release.assert_not_called()
                        self.assertFalse(any("_AGENT_VERSION=" in value for value in updates))
                        self.assertIn("Agents disabled: app-only release", summary)
                    else:
                        client.assert_called_once()
                        release.assert_called_once()
                        for key, value in release_pins.items():
                            self.assertIn(f"{key}={value}", updates)
                        self.assertIn("Agents enabled", summary)

    def test_smoke_requires_real_commit_and_rejects_auth_redirects(self) -> None:
        app = {"defaultHostName": "synthetic.azurewebsites.net"}

        class Response(io.BytesIO):
            def __init__(self, body: dict[str, str], revision: str = SHA) -> None:
                super().__init__(json.dumps(body).encode())
                self.headers = {"X-ASC-Commit": revision}

        for mode in (
            "ok",
            "agents_enabled",
            "wrong_commit",
            "redirect",
            "unready",
            "wrong_flag",
            "missing_flag",
            "chat_failed",
        ):
            opener = MagicMock()
            opener.open.side_effect = [
                Response({"status": "ok"}, HEAD if mode == "wrong_commit" else SHA),
                Response({"status": "ready" if mode != "unready" else "unavailable"}),
                HTTPError("", 302 if mode == "redirect" else 401, "", Message(), None),
                HTTPError("", 401, "", Message(), None),
            ]
            with tempfile.TemporaryDirectory() as directory:
                with (
                    patch.dict(
                        os.environ,
                        ENV
                        | {
                            "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary"),
                        },
                    ),
                    patch.object(
                        delivery,
                        "target",
                        return_value=(
                            app,
                            {}
                            if mode == "missing_flag"
                            else {
                                "ASC_AGENTS_ENABLED": "true"
                                if mode in {"agents_enabled", "wrong_flag", "chat_failed"}
                                else "false"
                            },
                        ),
                    ),
                    patch.object(
                        delivery,
                        "manifest",
                        return_value=ENABLED_MANIFEST["agents"]
                        if mode in {"agents_enabled", "chat_failed"}
                        else {},
                    ),
                    patch.object(
                        delivery,
                        "chat_smoke",
                        new_callable=AsyncMock,
                        side_effect=delivery.DeliveryError("Live chat failed.")
                        if mode == "chat_failed"
                        else None,
                    ) as chat,
                    patch("urllib.request.build_opener", return_value=opener),
                    patch.object(
                        delivery,
                        "time",
                        SimpleNamespace(
                            monotonic=MagicMock(side_effect=[0, 0, 301]),
                            sleep=MagicMock(),
                        ),
                    ),
                ):
                    if mode in {"ok", "agents_enabled"}:
                        delivery.smoke()
                    else:
                        with self.assertRaises(delivery.DeliveryError):
                            delivery.smoke()
                    if mode in {"agents_enabled", "chat_failed"}:
                        chat.assert_awaited_once()
                    else:
                        chat.assert_not_awaited()

    def test_federation_and_grants_are_scoped_to_dev_app_and_project(self) -> None:
        compiler = os.environ.get("BICEP_CLI")
        if not compiler:
            self.fail("Set BICEP_CLI to compile the delivery infrastructure.")
        template = json.loads(
            subprocess.check_output(
                [
                    compiler,
                    "build",
                    str(ROOT / "infra/dev-delivery.bicep"),
                    "--stdout",
                ],
                text=True,
            )
        )
        resources = template["resources"]
        federation = next(r for r in resources if "federatedIdentityCredentials" in r["type"])
        self.assertIn(
            "repo:AgamMakkar21@72168349/ai-sous-chef@1409242851:environment:",
            federation["properties"]["subject"],
        )
        self.assertIn("parameters('environment')", federation["properties"]["subject"])
        self.assertEqual(template["parameters"]["environment"]["defaultValue"], "dev")
        self.assertEqual(federation["properties"]["audiences"], ["api://AzureADTokenExchange"])
        grants = [r for r in resources if r["type"] == "Microsoft.Authorization/roleAssignments"]
        self.assertEqual(len(grants), 3)
        self.assertTrue(all("scope" in grant for grant in grants))
        self.assertIn("Microsoft.Web/sites", grants[0]["scope"])
        self.assertIn("Microsoft.Web/sites", grants[1]["scope"])
        self.assertIn("parameters('functionAppName')", grants[1]["scope"])
        self.assertEqual(grants[1]["condition"], "[not(empty(parameters('functionAppName')))]")
        self.assertEqual(template["parameters"]["functionAppName"]["defaultValue"], "")
        self.assertIn("Microsoft.CognitiveServices/accounts/projects", grants[2]["scope"])
        for forbidden in (
            "b24988ac-6180-42a0-ab88-20f7382dd24c",
            "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
        ):
            self.assertNotIn(forbidden, json.dumps(template))
