"""Dev delivery orchestration. No passwords, resource provisioning or production mode."""

import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from azure.ai.projects import AIProjectClient

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("orchestrator", "recipe_nutrition")


class DeliveryError(RuntimeError):
    """Safe, actionable deployment failure without upstream response bodies."""


def command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, cwd=ROOT)
    if result.returncode:
        raise DeliveryError(f"{args[0]} {args[1]} failed; inspect the service deployment logs.")
    return result.stdout


def az(*args: str) -> Any:
    return json.loads(
        command("az", *args, "--subscription", required("AZURE_SUBSCRIPTION_ID"), "-o", "json")
    )


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise DeliveryError(f"Set a valid nonsecret {name} environment variable.")
    return value


def commit() -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeliveryError("GITHUB_SHA must be the full reviewed merge commit.")
    return sha


def current_dev_commit() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise DeliveryError("GITHUB_REPOSITORY is required.")
    ref = json.loads(command("gh", "api", f"repos/{repo}/git/ref/heads/dev"))
    sha: str = ref["object"]["sha"]
    return sha


def dev_event() -> None:
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != (
        "refs/heads/dev"
    ):
        raise DeliveryError("Deployment is allowed only for a push to refs/heads/dev.")


def reviewed() -> None:
    dev_event()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise DeliveryError("GITHUB_REPOSITORY is required.")
    pulls = json.loads(command("gh", "api", f"repos/{repo}/commits/{commit()}/pulls"))
    for pr in pulls:
        if (
            pr["base"]["ref"] != "dev"
            or pr["base"]["repo"]["full_name"] != repo
            or not pr["merged_at"]
            or pr["merge_commit_sha"] != commit()
        ):
            continue
        reviews = json.loads(
            command(
                "gh", "api", "--paginate", "--slurp", f"repos/{repo}/pulls/{pr['number']}/reviews"
            )
        )
        latest: dict[str, dict[str, Any]] = {}
        for page in reviews:
            for review in page:
                if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                    latest[review["user"]["login"]] = review
        if any(
            login != pr["user"]["login"]
            and review["state"] == "APPROVED"
            and review["commit_id"] == pr["head"]["sha"]
            and review.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"}
            and review["user"].get("type") == "User"
            for login, review in latest.items()
        ):
            print("Reviewed dev merge verified.")
            return
    raise DeliveryError("No merged dev PR with a current peer approval matches this commit.")


def manifest() -> dict[str, dict[str, str]]:
    data = json.loads((ROOT / "deploy" / "agents.dev.json").read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(data.get("agents"), dict)
        or set(data["agents"]) != set(ROLES)
    ):
        raise DeliveryError("The reviewed dev agent manifest must contain both agent roles.")
    agents = data["agents"]
    validated = {}
    for role in ROLES:
        target = agents[role]
        if not isinstance(target, dict) or set(target) != {"name", "version"}:
            raise DeliveryError(f"Pin an explicit agent name and numeric version for {role}.")
        name, version = target["name"], target["version"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name)
            or not isinstance(version, str)
            or not re.fullmatch(r"[1-9][0-9]{0,9}", version)
        ):
            raise DeliveryError(f"Pin an explicit agent name and numeric version for {role}.")
        validated[role] = {"name": name, "version": version}
    if agents[ROLES[0]]["name"] == agents[ROLES[1]]["name"]:
        raise DeliveryError("The two agents must have different runtime names.")
    return validated


