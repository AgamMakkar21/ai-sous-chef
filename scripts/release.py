"""Reviewed promotion and explicit rollback. Never provisions resources or changes data."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

from scripts import deploy_dev as delivery

ROOT = delivery.ROOT
REPOSITORY = "AgamMakkar21/ai-sous-chef"
OWNER = "AgamMakkar21"
CHECKS = {
    f"CI / {name}" for name in ("lint", "types", "tests", "build", "infrastructure", "frontend")
}
TARGET_KEYS = {
    "subscription_id": "AZURE_SUBSCRIPTION_ID",
    "resource_group": "ASC_RESOURCE_GROUP",
    "app_name": "ASC_APP_NAME",
    "function_app_name": "ASC_FUNCTION_APP_NAME",
    "foundry_account_name": "ASC_FOUNDRY_ACCOUNT_NAME",
    "foundry_project_name": "ASC_FOUNDRY_PROJECT_NAME",
}
GUID = r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
FILES = {"app.zip", "worker.zip", "runtime.json"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise delivery.DeliveryError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), f"{path.name} must contain a JSON object.")
    return cast(dict[str, Any], data)


def github(path: str) -> Any:
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY, "Use the approved repository.")
    return json.loads(delivery.command("gh", "api", f"repos/{REPOSITORY}/{path}"))


def number(value: Any) -> int:
    require(type(value) is int and value > 0, "Evidence must identify a positive workflow run ID.")
    return int(value)


def sha(value: Any, length: int = 40) -> str:
    require(
        isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None,
        "Use an exact source commit or SHA-256 digest, never a branch or latest.",
    )
    return str(value)


def compatibility(data: dict[str, Any]) -> None:
    require(
        set(data) == {"schema_version", "data_contract", "rollback_safe"}
        and type(data["schema_version"]) is int
        and data["schema_version"] == 1
        and type(data["data_contract"]) is int
        and data["data_contract"] > 0
        and data["rollback_safe"] is True,
        "Release needs an explicit rollback-safe data contract; migrations are not automated.",
    )


def rollback_compatible(previous: dict[str, Any], current: dict[str, Any]) -> None:
    compatibility(previous)
    compatibility(current)
    require(
        previous["data_contract"] == current["data_contract"],
        "Cross-contract rollback needs a separately reviewed migration/compatibility procedure.",
    )


def merged_pr(commit: str, base: str) -> dict[str, Any]:
    for pr in github(f"commits/{sha(commit)}/pulls"):
        if (
            pr["base"]["ref"] == base
            and pr["base"]["repo"]["full_name"] == REPOSITORY
            and pr["merged_at"]
            and pr["merge_commit_sha"] == commit
            and (base != "main" or promotion_source(pr))
            and delivery.peer_approved(pr)
        ):
            return cast(dict[str, Any], pr)
    raise delivery.DeliveryError(f"No current peer-approved merged PR matches {base}/{commit}.")


def promotion_source(pr: dict[str, Any]) -> bool:
    return bool(
        pr["base"]["ref"] == "main"
        and pr["base"]["repo"]["full_name"] == REPOSITORY
        and pr["head"]["ref"] == "dev"
        and pr["head"]["repo"]["full_name"] == REPOSITORY
    )


def promotion_evidence(pr: dict[str, Any]) -> dict[str, Any]:
    require(promotion_source(pr), "Only this repository's dev branch may be promoted to main.")
    match = re.search(r"<!-- asc60-release\s+(\{.*?\})\s+-->", pr.get("body") or "", re.DOTALL)
    if match is None:
        raise delivery.DeliveryError(
            "Use the promotion PR template's asc60-release evidence block."
        )
    data = json.loads(match[1])
    require(
        isinstance(data, dict)
        and set(data)
        == {
            "source_commit",
            "dev_run_id",
            "rollback_run_id",
            "bundle_sha256",
            "config_sha256",
            "evaluation_url",
        },
        "Promotion evidence must identify source, dev/rollback runs, config and evaluation.",
    )
    require(sha(data["source_commit"]) == pr["head"]["sha"], "Evidence is stale after a dev push.")
    number(data["dev_run_id"])
    number(data["rollback_run_id"])
    sha(data["config_sha256"], 64)
    sha(data["bundle_sha256"], 64)
    require(
        isinstance(data["evaluation_url"], str)
        and re.fullmatch(
            rf"https://github\.com/{REPOSITORY}/(?:actions/runs/[1-9][0-9]*"
            r"|issues/[1-9][0-9]*#issuecomment-[1-9][0-9]*)",
            data["evaluation_url"],
        )
        is not None,
        "Link exact repository evaluation evidence; readiness remains ASC-63's owner gate.",
    )
    return cast(dict[str, Any], data)


def check_pr() -> None:
    event = read_json(Path(os.environ["GITHUB_EVENT_PATH"]))
    pr = event.get("pull_request")
    if pr is not None and pr["base"]["ref"] == "main":
        require(promotion_source(pr), "Only this repository's dev branch may reach main.")
        if not (ROOT / "deploy" / "production.json").exists():
            require(
                "<!-- asc60-bootstrap -->" in (pr.get("body") or ""),
                "A config-free plumbing bootstrap needs explicit asc60-bootstrap acknowledgement.",
            )
            print("Reviewed dev-to-main plumbing bootstrap only; production stays disabled.")
            return
        evidence = promotion_evidence(pr)
        require(
            digest(ROOT / "deploy" / "production.json") == evidence["config_sha256"],
            "The promotion's production configuration digest does not match the reviewed file.",
        )
        production_config(ROOT / "deploy" / "production.json")
        print("dev-to-main source and release evidence verified.")


def run_proof(run_id: int, workflow: str, commit: str | None = None) -> dict[str, Any]:
    run = github(f"actions/runs/{number(run_id)}")
    require(
        run["status"] == "completed"
        and run["conclusion"] == "success"
        and run["path"].split("@")[0] == f".github/workflows/{workflow}"
        and (commit is None or run["head_sha"] == commit),
        "Workflow evidence is unsuccessful, from the wrong workflow, or for a different source.",
    )
    return cast(dict[str, Any], run)


def check_dev_ci() -> tuple[dict[str, Any], dict[str, Any]]:
    delivery.dev_event()
    commit = delivery.commit()
    pr = merged_pr(commit, "dev")
    runs = github(
        f"actions/workflows/ci.yml/runs?head_sha={sha(pr['head']['sha'])}"
        "&event=pull_request&per_page=1"
    )["workflow_runs"]
    require(bool(runs), "No PR quality run is recorded for the dev PR's exact reviewed head.")
    ci = run_proof(number(runs[0]["id"]), "ci.yml", pr["head"]["sha"])
    jobs = github(f"actions/runs/{ci['id']}/jobs?per_page=100")["jobs"]
    require(
        CHECKS <= {job["name"] for job in jobs if job["conclusion"] == "success"},
        "All six PR checks must pass before publishing a promotable dev artifact.",
    )
    return ci, pr


def seal() -> None:
    delivery.dev_event()
    commit = delivery.commit()
    require(
        delivery.command("git", "rev-parse", "HEAD").strip() == commit
        and delivery.current_dev_commit() == commit,
        "Seal only the current, reviewed dev checkout after all deployment smoke checks.",
    )
    ci, pr = check_dev_ci()
    directory = Path(os.environ["ASC_RELEASE_DIRECTORY"])
    policy = read_json(ROOT / "deploy" / "compatibility.json")
    compatibility(policy)
    bundle = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "commit": commit,
        "tree": delivery.command("git", "rev-parse", f"{commit}^{{tree}}").strip(),
        "dev_run_id": number(int(os.environ["GITHUB_RUN_ID"])),
        "dev_run_attempt": number(int(os.environ["GITHUB_RUN_ATTEMPT"])),
        "ci_run_id": ci["id"],
        "ci_head_sha": pr["head"]["sha"],
        "dev_target": {key: delivery.required(variable) for key, variable in TARGET_KEYS.items()},
        "compatibility": policy,
        "files": {name: digest(directory / name) for name in sorted(FILES)},
    }
    (directory / "bundle.json").write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    read_bundle(directory)
    print(f"Sealed tested source archives for {commit}; no runtime binary reproducibility claim.")


def read_bundle(directory: Path) -> dict[str, Any]:
    bundle = read_json(directory / "bundle.json")
    require(
        set(bundle)
        == {
            "schema_version",
            "repository",
            "commit",
            "tree",
            "dev_run_id",
            "dev_run_attempt",
            "ci_run_id",
            "ci_head_sha",
            "dev_target",
            "compatibility",
            "files",
        }
        and type(bundle["schema_version"]) is int
        and bundle["schema_version"] == 1
        and bundle["repository"] == REPOSITORY
        and isinstance(bundle["files"], dict)
        and set(bundle["files"]) == FILES
        and isinstance(bundle["dev_target"], dict)
        and set(bundle["dev_target"]) == set(TARGET_KEYS),
        "Invalid release bundle contract.",
    )
    for field in ("commit", "tree", "ci_head_sha"):
        sha(bundle[field])
    for field in ("dev_run_id", "dev_run_attempt", "ci_run_id"):
        number(bundle[field])
    compatibility(bundle["compatibility"])
    for name in FILES:
        require(
            digest(directory / name) == sha(bundle["files"][name], 64), "Artifact hash mismatch."
        )
    for name, marker in (
        ("app.zip", "backend/src/sous_chef/release.json"),
        ("worker.zip", "release.json"),
    ):
        with zipfile.ZipFile(directory / name) as archive:
            names = archive.namelist()
            require(len(names) == len(set(names)), "Duplicate archive members are not accepted.")
            for member in names:
                path = PurePosixPath(member)
                require(
                    not path.is_absolute()
                    and ".." not in path.parts
                    and "\\" not in member
                    and not any(part.startswith((".env", ".git")) for part in path.parts)
                    and (
                        name != "app.zip"
                        or member == "requirements.txt"
                        or member.startswith("backend/")
                    ),
                    "Unsafe or unexpected release archive member.",
                )
            require(
                json.loads(archive.read(marker)) == {"commit": bundle["commit"]},
                "The archive does not contain its exact tested source marker.",
            )
    validate_runtime(read_json(directory / "runtime.json"))
    return bundle


def validate_runtime(pins: dict[str, Any]) -> None:
    common = {
        "ASC_AGENTS_ENABLED",
        "ASC_APP_WORKLOAD_PRINCIPAL_ID",
        "ASC_READINESS_TIMEOUT_SECONDS",
    }
    agent_keys = {
        f"ASC_FOUNDRY_{role.upper()}_AGENT_{field}"
        for role in delivery.ROLES
        for field in ("NAME", "VERSION", "PRINCIPAL_ID")
    }
    mode_key = "ASC_FOUNDRY_RECIPE_NUTRITION_MODE"
    phase_keys = {
        "ASC_FOUNDRY_RECIPE_NUTRITION_RETRIEVAL_AGENT_VERSION",
        "ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION",
    }
    mode = pins.get(mode_key)
    if mode == "combined":
        agent_keys.add(mode_key)
        if "ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION" in pins:
            agent_keys.add("ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION")
    elif mode == "phased":
        agent_keys |= {mode_key, *phase_keys}
    elif mode is not None:
        raise delivery.DeliveryError("Runtime recipe mode must be phased or combined.")
    require(
        pins.get("ASC_AGENTS_ENABLED") in {"true", "false"}
        and set(pins) == (common | agent_keys if pins["ASC_AGENTS_ENABLED"] == "true" else common)
        and pins.get("ASC_READINESS_TIMEOUT_SECONDS") == "10"
        and re.fullmatch(GUID, pins.get("ASC_APP_WORKLOAD_PRINCIPAL_ID", "")) is not None,
        "Runtime release records may contain only explicit feature and workload/agent pins.",
    )
    identities = [pins["ASC_APP_WORKLOAD_PRINCIPAL_ID"].lower()]
    names = []
    if pins["ASC_AGENTS_ENABLED"] == "true":
        if mode == "phased" or "ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION" in pins:
            selected_phase_keys = (
                phase_keys
                if mode == "phased"
                else {"ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION"}
            )
            versions = [pins.get(field, "") for field in selected_phase_keys]
            versions.append(pins.get("ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_VERSION", ""))
            require(
                all(
                    isinstance(version, str) and re.fullmatch(r"[1-9][0-9]{0,9}", version)
                    for version in versions
                )
                and len(set(versions)) == len(versions),
                "Runtime specialist phase versions must be explicit and distinct.",
            )
        for role in delivery.ROLES:
            prefix = f"ASC_FOUNDRY_{role.upper()}_AGENT_"
            require(
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", pins[prefix + "NAME"]) is not None
                and re.fullmatch(r"[1-9][0-9]{0,9}", pins[prefix + "VERSION"]) is not None
                and re.fullmatch(GUID, pins[prefix + "PRINCIPAL_ID"]) is not None,
                "Pin exact agent versions and distinct Entra principal IDs.",
            )
            identities.append(pins[prefix + "PRINCIPAL_ID"].lower())
            names.append(pins[prefix + "NAME"])
    require(
        len(identities) == len(set(identities)) and len(names) == len(set(names)),
        "Workload and agent identities/names must remain distinct.",
    )


def production_config(path: Path) -> dict[str, Any]:
    config = read_json(path)
    require(
        set(config) == {"schema_version", "environment", "agents"} | set(TARGET_KEYS)
        and type(config["schema_version"]) is int
        and config["schema_version"] == 1
        and config["environment"] == "prod"
        and isinstance(config["agents"], dict)
        and (not config["agents"] or set(config["agents"]) == set(delivery.ROLES)),
        "Use the explicit, separately parameterized production configuration.",
    )
    for key in TARGET_KEYS:
        require(
            isinstance(config[key], str)
            and re.fullmatch(
                GUID if key == "subscription_id" else r"[A-Za-z0-9][A-Za-z0-9._-]{0,89}",
                config[key],
            )
            is not None,
            f"Set an owner-approved production {key}; examples are not deployable.",
        )
    require(
        config["app_name"] != config["function_app_name"], "Web and Function targets must differ."
    )
    for agent in config["agents"].values():
        require(
            isinstance(agent, dict)
            and set(agent) == {"name", "version", "principal_id", "definition_sha256"}
            and isinstance(agent["name"], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", agent["name"]) is not None
            and isinstance(agent["version"], str)
            and re.fullmatch(r"[1-9][0-9]{0,9}", agent["version"]) is not None
            and isinstance(agent["principal_id"], str)
            and re.fullmatch(GUID, agent["principal_id"]) is not None,
            "Production agents require exact existing versions, identities and definition hashes.",
        )
        sha(agent["definition_sha256"], 64)
    return config


def production_gates(config_hash: str, source: str | None, bundle_hash: str = "") -> None:
    rules = github("rules/branches/main")
    reviews = [rule["parameters"] for rule in rules if rule["type"] == "pull_request"]
    checks = [rule["parameters"] for rule in rules if rule["type"] == "required_status_checks"]
    require(
        {"deletion", "non_fast_forward"} <= {rule["type"] for rule in rules}
        and any(
            rule["required_approving_review_count"] >= 1
            and rule["dismiss_stale_reviews_on_push"]
            and rule["require_last_push_approval"]
            for rule in reviews
        )
        and any(
            CHECKS
            <= {
                check["context"]
                for check in rule["required_status_checks"]
                if check.get("integration_id") == 15368
            }
            and rule["strict_required_status_checks_policy"]
            for rule in checks
        ),
        "Owner must enforce main peer review, stale-review dismissal "
        "and all six GitHub Actions checks.",
    )
    environment = github("environments/prod")
    require(
        environment.get("deployment_branch_policy")
        == {"protected_branches": False, "custom_branch_policies": True}
        and any(
            rule["type"] == "required_reviewers"
            and rule.get("prevent_self_review") is True
            and len(rule.get("reviewers", [])) == 1
            and any(
                reviewer["type"] == "User" and reviewer["reviewer"]["login"] == OWNER
                for reviewer in rule.get("reviewers", [])
            )
            for rule in environment["protection_rules"]
        ),
        "Production requires the owner-only approval gate with self-review disabled; "
        "no manual substitute.",
    )
    policies = github("environments/prod/deployment-branch-policies")["branch_policies"]
    require(
        len(policies) == 1 and policies[0]["name"] == "main" and policies[0]["type"] == "branch",
        "The production environment must allow only the main branch, never tags or PR branches.",
    )
    comment_id = os.environ.get("ASC_OWNER_APPROVAL_COMMENT_ID", "")
    require(
        re.fullmatch(r"[1-9][0-9]*", comment_id) is not None,
        "Record owner environment/cost approval.",
    )
    comment = github(f"issues/comments/{comment_id}")
    require(
        comment["user"]["login"] == OWNER
        and comment["user"]["type"] == "User"
        and f"ASC-60: approve production environment and costs; config-sha256={config_hash}"
        in comment["body"].splitlines()
        and (
            source is None
            or f"ASC-63: release readiness accepted; source={source}; bundle-sha256={bundle_hash}"
            in comment["body"].splitlines()
        ),
        "Owner approval must bind the exact production config/costs and ASC-63 release source.",
    )


def download(run_id: int, artifact: str, directory: Path) -> None:
    require(not directory.exists(), "Use a fresh release download directory.")
    delivery.command(
        "gh",
        "run",
        "download",
        str(number(run_id)),
        "--repo",
        REPOSITORY,
        "--name",
        artifact,
        "--dir",
        str(directory),
    )


def verified_dev_bundle(run_id: int, directory: Path, commit: str | None = None) -> dict[str, Any]:
    run = run_proof(run_id, "deploy-dev.yml", commit)
    require(run["head_branch"] == "dev" and run["event"] == "push", "Use a dev push deployment.")
    attempt = number(run["run_attempt"])
    download(run_id, f"asc-release-{sha(run['head_sha'])}-{attempt}", directory)
    bundle = read_bundle(directory)
    require(
        bundle["commit"] == run["head_sha"]
        and bundle["dev_run_id"] == run_id
        and bundle["dev_run_attempt"] == attempt,
        "Bundle provenance does not match the successful dev run.",
    )
    pr = merged_pr(bundle["commit"], "dev")
    require(pr["head"]["sha"] == bundle["ci_head_sha"], "CI evidence refers to another source.")
    run_proof(bundle["ci_run_id"], "ci.yml", bundle["ci_head_sha"])
    return bundle


def evaluation_evidence(url: str, source: str) -> None:
    if "/actions/runs/" in url:
        run = github(f"actions/runs/{url.rsplit('/', 1)[1]}")
        require(
            run["conclusion"] == "success" and run["head_sha"] == source,
            "Evaluation run must pass for the promoted dev commit.",
        )
    else:
        comment = github(f"issues/comments/{url.rsplit('-', 1)[1]}")
        require(
            source in comment["body"]
            and comment["user"]["type"] == "User"
            and comment["author_association"] in {"OWNER", "MEMBER", "COLLABORATOR"},
            "Evaluation evidence must name the source and be recorded by a human collaborator.",
        )


def rehearsal_evidence(run_id: int, source: str, bundle_hash: str) -> None:
    run = run_proof(run_id, "rehearse-rollback.yml", source)
    require(
        run["head_branch"] == "dev" and run["event"] == "workflow_dispatch",
        "Record a successful dev rollback rehearsal for this source.",
    )
    directory = Path(os.environ["RUNNER_TEMP"]) / "asc-promotion-rehearsal"
    download(run_id, f"asc-rollback-rehearsal-{number(run['run_attempt'])}", directory)
    record = read_json(directory / "asc-rehearsal.json")
    require(
        record.get("schema_version") == 1
        and record.get("source_commit") == source
        and record.get("bundle_sha256") == bundle_hash
        and record.get("previous_commit") != source
        and record.get("rollback_smoke") == record.get("restore_smoke") == "passed"
        and record.get("data_operations") == "none",
        "Rehearsal must roll back and restore this exact artifact, "
        "not another build of the source.",
    )
    sha(record["previous_commit"])
    sha(record["previous_bundle_sha256"], 64)


def candidate() -> tuple[Path, dict[str, Any], dict[str, Any]]:
    delivery.release_event("prod", True)
    require(
        github("git/ref/heads/main")["object"]["sha"] == delivery.commit(),
        "A superseded main run cannot replace a newer release.",
    )
    config_path = ROOT / "deploy" / "production.json"
    config = production_config(config_path)
    pr = merged_pr(delivery.commit(), "main")
    operation = os.environ.get("ASC_RELEASE_OPERATION", "promote")
    directory = Path(os.environ["RUNNER_TEMP"]) / "asc-release-candidate"
    require(operation in {"promote", "rollback"}, "Select promote or rollback explicitly.")
    if operation == "promote":
        evidence = promotion_evidence(pr)
        require(
            digest(config_path) == evidence["config_sha256"], "Production config evidence is stale."
        )
        production_gates(
            evidence["config_sha256"], evidence["source_commit"], evidence["bundle_sha256"]
        )
        bundle = verified_dev_bundle(evidence["dev_run_id"], directory, evidence["source_commit"])
        require(
            digest(directory / "bundle.json") == evidence["bundle_sha256"],
            "The artifact changed after release evidence was recorded.",
        )
        require(
            bundle["tree"]
            == delivery.command("git", "rev-parse", f"{delivery.commit()}^{{tree}}").strip(),
            "Main differs from the exact tested dev tree; rebuild/retest instead of promoting.",
        )
        rehearsal_evidence(evidence["rollback_run_id"], bundle["commit"], evidence["bundle_sha256"])
        evaluation_evidence(evidence["evaluation_url"], bundle["commit"])
        dev = bundle["dev_target"]
        require(
            (config["subscription_id"].lower(), config["resource_group"].lower())
            != (dev["subscription_id"].lower(), dev["resource_group"].lower()),
            "Production must not reuse the dev subscription/resource-group target.",
        )
    else:
        production_gates(digest(config_path), None)
        previous_id = number(int(os.environ.get("ASC_ROLLBACK_RUN_ID", "0")))
        run = run_proof(previous_id, "promote.yml")
        require(run["head_branch"] == "main", "Rollback must use a previous production release.")
        download(previous_id, f"asc-production-release-{number(run['run_attempt'])}", directory)
        bundle = read_bundle(directory)
        record = read_json(directory / "production-record.json")
        old_config = production_config(directory / "production.json")
        require(
            record
            == {
                "source_commit": bundle["commit"],
                "config_sha256": digest(directory / "production.json"),
            }
            and all(config[key] == old_config[key] for key in TARGET_KEYS),
            "Rollback cannot change the approved production targets or use an unrelated record.",
        )
        config = old_config
    rollback_compatible(bundle["compatibility"], read_json(ROOT / "deploy" / "compatibility.json"))
    return directory, bundle, config


def agent_pins(
    app: dict[str, Any], settings: dict[str, str], agents: dict[str, Any]
) -> dict[str, str]:
    identities = app["identity"]["userAssignedIdentities"]
    principals = [
        identity["principalId"]
        for identity in identities.values()
        if identity["clientId"] == settings["AZURE_CLIENT_ID"]
    ]
    require(len(principals) == 1, "The app must use its own configured workload identity.")
    pins = {
        "ASC_AGENTS_ENABLED": "true" if agents else "false",
        "ASC_APP_WORKLOAD_PRINCIPAL_ID": principals[0],
        "ASC_READINESS_TIMEOUT_SECONDS": "10",
    }
    if agents:
        from azure.ai.projects import AIProjectClient
        from azure.core.exceptions import AzureError
        from azure.identity import AzureCliCredential
        from sous_chef.telemetry import configure_safe_telemetry

        configure_safe_telemetry()
        try:
            with (
                AzureCliCredential() as credential,
                AIProjectClient(
                    endpoint=settings["ASC_FOUNDRY_PROJECT_ENDPOINT"],
                    credential=credential,
                    api_version="v1",
                    retry_total=0,
                ) as project,
            ):
                clients = {settings["AZURE_CLIENT_ID"].lower()}
                for role, pin in agents.items():
                    agent = project.agents.get(pin["name"])
                    version = project.agents.get_version(pin["name"], pin["version"])
                    identity = agent.instance_identity
                    if identity is None or not isinstance(identity.client_id, str):
                        raise delivery.DeliveryError("A pinned agent has no Entra client identity.")
                    require(
                        agent.state == "enabled"
                        and identity.principal_id == pin["principal_id"]
                        and re.fullmatch(GUID, identity.client_id) is not None
                        and identity.client_id.lower() not in clients
                        and version.definition.kind == "prompt",
                        "A pinned agent is disabled, identity-shared/mismatched "
                        "or not a New Foundry prompt agent.",
                    )
                    clients.add(identity.client_id.lower())
                    if "definition_sha256" in pin:
                        definition = json.dumps(
                            version.definition.as_dict(), sort_keys=True, separators=(",", ":")
                        )
                        require(
                            hashlib.sha256(definition.encode()).hexdigest()
                            == pin["definition_sha256"],
                            "The production agent definition differs from its approved digest.",
                        )
                    prefix = f"ASC_FOUNDRY_{role.upper()}_AGENT_"
                    pins.update(
                        {
                            prefix + "NAME": pin["name"],
                            prefix + "VERSION": pin["version"],
                            prefix + "PRINCIPAL_ID": pin["principal_id"],
                        }
                    )
        except AzureError as exc:
            raise delivery.DeliveryError(
                f"Pinned agent validation failed: {type(exc).__name__}."
            ) from None
    validate_runtime(pins)
    return pins


def apply_bundle(directory: Path, environment: str, pins: dict[str, str]) -> None:
    bundle = read_bundle(directory)
    validate_runtime(pins)
    app, settings = delivery.target(environment, allow_dispatch=True)
    delivery.worker_target(environment, allow_dispatch=True)
    current_principals = [
        identity["principalId"]
        for identity in app["identity"]["userAssignedIdentities"].values()
        if identity["clientId"] == settings["AZURE_CLIENT_ID"]
    ]
    require(
        current_principals == [pins["ASC_APP_WORKLOAD_PRINCIPAL_ID"]]
        and settings.get("SCM_DO_BUILD_DURING_DEPLOYMENT", "").lower() == "true"
        and settings.get("WEBSITE_RUN_FROM_PACKAGE", "") in {"", "0"},
        "Workload identity or source-ZIP build configuration changed; "
        "do not restore stale settings.",
    )
    for filename, variable in (
        ("app.zip", "ASC_APP_NAME"),
        ("worker.zip", "ASC_FUNCTION_APP_NAME"),
    ):
        result = delivery.az(
            "webapp",
            "deploy",
            "-g",
            delivery.required("ASC_RESOURCE_GROUP"),
            "-n",
            delivery.required(variable),
            "--src-path",
            str(directory / filename),
            "--type",
            "zip",
            "--clean",
            "true",
            "--restart",
            "false",
            "--async",
            "true",
            "--track-status",
            "false",
        )
        require(
            isinstance(result, dict) and result.get("status") == 4,
            "Release build did not finish successfully; no successful release is recorded.",
        )
    delivery.az(
        "webapp",
        "config",
        "appsettings",
        "set",
        "-g",
        delivery.required("ASC_RESOURCE_GROUP"),
        "-n",
        delivery.required("ASC_APP_NAME"),
        "--settings",
        *(f"{name}={value}" for name, value in pins.items()),
    )
    for variable in ("ASC_APP_NAME", "ASC_FUNCTION_APP_NAME"):
        delivery.az(
            "webapp",
            "restart",
            "-g",
            delivery.required("ASC_RESOURCE_GROUP"),
            "-n",
            delivery.required(variable),
            expect_json=False,
        )
    _, deployed = delivery.target(environment, allow_dispatch=True)
    require(
        all(deployed.get(key) == value for key, value in pins.items()),
        "Runtime pins were not applied.",
    )
    delivery.smoke(
        environment,
        expected_commit=bundle["commit"],
        agents_enabled=pins["ASC_AGENTS_ENABLED"] == "true",
        allow_dispatch=True,
    )
    delivery.worker_smoke(environment, expected_commit=bundle["commit"], allow_dispatch=True)


def production(deploy: bool) -> None:
    directory, bundle, config = candidate()
    if not deploy:
        print(
            f"Promotion/rollback gates passed for source {bundle['commit']}. "
            "Environment approval is still required.",
        )
        return
    for key, variable in TARGET_KEYS.items():
        if key == "subscription_id":
            require(
                os.environ.get(variable, "").lower() == config[key].lower(),
                "Azure login subscription differs from the approved production configuration.",
            )
        os.environ[variable] = config[key]
    app, settings = delivery.target("prod", allow_dispatch=True)
    pins = agent_pins(app, settings, config["agents"])
    apply_bundle(directory, "prod", pins)
    if os.environ.get("ASC_RELEASE_OPERATION", "promote") == "promote":
        shutil.copyfile(ROOT / "deploy" / "production.json", directory / "production.json")
    (directory / "production-record.json").write_text(
        json.dumps(
            {
                "source_commit": bundle["commit"],
                "config_sha256": digest(directory / "production.json"),
            }
        ),
        encoding="utf-8",
    )
    print(f"Production source {bundle['commit']} and exact agent pins passed all smoke checks.")


def rehearse() -> None:
    delivery.release_event("dev", True)
    require(
        os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch",
        "Rehearsal is explicitly dispatched.",
    )
    require(delivery.current_dev_commit() == delivery.commit(), "Rehearse only current dev.")
    root = Path(os.environ["RUNNER_TEMP"])
    current_dir, previous_dir = root / "asc-rehearsal-current", root / "asc-rehearsal-previous"
    current = verified_dev_bundle(
        number(int(os.environ["ASC_CURRENT_RUN_ID"])), current_dir, delivery.commit()
    )
    previous = verified_dev_bundle(number(int(os.environ["ASC_PREVIOUS_RUN_ID"])), previous_dir)
    require(
        current["commit"] != previous["commit"],
        "Rehearsal must restore a different previous source.",
    )
    target = {key: delivery.required(variable) for key, variable in TARGET_KEYS.items()}
    require(
        current["dev_target"] == previous["dev_target"] == target,
        "Rehearsal cannot switch environments or resource targets.",
    )
    rollback_compatible(previous["compatibility"], current["compatibility"])

    def pins(directory: Path) -> dict[str, str]:
        recorded = read_json(directory / "runtime.json")
        agents = (
            {
                role: {
                    field: recorded[f"ASC_FOUNDRY_{role.upper()}_AGENT_{suffix}"]
                    for field, suffix in (
                        ("name", "NAME"),
                        ("version", "VERSION"),
                        ("principal_id", "PRINCIPAL_ID"),
                    )
                }
                for role in delivery.ROLES
            }
            if recorded["ASC_AGENTS_ENABLED"] == "true"
            else {}
        )
        app, settings = delivery.target("dev", allow_dispatch=True)
        actual = agent_pins(app, settings, agents)
        require(
            actual == recorded, "Previous agent/workload identities no longer match their release."
        )
        return actual

    current_pins, previous_pins = pins(current_dir), pins(previous_dir)
    try:
        apply_bundle(previous_dir, "dev", previous_pins)
    finally:
        apply_bundle(current_dir, "dev", current_pins)
    (root / "asc-rehearsal.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_commit": current["commit"],
                "previous_commit": previous["commit"],
                "bundle_sha256": digest(current_dir / "bundle.json"),
                "previous_bundle_sha256": digest(previous_dir / "bundle.json"),
                "rollback_smoke": "passed",
                "restore_smoke": "passed",
                "data_operations": "none",
            }
        ),
        encoding="utf-8",
    )
    print(
        "Previous and restored dev releases passed smoke. "
        "No data migration/write/delete was performed.",
    )


def main() -> int:
    actions = {
        "check-pr": check_pr,
        "check-dev-ci": check_dev_ci,
        "seal": seal,
        "verify": lambda: production(False),
        "deploy": lambda: production(True),
        "rehearse": rehearse,
    }
    try:
        action = sys.argv[1] if len(sys.argv) == 2 else ""
        require(
            action in actions, "Choose check-pr, check-dev-ci, seal, verify, deploy or rehearse."
        )
        actions[action]()
    except (
        delivery.DeliveryError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as exc:
        message = str(exc) if isinstance(exc, delivery.DeliveryError) else type(exc).__name__
        print(f"Release failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
