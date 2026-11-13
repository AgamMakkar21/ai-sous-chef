"""Offline release acceptance. No Azure credentials, deployments or user data."""

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from test_foundation import compile_bicep

from scripts import deploy_dev as delivery
from scripts import release
from worker import function_app as host

SOURCE, TREE, HEAD, MAIN = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
PRINCIPAL = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"
PINS = {
    "ASC_AGENTS_ENABLED": "false",
    "ASC_APP_WORKLOAD_PRINCIPAL_ID": PRINCIPAL,
    "ASC_READINESS_TIMEOUT_SECONDS": "10",
}
TARGET = {
    "subscription_id": SUBSCRIPTION,
    "resource_group": "synthetic-dev",
    "app_name": "synthetic-dev-web",
    "function_app_name": "synthetic-dev-feedback",
    "foundry_account_name": "synthetic-dev-foundry",
    "foundry_project_name": "synthetic-dev-project",
}
CONFIG = {
    "schema_version": 1,
    "environment": "prod",
    "agents": {},
    **{key: value.replace("-dev", "-prod") for key, value in TARGET.items()},
}
POLICY = {"schema_version": 1, "data_contract": 1, "rollback_safe": True}
ENV = {
    "GITHUB_REPOSITORY": release.REPOSITORY,
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_SHA": MAIN,
    "ASC_OWNER_APPROVAL_COMMENT_ID": "12",
    "ASC_RELEASE_OPERATION": "promote",
    **{variable: TARGET[key] for key, variable in release.TARGET_KEYS.items()},
}
APP = {
    "identity": {
        "userAssignedIdentities": {"identity": {"clientId": CLIENT, "principalId": PRINCIPAL}}
    },
}
SETTINGS = {
    "AZURE_CLIENT_ID": CLIENT,
    "SCM_DO_BUILD_DURING_DEPLOYMENT": "true",
    "ASC_FOUNDRY_PROJECT_ENDPOINT": "https://synthetic.services.ai.azure.com/api/projects/prod",
    "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET": "must-not-be-restored",
    **PINS,
}


def write_bundle(directory: Path, commit: str = SOURCE) -> dict[str, Any]:
    directory.mkdir(parents=True)
    for filename, marker, content in (
        ("app.zip", "backend/src/sous_chef/release.json", "backend/src/sous_chef/app.py"),
        ("worker.zip", "release.json", "function_app.py"),
    ):
        with zipfile.ZipFile(directory / filename, "w") as archive:
            archive.writestr(marker, json.dumps({"commit": commit}))
            archive.writestr(content, f"# synthetic source {commit}\n")
            archive.writestr("requirements.txt", "# synthetic locked dependencies\n")
    (directory / "runtime.json").write_text(json.dumps(PINS))
    bundle = {
        "schema_version": 1,
        "repository": release.REPOSITORY,
        "commit": commit,
        "tree": TREE,
        "dev_run_id": 101,
        "dev_run_attempt": 1,
        "ci_run_id": 102,
        "ci_head_sha": HEAD,
        "dev_target": TARGET,
        "compatibility": POLICY,
        "files": {name: release.digest(directory / name) for name in release.FILES},
    }
    (directory / "bundle.json").write_text(json.dumps(bundle))
    return bundle


def pr(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": 123,
        "merged_at": "2026-11-02T00:00:00Z",
        "merge_commit_sha": MAIN,
        "base": {"ref": "main", "repo": {"full_name": release.REPOSITORY}},
        "head": {"ref": "dev", "sha": SOURCE, "repo": {"full_name": release.REPOSITORY}},
        "body": "<!-- asc60-release\n" + json.dumps(evidence) + "\n-->",
    }


def evidence() -> dict[str, Any]:
    return {
        "source_commit": SOURCE,
        "dev_run_id": 101,
        "rollback_run_id": 103,
        "config_sha256": "e" * 64,
        "bundle_sha256": "f" * 64,
        "evaluation_url": f"https://github.com/{release.REPOSITORY}/issues/58#issuecomment-99",
    }