def target() -> tuple[dict[str, Any], dict[str, str]]:
    dev_event()
    group, app_name = required("ASC_RESOURCE_GROUP"), required("ASC_APP_NAME")
    app = az("webapp", "show", "-g", group, "-n", app_name)
    project_id = (
        f"/subscriptions/{required('AZURE_SUBSCRIPTION_ID')}/resourceGroups/{group}"
        "/providers/Microsoft.CognitiveServices/accounts/"
        f"{required('ASC_FOUNDRY_ACCOUNT_NAME')}/projects/{required('ASC_FOUNDRY_PROJECT_NAME')}"
    )
    project = az(
        "resource",
        "show",
        "--ids",
        project_id,
        "--api-version",
        "2025-06-01",
    )
    for resource in (app, project):
        if (
            resource.get("tags", {}).get("environment") != "dev"
            or resource.get("tags", {}).get("project") != "ai-sous-chef"
        ):
            raise DeliveryError(
                "The target must carry ai-sous-chef project and dev environment tags."
            )
    settings = {
        item["name"]: item["value"]
        for item in az("webapp", "config", "appsettings", "list", "-g", group, "-n", app_name)
    }
    endpoint = (
        f"https://{required('ASC_FOUNDRY_ACCOUNT_NAME')}.services.ai.azure.com"
        f"/api/projects/{required('ASC_FOUNDRY_PROJECT_NAME')}"
    )
    if (
        settings.get("ASC_ENVIRONMENT") != "dev"
        or settings.get("ASC_ADAPTER_MODE") != "live"
        or settings.get("ASC_FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/") != endpoint
        or not settings.get("AZURE_CLIENT_ID")
        or not app.get("identity", {}).get("userAssignedIdentities")
    ):
        raise DeliveryError("Dev app settings, Foundry target or managed identity do not match.")
    authentication = az("webapp", "auth", "show", "-g", group, "-n", app_name)
    if (
        authentication.get("platform", {}).get("enabled") is not True
        or authentication.get("globalValidation", {}).get("requireAuthentication") is not True
        or authentication.get("globalValidation", {}).get("unauthenticatedClientAction")
        != "Return401"
    ):
        raise DeliveryError(
            "App Service authentication must be enabled and reject anonymous access."
        )
    return app, settings


def release_agents(
    project: "AIProjectClient", agents: dict[str, dict[str, str]], workload_principal: str
) -> dict[str, str]:
    """Publish exact approved versions under the same agents; retain their Entra identities."""
    from sous_chef.domain.agent_identity import (
        AgentIdentity,
        AgentRole,
        IdentityError,
        verify_distinct,
    )

    identities = []
    sources = {}
    for role, pin in agents.items():
        details = project.agents.get(pin["name"])
        instance = details.instance_identity
        if instance is None or details.state != "enabled":
            raise DeliveryError("Both agents need enabled, distinct Entra identities.")
        identities.append(
            AgentIdentity(AgentRole(role), pin["name"], instance.principal_id, instance.client_id)
        )
        source = project.agents.get_version(pin["name"], pin["version"])
        if source.definition.kind != "prompt":
            raise DeliveryError("This release expects New Foundry prompt agents.")
        sources[role] = source
    try:
        verify_distinct(identities, workload_principal)
    except IdentityError:
        raise DeliveryError(
            "Agent identities must be distinct from each other and the app."
        ) from None
    settings = {}
    for role, pin in agents.items():
        source = sources[role]
        version = project.agents.create_version(
            agent_name=pin["name"],
            definition=source.definition,
            metadata={"asc_commit": commit(), "asc_source_version": pin["version"]},
        )
        if not re.fullmatch(r"[1-9][0-9]{0,9}", version.version):
            raise DeliveryError("Foundry returned an invalid release version.")
        settings[f"ASC_FOUNDRY_{role.upper()}_AGENT_NAME"] = pin["name"]
        settings[f"ASC_FOUNDRY_{role.upper()}_AGENT_VERSION"] = version.version
    for identity in identities:
        settings[f"ASC_FOUNDRY_{identity.role.value.upper()}_AGENT_PRINCIPAL_ID"] = (
            identity.principal_id
        )
    return settings


