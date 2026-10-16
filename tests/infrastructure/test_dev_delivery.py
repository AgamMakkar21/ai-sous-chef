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
from unittest.mock import MagicMock, patch
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


class DeliveryTests(unittest.TestCase):
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
                patch.object(
                    delivery,
                    "command",
                    side_effect=[
                        json.dumps([pr]),
                        json.dumps([[review]]),
                    ],
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
            patch.object(
                delivery,
                "command",
                side_effect=[
                    json.dumps([pr]),
                    json.dumps([[approval], [dismissed]]),
                ],
            ),
        ):
            with self.assertRaises(delivery.DeliveryError):
                delivery.reviewed()

    def test_direct_push_does_not_deploy(self) -> None:
        with patch.dict(os.environ, ENV), patch.object(delivery, "command", return_value="[]"):
            with self.assertRaises(delivery.DeliveryError):
                delivery.reviewed()

    def test_manifest_rejects_latest_shared_names_missing_roles_and_bad_shapes(self) -> None:
        original = {"schema_version": 1, "agents": delivery.manifest()}
        variants = [
            [],
            {"schema_version": 2},
            {"schema_version": 1, "agents": {}},
            {"schema_version": 1, "agents": {"orchestrator": {}, "recipe_nutrition": []}},
        ]
        for field, value in (("version", "latest"), ("name", "recipe-nutrition")):
            data = json.loads(json.dumps(original))
            data["agents"]["orchestrator"][field] = value
            variants.append(data)
        for data in variants:
            with patch.object(Path, "read_text", return_value=json.dumps(data)):
                with self.assertRaises(delivery.DeliveryError):
                    delivery.manifest()

    def test_target_checks_tags_endpoint_identity_and_easy_auth_before_writes(self) -> None:
        tags = {"environment": "dev", "project": "ai-sous-chef"}
        app = {"tags": tags, "identity": {"userAssignedIdentities": {"identity": {}}}}
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
                        authentication,
                    ],
                ) as cli,
            ):
                if bad == "none":
                    delivery.target()
                else:
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.target()
                self.assertTrue(all("set" not in call.args for call in cli.call_args_list))

    def test_archive_contains_exact_git_commit_and_no_environment_secrets(self) -> None:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "app.zip"
            delivery.package(sha, archive)
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
                self.assertIn("requirements.txt", names)
                self.assertIn("backend/src/sous_chef/app.py", names)
                self.assertEqual(
                    json.loads(package.read("backend/src/sous_chef/release.json")), {"commit": sha}
                )
                self.assertFalse(any(".env" in name or ".git/" in name for name in names))
                self.assertTrue(
                    all(name == "requirements.txt" or name.startswith("backend/") for name in names)
                )

    def test_agent_release_preserves_approved_definition_and_separate_identities(self) -> None:
        definitions = [
            SimpleNamespace(kind="prompt", instructions="approved", tools=["approved-tool"]),
            SimpleNamespace(kind="prompt", instructions="specialist", tools=["approved-tool"]),
        ]
        for mode in ("ok", "shared", "workload", "legacy", "disabled"):
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
            project.agents.get_version.side_effect = [
                SimpleNamespace(definition=definition) for definition in definitions
            ]
            project.agents.create_version.side_effect = [
                SimpleNamespace(version="8"),
                SimpleNamespace(version="9"),
            ]
            with (
                patch.dict(os.environ, ENV),
                patch("sys.path", [str(ROOT / "backend" / "src"), *sys.path]),
            ):
                if mode != "ok":
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.release_agents(project, delivery.manifest(), "app")
                    project.agents.create_version.assert_not_called()
                    continue
                pins = delivery.release_agents(project, delivery.manifest(), "app")
            self.assertEqual(pins["ASC_FOUNDRY_ORCHESTRATOR_AGENT_VERSION"], "8")
            self.assertEqual(pins["ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_VERSION"], "9")
            for call, definition in zip(
                project.agents.create_version.call_args_list, definitions, strict=True
            ):
                self.assertIs(call.kwargs["definition"], definition)
                self.assertEqual(call.kwargs["metadata"]["asc_commit"], SHA)
                self.assertEqual(call.kwargs["metadata"]["asc_source_version"], "1")

    def test_smoke_requires_real_commit_and_rejects_auth_redirects(self) -> None:
        app = {"defaultHostName": "synthetic.azurewebsites.net"}

        class Response(io.BytesIO):
            def __init__(self, body: dict[str, str], revision: str = SHA) -> None:
                super().__init__(json.dumps(body).encode())
                self.headers = {"X-ASC-Commit": revision}

        for mode in ("ok", "wrong_commit", "redirect", "unready"):
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
                    patch.object(delivery, "target", return_value=(app, {})),
                    patch("urllib.request.build_opener", return_value=opener),
                    patch("scripts.deploy_dev.time.monotonic", side_effect=[0, 0, 301]),
                    patch("scripts.deploy_dev.time.sleep"),
                ):
                    if mode == "ok":
                        delivery.smoke()
                    else:
                        with self.assertRaises(delivery.DeliveryError):
                            delivery.smoke()

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
        self.assertEqual(
            federation["properties"]["subject"],
            "repo:AgamMakkar21/ai-sous-chef:environment:dev",
        )
        self.assertEqual(federation["properties"]["audiences"], ["api://AzureADTokenExchange"])
        grants = [r for r in resources if r["type"] == "Microsoft.Authorization/roleAssignments"]
        self.assertEqual(len(grants), 2)
        self.assertTrue(all("scope" in grant for grant in grants))
        self.assertIn("Microsoft.Web/sites", grants[0]["scope"])
        self.assertIn("Microsoft.CognitiveServices/accounts/projects", grants[1]["scope"])
        for forbidden in (
            "b24988ac-6180-42a0-ab88-20f7382dd24c",
            "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
        ):
            self.assertNotIn(forbidden, json.dumps(template))