class ReleaseTests(unittest.TestCase):
    def test_predeployment_gate_rejects_missing_failed_or_stale_ci_evidence(self) -> None:
        for fault in ("none", "missing_run", "missing_check", "failed", "stale"):
            proposal = pr(evidence())
            proposal["head"]["sha"] = HEAD
            jobs = [{"name": check, "conclusion": "success"} for check in release.CHECKS]
            if fault == "missing_check":
                jobs.pop()
            elif fault == "failed":
                jobs[0]["conclusion"] = "failure"
            run = {
                "id": 102,
                "status": "completed",
                "conclusion": "success",
                "path": ".github/workflows/ci.yml",
                "head_sha": SOURCE if fault == "stale" else HEAD,
            }
            with (
                self.subTest(fault=fault),
                patch.dict(os.environ, ENV | {"GITHUB_REF": "refs/heads/dev"}),
                patch.object(release, "merged_pr", return_value=proposal),
                patch.object(
                    release,
                    "github",
                    side_effect=[
                        {"workflow_runs": [] if fault == "missing_run" else [{"id": 102}]},
                        run,
                        {"jobs": jobs},
                    ],
                ),
                patch.object(delivery, "az") as azure,
            ):
                if fault == "none":
                    self.assertEqual(release.check_dev_ci(), (run, proposal))
                else:
                    with self.assertRaises(delivery.DeliveryError):
                        release.check_dev_ci()
                azure.assert_not_called()

    def test_feature_fork_and_stale_evidence_cannot_bypass_main(self) -> None:
        original = pr(evidence())
        variants = []
        for field, value in (("ref", "feature/unsafe"), ("repo", {"full_name": "other/fork"})):
            changed = copy.deepcopy(original)
            changed["head"][field] = value
            variants.append(changed)
        changed = copy.deepcopy(original)
        changed["head"]["sha"] = HEAD
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed["body"] = ""
        variants.append(changed)
        self.assertEqual(release.promotion_evidence(original), evidence())
        for candidate in variants:
            with self.subTest(candidate=candidate), self.assertRaises(delivery.DeliveryError):
                release.promotion_evidence(candidate)
        with (
            patch.object(release, "github", return_value=variants[:2]),
            patch.object(delivery, "peer_approved", return_value=True),
            self.assertRaises(delivery.DeliveryError),
        ):
            release.merged_pr(MAIN, "main")

    def test_build_gate_rejects_bad_main_pr_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            candidate = pr(evidence())
            candidate["head"]["ref"] = "feature/direct"
            event.write_text(json.dumps({"pull_request": candidate}))
            with (
                patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}),
                patch.object(release, "github") as github,
                self.assertRaises(delivery.DeliveryError),
            ):
                release.check_pr()
            github.assert_not_called()
            candidate["base"]["ref"] = "dev"
            event.write_text(json.dumps({"pull_request": candidate}))
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}):
                release.check_pr()

    def test_bootstrap_is_same_repo_dev_only_and_never_waives_a_real_production_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy").mkdir()
            event = root / "event.json"
            proposal = pr(evidence())
            proposal["body"] = "<!-- asc60-bootstrap -->"
            event.write_text(json.dumps({"pull_request": proposal}))
            with (
                patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}),
                patch.object(release, "ROOT", root),
                patch.object(release, "github") as github,
                patch.object(delivery, "az") as azure,
            ):
                release.check_pr()
                proposal["head"]["ref"] = "feature/bypass"
                event.write_text(json.dumps({"pull_request": proposal}))
                with self.assertRaises(delivery.DeliveryError):
                    release.check_pr()
                proposal["head"]["ref"] = "dev"
                event.write_text(json.dumps({"pull_request": proposal}))
                config = root / "deploy" / "production.json"
                config.write_text(json.dumps(CONFIG))
                with self.assertRaises(delivery.DeliveryError):
                    release.check_pr()
                proposal = pr(evidence() | {"config_sha256": release.digest(config)})
                event.write_text(json.dumps({"pull_request": proposal}))
                release.check_pr()
                github.assert_not_called()
                azure.assert_not_called()

    def test_sealing_requires_all_six_quality_checks_for_the_reviewed_feature_head(self) -> None:
        for missing_check in (False, True):
            with (
                self.subTest(missing_check=missing_check),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                directory = root / "release"
                write_bundle(directory)
                (root / "deploy").mkdir()
                (root / "deploy" / "compatibility.json").write_text(json.dumps(POLICY))
                proposal = pr(evidence())
                proposal["head"]["sha"] = HEAD
                jobs = [
                    {"name": check, "conclusion": "success"} for check in sorted(release.CHECKS)
                ]
                if missing_check:
                    jobs[-1]["conclusion"] = "skipped"
                with (
                    patch.dict(
                        os.environ,
                        ENV
                        | {
                            "GITHUB_EVENT_NAME": "push",
                            "GITHUB_REF": "refs/heads/dev",
                            "GITHUB_SHA": SOURCE,
                            "GITHUB_RUN_ID": "101",
                            "GITHUB_RUN_ATTEMPT": "2",
                            "ASC_RELEASE_DIRECTORY": str(directory),
                        },
                    ),
                    patch.object(release, "ROOT", root),
                    patch.object(delivery, "command", side_effect=[SOURCE, TREE]),
                    patch.object(delivery, "current_dev_commit", return_value=SOURCE),
                    patch.object(release, "merged_pr", return_value=proposal),
                    patch.object(release, "run_proof", return_value={"id": 102}) as proof,
                    patch.object(
                        release,
                        "github",
                        side_effect=[
                            {"workflow_runs": [{"id": 102}]},
                            {"jobs": jobs},
                        ],
                    ),
                ):
                    if missing_check:
                        with self.assertRaises(delivery.DeliveryError):
                            release.seal()
                    else:
                        release.seal()
                        sealed = release.read_bundle(directory)
                        self.assertEqual(sealed["dev_run_attempt"], 2)
                        self.assertEqual(sealed["ci_head_sha"], HEAD)
                    proof.assert_called_once_with(102, "ci.yml", HEAD)

    def test_rehearsal_evidence_binds_archive_bytes_not_just_the_source(self) -> None:
        for bad in ("none", "artifact", "source", "restore", "same-source"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "proof"
                source.mkdir()
                record = {
                    "schema_version": 1,
                    "source_commit": SOURCE,
                    "previous_commit": HEAD,
                    "bundle_sha256": "f" * 64,
                    "previous_bundle_sha256": "e" * 64,
                    "rollback_smoke": "passed",
                    "restore_smoke": "passed",
                    "data_operations": "none",
                }
                if bad == "artifact":
                    record["bundle_sha256"] = "0" * 64
                elif bad == "source":
                    record["source_commit"] = HEAD
                elif bad == "restore":
                    record["restore_smoke"] = "failed"
                elif bad == "same-source":
                    record["previous_commit"] = SOURCE
                (source / "asc-rehearsal.json").write_text(json.dumps(record))
                with (
                    patch.dict(os.environ, {"RUNNER_TEMP": temporary}),
                    patch.object(
                        release,
                        "run_proof",
                        return_value={
                            "head_branch": "dev",
                            "event": "workflow_dispatch",
                            "run_attempt": 3,
                        },
                    ),
                    patch.object(
                        release,
                        "download",
                        side_effect=lambda run, name, directory, source=source: shutil.copytree(
                            source, directory
                        ),
                    ) as download,
                ):
                    if bad == "none":
                        release.rehearsal_evidence(103, SOURCE, "f" * 64)
                    else:
                        with self.assertRaises(delivery.DeliveryError):
                            release.rehearsal_evidence(103, SOURCE, "f" * 64)
                    self.assertEqual(download.call_args.args[1], "asc-rollback-rehearsal-3")

    def test_corruption_marker_traversal_and_arbitrary_runtime_settings_fail_before_writes(
        self,
    ) -> None:
        for bad in ("hash", "marker", "traversal", "runtime", "latest"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "bundle"
                bundle = write_bundle(root)
                if bad == "hash":
                    with (root / "app.zip").open("ab") as archive:
                        archive.write(b"unapproved")
                elif bad in {"marker", "traversal"}:
                    with zipfile.ZipFile(root / "app.zip", "w") as archive:
                        archive.writestr(
                            "backend/src/sous_chef/release.json",
                            json.dumps(
                                {
                                    "commit": HEAD if bad == "marker" else SOURCE,
                                }
                            ),
                        )
                        if bad == "traversal":
                            archive.writestr("backend/../../.env", "synthetic")
                    bundle["files"]["app.zip"] = release.digest(root / "app.zip")
                else:
                    pins = PINS | {"MICROSOFT_PROVIDER_AUTHENTICATION_SECRET": "forbidden"}
                    if bad == "latest":
                        pins = PINS | {
                            "ASC_AGENTS_ENABLED": "true",
                            **{
                                f"ASC_FOUNDRY_{role.upper()}_AGENT_{field}": value
                                for role in delivery.ROLES
                                for field, value in (
                                    ("NAME", role),
                                    ("VERSION", "latest"),
                                    ("PRINCIPAL_ID", CLIENT),
                                )
                            },
                        }
                    (root / "runtime.json").write_text(json.dumps(pins))
                    bundle["files"]["runtime.json"] = release.digest(root / "runtime.json")
                (root / "bundle.json").write_text(json.dumps(bundle))
                with (
                    patch.object(delivery, "az") as azure,
                    self.assertRaises(delivery.DeliveryError),
                ):
                    release.apply_bundle(root, "prod", PINS)
                azure.assert_not_called()

    def test_source_archives_build_before_pin_updates_and_both_smokes_are_required(self) -> None:
        for build_status in (4, 1, 3):
            with (
                self.subTest(build_status=build_status),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "bundle"
                write_bundle(root)
                with (
                    patch.dict(os.environ, ENV),
                    patch.object(delivery, "target", return_value=(APP, SETTINGS)),
                    patch.object(delivery, "worker_target"),
                    patch.object(
                        delivery,
                        "az",
                        side_effect=[{"status": 4}, {"status": build_status}, {}, None, None],
                    ) as azure,
                    patch.object(delivery, "smoke") as web_smoke,
                    patch.object(delivery, "worker_smoke") as worker_smoke,
                ):
                    if build_status != 4:
                        with self.assertRaises(delivery.DeliveryError):
                            release.apply_bundle(root, "prod", PINS)
                        self.assertEqual(azure.call_count, 2)
                        web_smoke.assert_not_called()
                        worker_smoke.assert_not_called()
                        continue
                    release.apply_bundle(root, "prod", PINS)
                    updates = azure.call_args_list[2].args
                    self.assertEqual(updates[:4], ("webapp", "config", "appsettings", "set"))
                    self.assertNotIn("must-not-be-restored", " ".join(updates))
                    self.assertEqual(azure.call_count, 5)
                    web_smoke.assert_called_once_with(
                        "prod",
                        expected_commit=SOURCE,
                        agents_enabled=False,
                        allow_dispatch=True,
                    )
                    worker_smoke.assert_called_once_with(
                        "prod", expected_commit=SOURCE, allow_dispatch=True
                    )

    def test_missing_protection_self_review_wrong_branch_or_fake_owner_approval_blocks_production(
        self,
    ) -> None:
        rules = [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "dismiss_stale_reviews_on_push": True,
                    "require_last_push_approval": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": check, "integration_id": 15368} for check in release.CHECKS
                    ],
                },
            },
        ]
        environment = {
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": True,
                    "reviewers": [{"type": "User", "reviewer": {"login": release.OWNER}}],
                }
            ],
        }
        policies = {"branch_policies": [{"name": "main", "type": "branch"}]}
        comment = {
            "user": {"login": release.OWNER, "type": "User"},
            "body": f"ASC-60: approve production environment and costs; config-sha256={'e' * 64}\n"
            f"ASC-63: release readiness accepted; source={SOURCE}; bundle-sha256={'f' * 64}",
        }
        for bad in (
            "none",
            "protection",
            "self-review",
            "other-reviewer",
            "branch",
            "comment",
            "config",
            "readiness",
        ):
            responses: list[Any] = copy.deepcopy([rules, environment, policies, comment])
            if bad == "protection":
                responses[0] = []
            elif bad == "self-review":
                responses[1]["protection_rules"][0]["prevent_self_review"] = False
            elif bad == "other-reviewer":
                responses[1]["protection_rules"][0]["reviewers"].append(
                    {"type": "User", "reviewer": {"login": "not-the-owner"}}
                )
            elif bad == "branch":
                responses[2]["branch_policies"].append({"name": "dev", "type": "branch"})
            elif bad == "comment":
                responses[3]["user"]["login"] = "not-the-owner"
            elif bad == "config":
                responses[3]["body"] = responses[3]["body"].replace("e" * 64, "0" * 64)
            elif bad == "readiness":
                responses[3]["body"] = responses[3]["body"].splitlines()[0]
            with (
                self.subTest(bad=bad),
                patch.dict(os.environ, ENV),
                patch.object(release, "github", side_effect=responses),
                patch.object(delivery, "az") as azure,
            ):
                if bad == "none":
                    release.production_gates("e" * 64, SOURCE, "f" * 64)
                else:
                    with self.assertRaises(delivery.DeliveryError):
                        release.production_gates("e" * 64, SOURCE, "f" * 64)
                azure.assert_not_called()

    def test_promotion_rejects_changed_artifact_or_unapproved_main_tree(self) -> None:
        for bad in ("none", "artifact", "tree"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / "asc-release-candidate"
                bundle = write_bundle(directory)
                (root / "deploy").mkdir()
                config = root / "deploy" / "production.json"
                config.write_text(json.dumps(CONFIG))
                (root / "deploy" / "compatibility.json").write_text(json.dumps(POLICY))
                recorded = evidence() | {
                    "config_sha256": release.digest(config),
                    "bundle_sha256": "0" * 64
                    if bad == "artifact"
                    else release.digest(directory / "bundle.json"),
                }
                with (
                    patch.dict(os.environ, ENV | {"RUNNER_TEMP": temporary}),
                    patch.object(release, "ROOT", root),
                    patch.object(release, "github", return_value={"object": {"sha": MAIN}}),
                    patch.object(release, "merged_pr", return_value=pr(recorded)),
                    patch.object(release, "production_gates"),
                    patch.object(release, "verified_dev_bundle", return_value=bundle),
                    patch.object(delivery, "command", return_value=HEAD if bad == "tree" else TREE),
                    patch.object(release, "rehearsal_evidence"),
                    patch.object(release, "evaluation_evidence"),
                    patch.object(delivery, "az") as azure,
                ):
                    if bad == "none":
                        self.assertEqual(release.candidate()[1]["commit"], SOURCE)
                    else:
                        with self.assertRaises(delivery.DeliveryError):
                            release.candidate()
                    azure.assert_not_called()

    def test_rehearsal_restores_both_hosts_on_smoke_failure_without_data_operations(
        self,
    ) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                current = write_bundle(root / "asc-rehearsal-current")
                previous = write_bundle(root / "asc-rehearsal-previous", HEAD)
                before = {
                    "Users": {"synthetic": {"schemaVersion": 1}},
                    "RecipeHistory": ["synthetic"],
                }
                installed = {
                    "web": SOURCE,
                    "worker": SOURCE,
                    "pins": PINS.copy(),
                    "documents": copy.deepcopy(before),
                }
                observed: list[str] = []

                def azure(
                    *args: str,
                    expect_json: bool = True,
                    state: dict[str, Any] = installed,
                ) -> Any:
                    if args[:2] == ("webapp", "deploy"):
                        web = args[args.index("-n") + 1] == TARGET["app_name"]
                        archive = Path(args[args.index("--src-path") + 1])
                        with zipfile.ZipFile(archive) as source:
                            marker = "backend/src/sous_chef/release.json" if web else "release.json"
                            state["web" if web else "worker"] = json.loads(source.read(marker))[
                                "commit"
                            ]
                        return {"status": 4}
                    if args[:4] == ("webapp", "config", "appsettings", "set"):
                        state["pins"] = dict(
                            value.split("=", 1) for value in args[args.index("--settings") + 1 :]
                        )
                        return {}
                    if args[:2] == ("webapp", "restart"):
                        return None
                    raise AssertionError("Rehearsal attempted an unexpected/data-plane operation.")

                def web_smoke(
                    environment: str,
                    *,
                    expected_commit: str,
                    agents_enabled: bool,
                    allow_dispatch: bool,
                    state: dict[str, Any] = installed,
                    observations: list[str] = observed,
                    fail_previous: bool = fail,
                ) -> None:
                    self.assertEqual(state["web"], expected_commit)
                    self.assertEqual(state["pins"], PINS)
                    observations.append(expected_commit)
                    if fail_previous and expected_commit == HEAD:
                        raise delivery.DeliveryError("Synthetic rollback smoke failure")

                def worker_smoke(
                    environment: str,
                    *,
                    expected_commit: str,
                    allow_dispatch: bool,
                    state: dict[str, Any] = installed,
                ) -> None:
                    self.assertEqual(state["worker"], expected_commit)

                with (
                    patch.dict(
                        os.environ,
                        ENV
                        | {
                            "GITHUB_EVENT_NAME": "workflow_dispatch",
                            "GITHUB_REF": "refs/heads/dev",
                            "GITHUB_SHA": SOURCE,
                            "RUNNER_TEMP": temporary,
                            "ASC_CURRENT_RUN_ID": "101",
                            "ASC_PREVIOUS_RUN_ID": "100",
                        },
                    ),
                    patch.object(delivery, "current_dev_commit", return_value=SOURCE),
                    patch.object(release, "verified_dev_bundle", side_effect=[current, previous]),
                    patch.object(delivery, "target", return_value=(APP, SETTINGS)),
                    patch.object(delivery, "worker_target"),
                    patch.object(delivery, "az", side_effect=azure),
                    patch.object(delivery, "smoke", side_effect=web_smoke),
                    patch.object(delivery, "worker_smoke", side_effect=worker_smoke),
                ):
                    if fail:
                        with self.assertRaises(delivery.DeliveryError):
                            release.rehearse()
                        self.assertFalse((root / "asc-rehearsal.json").exists())
                    else:
                        release.rehearse()
                        self.assertEqual(
                            json.loads((root / "asc-rehearsal.json").read_text())["restore_smoke"],
                            "passed",
                        )
                self.assertEqual(observed, [HEAD, SOURCE])
                self.assertEqual(
                    installed,
                    {
                        "web": SOURCE,
                        "worker": SOURCE,
                        "pins": PINS,
                        "documents": before,
                    },
                )

    def test_rollback_refuses_different_data_contract_or_production_targets(self) -> None:
        with self.assertRaises(delivery.DeliveryError):
            release.rollback_compatible(POLICY, POLICY | {"data_contract": 2})
        with self.assertRaises(delivery.DeliveryError):
            release.rollback_compatible(POLICY, POLICY | {"rollback_safe": False})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "retained"
            bundle = write_bundle(source)
            (root / "deploy").mkdir()
            (root / "deploy" / "production.json").write_text(json.dumps(CONFIG))
            (root / "deploy" / "compatibility.json").write_text(json.dumps(POLICY))
            (source / "production.json").write_text(
                json.dumps(CONFIG | {"resource_group": "other-prod"})
            )
            (source / "production-record.json").write_text(
                json.dumps(
                    {
                        "source_commit": bundle["commit"],
                        "config_sha256": release.digest(source / "production.json"),
                    }
                )
            )
            with (
                patch.dict(
                    os.environ,
                    ENV
                    | {
                        "RUNNER_TEMP": temporary,
                        "ASC_RELEASE_OPERATION": "rollback",
                        "ASC_ROLLBACK_RUN_ID": "88",
                    },
                ),
                patch.object(release, "ROOT", root),
                patch.object(release, "github", return_value={"object": {"sha": MAIN}}),
                patch.object(release, "merged_pr", return_value=pr(evidence())),
                patch.object(release, "production_gates"),
                patch.object(
                    release, "run_proof", return_value={"head_branch": "main", "run_attempt": 1}
                ),
                patch.object(
                    release,
                    "download",
                    side_effect=lambda run, name, directory: shutil.copytree(source, directory),
                ),
                patch.object(delivery, "az") as azure,
                self.assertRaises(delivery.DeliveryError),
            ):
                release.candidate()
            azure.assert_not_called()

    def test_production_rollback_restores_the_original_config_and_agent_pins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "retained"
            write_bundle(source)
            (root / "deploy").mkdir()
            current_config = root / "deploy" / "production.json"
            current_config.write_text(json.dumps(CONFIG))
            (root / "deploy" / "compatibility.json").write_text(json.dumps(POLICY))
            old_agents = {
                role: {
                    "name": "previous-" + role,
                    "version": str(index + 1),
                    "principal_id": str(index + 4) * 8 + "-4444-4444-4444-" + str(index + 4) * 12,
                    "definition_sha256": "e" * 64,
                }
                for index, role in enumerate(delivery.ROLES)
            }
            old_config = CONFIG | {"agents": old_agents}
            config_bytes = json.dumps(old_config, indent=2).encode()
            (source / "production.json").write_bytes(config_bytes)
            (source / "production-record.json").write_text(
                json.dumps(
                    {
                        "source_commit": SOURCE,
                        "config_sha256": release.digest(source / "production.json"),
                    }
                )
            )
            pins = PINS | {"ASC_AGENTS_ENABLED": "true"}
            for role, agent in old_agents.items():
                for field, suffix in (
                    ("name", "NAME"),
                    ("version", "VERSION"),
                    ("principal_id", "PRINCIPAL_ID"),
                ):
                    pins[f"ASC_FOUNDRY_{role.upper()}_AGENT_{suffix}"] = agent[field]
            with (
                patch.dict(
                    os.environ,
                    ENV
                    | {
                        "RUNNER_TEMP": temporary,
                        "ASC_RELEASE_OPERATION": "rollback",
                        "ASC_ROLLBACK_RUN_ID": "88",
                    },
                ),
                patch.object(release, "ROOT", root),
                patch.object(release, "github", return_value={"object": {"sha": MAIN}}),
                patch.object(release, "merged_pr", return_value=pr(evidence())),
                patch.object(release, "production_gates") as gates,
                patch.object(
                    release, "run_proof", return_value={"head_branch": "main", "run_attempt": 2}
                ),
                patch.object(
                    release,
                    "download",
                    side_effect=lambda run, name, directory: shutil.copytree(source, directory),
                ) as download,
                patch.object(delivery, "target", return_value=(APP, SETTINGS)),
                patch.object(release, "agent_pins", return_value=pins) as resolve_pins,
                patch.object(release, "apply_bundle") as apply,
            ):
                release.production(True)
                restored = root / "asc-release-candidate"
                gates.assert_called_once_with(release.digest(current_config), None)
                self.assertEqual(download.call_args.args[1], "asc-production-release-2")
                resolve_pins.assert_called_once_with(APP, SETTINGS, old_agents)
                apply.assert_called_once_with(restored, "prod", pins)
                self.assertEqual((restored / "production.json").read_bytes(), config_bytes)

    def test_production_agent_versions_are_verified_and_never_republished(self) -> None:
        definition = {"kind": "prompt", "model": "synthetic-reviewed-model"}
        agents = {
            role: {
                "name": role,
                "version": str(index + 1),
                "principal_id": f"{index + 4}" * 8 + "-4444-4444-4444-" + f"{index + 4}" * 12,
                "definition_sha256": hashlib.sha256(
                    json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            for index, role in enumerate(delivery.ROLES)
        }
        for bad in ("none", "identity", "definition", "shared", "shared-client", "workload-client"):
            project = MagicMock()
            project.agents.get.side_effect = lambda name, bad=bad: SimpleNamespace(
                state="enabled",
                instance_identity=SimpleNamespace(
                    principal_id=PRINCIPAL if bad == "identity" else agents[name]["principal_id"],
                    client_id=CLIENT
                    if bad == "workload-client"
                    else "66666666-6666-6666-6666-666666666666"
                    if bad == "shared-client"
                    else f"{delivery.ROLES.index(name) + 6}" * 8
                    + "-6666-6666-6666-"
                    + f"{delivery.ROLES.index(name) + 6}" * 12,
                ),
            )
            project.agents.get_version.return_value.definition = SimpleNamespace(
                kind="prompt",
                as_dict=lambda bad=bad: (
                    definition | ({"model": "changed"} if bad == "definition" else {})
                ),
            )
            factory = MagicMock()
            factory.return_value.__enter__.return_value = project
            modules = {
                "azure.ai.projects": SimpleNamespace(AIProjectClient=factory),
                "azure.identity": SimpleNamespace(AzureCliCredential=MagicMock()),
                "sous_chef.telemetry": SimpleNamespace(configure_safe_telemetry=MagicMock()),
            }
            chosen = copy.deepcopy(agents)
            if bad == "shared":
                chosen["recipe_nutrition"]["principal_id"] = chosen["orchestrator"]["principal_id"]
                project.agents.get.side_effect = lambda name, chosen=chosen: SimpleNamespace(
                    state="enabled",
                    instance_identity=SimpleNamespace(
                        principal_id=chosen[name]["principal_id"], client_id=CLIENT
                    ),
                )
            with self.subTest(bad=bad), patch.dict(sys.modules, modules):
                if bad == "none":
                    pins = release.agent_pins(APP, SETTINGS, chosen)
                    self.assertEqual(pins["ASC_FOUNDRY_ORCHESTRATOR_AGENT_VERSION"], "1")
                else:
                    with self.assertRaises(delivery.DeliveryError):
                        release.agent_pins(APP, SETTINGS, chosen)
            project.agents.create_version.assert_not_called()

    def test_workflows_keep_prs_unprivileged_production_main_only_and_rehearsal_serialized(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[2] / ".github" / "workflows"
        production = (root / "promote.yml").read_text()
        gate, deploy = production.split("\n  deploy:", 1)
        self.assertNotIn("id-token", gate)
        self.assertIn("needs: verify", deploy)
        self.assertIn("name: prod", deploy)
        self.assertIn("github.ref == 'refs/heads/main'", deploy)
        for workflow in (production, (root / "rehearse-rollback.yml").read_text()):
            self.assertNotIn("pull_request:", workflow)
            self.assertNotIn("pull_request_target:", workflow)
            self.assertNotIn("secrets.", workflow)
            self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("group: deploy-dev", (root / "rehearse-rollback.yml").read_text())
        self.assertIn("python -m scripts.release check-pr", (root / "ci.yml").read_text())


class ProductionInfrastructureTests(unittest.TestCase):
    def test_production_reuses_functions_and_gets_separate_environment_scoped_publisher(
        self,
    ) -> None:
        template = compile_bicep("infra/functions.bicep")
        self.assertEqual(template["parameters"]["environment"]["allowedValues"], ["dev", "prod"])
        module = next(
            resource for resource in template["resources"] if "feedback-host" in resource["name"]
        )
        self.assertEqual(
            module["properties"]["parameters"]["environment"]["value"],
            "[parameters('environment')]",
        )
        inner = module["properties"]["template"]
        site = next(
            resource for resource in inner["resources"] if resource["type"] == "Microsoft.Web/sites"
        )
        environment = next(
            setting
            for setting in site["properties"]["siteConfig"]["appSettings"]
            if setting["name"] == "ASC_ENVIRONMENT"
        )
        self.assertEqual(environment["value"], "[parameters('environment')]")
        publisher = compile_bicep("infra/dev-delivery.bicep")
        federation = next(
            resource
            for resource in publisher["resources"]
            if "federatedIdentityCredentials" in resource["type"]
        )
        self.assertIn("parameters('environment')", federation["properties"]["subject"])
        self.assertEqual(publisher["parameters"]["environment"]["defaultValue"], "dev")
        self.assertEqual(publisher["parameters"]["environment"]["allowedValues"], ["dev", "prod"])

    def test_host_accepts_explicit_production_without_a_local_fallback(self) -> None:
        from test_functions import SETTINGS

        with patch.dict(os.environ, SETTINGS | {"ASC_ENVIRONMENT": "prod"}, clear=True):
            host.validate_configuration()
        with patch.dict(os.environ, SETTINGS | {"ASC_ENVIRONMENT": "local"}, clear=True):
            with self.assertRaises(RuntimeError):
                host.validate_configuration()