def package(sha: str, output: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=zip", sha, "backend", "requirements.txt"],
        check=True,
        capture_output=True,
        cwd=ROOT,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(output, "w") as dest:
        for entry in source.infolist():
            if entry.filename != "backend/src/sous_chef/release.json":
                dest.writestr(entry, source.read(entry.filename))
        dest.writestr("backend/src/sous_chef/release.json", json.dumps({"commit": sha}))


def deploy() -> None:
    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import AzureError
    from azure.identity import AzureCliCredential
    from sous_chef.telemetry import configure_safe_telemetry

    configure_safe_telemetry()
    sha = commit()
    if command("git", "rev-parse", "HEAD").strip() != sha:
        raise DeliveryError("Checkout does not match the reviewed merge commit.")
    if current_dev_commit() != sha:
        raise DeliveryError("This run is superseded by a newer dev commit; no deployment started.")
    app, settings = target()
    agents = manifest()
    identity = app["identity"]["userAssignedIdentities"]
    app_principals = [
        value["principalId"]
        for value in identity.values()
        if value["clientId"] == settings["AZURE_CLIENT_ID"]
    ]
    if len(app_principals) != 1:
        raise DeliveryError("App Service must use its configured user-assigned identity.")
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
            pins = release_agents(project, agents, app_principals[0])
    except AzureError as exc:
        raise DeliveryError(f"Foundry agent release failed: {type(exc).__name__}.") from None
    pins["ASC_APP_WORKLOAD_PRINCIPAL_ID"] = app_principals[0]
    pins["ASC_READINESS_TIMEOUT_SECONDS"] = "10"
    # Persist only new pins: never replace or print existing authentication/Key Vault settings.
    az(
        "webapp",
        "config",
        "appsettings",
        "set",
        "-g",
        required("ASC_RESOURCE_GROUP"),
        "-n",
        required("ASC_APP_NAME"),
        "--settings",
        *(f"{name}={value}" for name, value in pins.items()),
    )
    archive = Path(os.environ["RUNNER_TEMP"]) / "asc-dev.zip"
    package(sha, archive)
    try:
        az(
            "webapp",
            "deploy",
            "-g",
            required("ASC_RESOURCE_GROUP"),
            "-n",
            required("ASC_APP_NAME"),
            "--src-path",
            str(archive),
            "--type",
            "zip",
            "--clean",
            "true",
            "--restart",
            "true",
            "--track-status",
            "true",
        )
    finally:
        archive.unlink(missing_ok=True)
    url = f"https://{app['defaultHostName']}"
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"url={url}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write(f"Deployed `{sha}` to [dev]({url}). Smoke checks must pass below.\n")
        for name, value in pins.items():
            if name.endswith("_AGENT_VERSION"):
                summary.write(f"- `{name}`: `{value}`\n")


def smoke() -> None:
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    app, _ = target()
    origin = f"https://{app['defaultHostName']}"
    opener = build_opener(NoRedirect())
    deadline = time.monotonic() + 300
    last_failure = "health"
    while time.monotonic() < deadline:
        try:
            for path, expected in (
                ("/health", {"status": "ok"}),
                ("/ready", {"status": "ready"}),
            ):
                last_failure = path
                with opener.open(origin + path, timeout=15) as response:
                    if (
                        json.load(response) != expected
                        or response.headers.get("X-ASC-Commit") != commit()
                    ):
                        raise DeliveryError(f"{path} did not report the expected deployed commit.")
            for path in ("/auth/session", "/api/v1/deployment-smoke"):
                last_failure = path
                try:
                    opener.open(Request(origin + path), timeout=15).close()
                except HTTPError as exc:
                    if exc.code != 401:
                        raise DeliveryError(
                            "Anonymous access must return 401, not redirect."
                        ) from None
                else:
                    raise DeliveryError("Anonymous access unexpectedly succeeded.")
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
                summary.write(
                    "Health, commit, anonymous-auth rejection and dependency checks passed.\n"
                )
            print("Dev deployment smoke checks passed.")
            return
        except (HTTPError, URLError, TimeoutError, DeliveryError):
            time.sleep(10)
    raise DeliveryError(f"Dev smoke failed at {last_failure} after five minutes; inspect dev logs.")


def main() -> int:
    try:
        action = sys.argv[1] if len(sys.argv) == 2 else ""
        if action not in {"reviewed", "deploy", "smoke"}:
            raise DeliveryError("Choose reviewed, deploy or smoke.")
        {"reviewed": reviewed, "deploy": deploy, "smoke": smoke}[action]()
    except (DeliveryError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        message = str(exc) if isinstance(exc, DeliveryError) else type(exc).__name__
        print(f"Dev delivery failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
