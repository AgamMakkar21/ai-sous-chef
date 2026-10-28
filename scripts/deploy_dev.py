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
        operation = list(args[:2])
        if args[0] == "az":
            for argument in args[2:]:
                if argument.startswith("-"):
                    break
                operation.append(argument)
        raise DeliveryError(
            f"{' '.join(operation)} failed; inspect deployment and startup logs. "
            "Command output is withheld because it can contain credentials."
        )
    return result.stdout


def az(*args: str, expect_json: bool = True) -> Any:
    output = command(
        "az",
        *args,
        "--subscription",
        required("AZURE_SUBSCRIPTION_ID"),
        "-o",
        "json" if expect_json else "none",
    )
    return json.loads(output) if expect_json else None


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
        or set(data) != {"schema_version", "agents_enabled", "agents"}
        or type(data.get("schema_version")) is not int
        or data.get("schema_version") != 2
        or type(data.get("agents_enabled")) is not bool
        or not isinstance(data.get("agents"), dict)
    ):
        raise DeliveryError("Use a schema-version-2 manifest with an explicit agents_enabled flag.")
    if data["agents_enabled"] is False:
        if data["agents"]:
            raise DeliveryError("Disabled agent releases must have an empty agents mapping.")
        return {}
    if set(data["agents"]) != set(ROLES):
        raise DeliveryError("Enabled agent releases must contain both agent roles.")
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
    authentication = az(
        "rest",
        "--method",
        "GET",
        "--url",
        f"https://management.azure.com{app['id']}/config/authsettingsV2/list"
        "?api-version=2024-04-01",
    ).get("properties", {})
    validate_authentication(authentication, settings, app.get("defaultHostName", ""))
    return app, settings


def validate_authentication(
    authentication: dict[str, Any], settings: dict[str, str], hostname: str
) -> None:
    if (
        authentication.get("platform", {}).get("enabled") is not True
        or authentication.get("globalValidation", {}).get("requireAuthentication") is not True
        or authentication.get("globalValidation", {}).get("unauthenticatedClientAction")
        != "Return401"
    ):
        raise DeliveryError(
            "App Service authentication must be enabled and reject anonymous access."
        )
    mode = settings.get("ASC_AUTH_MODE", "external_id")
    if mode == "external_id":
        return
    if mode != "app_service":
        raise DeliveryError("Select a supported ASC_AUTH_MODE.")
    paths = {
        "/health",
        "/ready",
        "/auth/login",
        "/auth/logout",
        "/auth/signed-out",
        "/auth/login/aad",
        "/auth/login/google",
        "/auth/login/github",
    }
    login = authentication.get("login", {})
    if (
        not re.fullmatch(r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.azurewebsites\.net", hostname)
        or settings.get("ASC_AUTH_ORIGIN", "").rstrip("/") != f"https://{hostname}"
        or any(
            settings.get(key)
            for key in ("ASC_AUTH_ISSUER", "ASC_AUTH_JWKS_URI", "ASC_AUTH_CLIENT_ID")
        )
        or authentication.get("httpSettings", {}).get("requireHttps") is not True
        or set(authentication.get("globalValidation", {}).get("excludedPaths", [])) != paths
        or login.get("allowedExternalRedirectUrls", []) != []
        or login.get("cookieExpiration", {}).get("convention") != "FixedTime"
        or login.get("cookieExpiration", {}).get("timeToExpiration") != "01:00:00"
        or login.get("tokenStore", {}).get("enabled") is not True
        or login.get("tokenStore", {}).get("tokenRefreshExtensionHours") != 0
    ):
        raise DeliveryError("App Service session policy or canonical auth origin does not match.")
    providers = authentication.get("identityProviders", {})
    expected = {
        "azureActiveDirectory": "MICROSOFT",
        "google": "GOOGLE",
        "gitHub": "GITHUB",
    }
    if {name for name, provider in providers.items() if provider.get("enabled") is True} != set(
        expected
    ):
        raise DeliveryError("Enable exactly the Microsoft, Google and GitHub sign-in providers.")
    for name, prefix in expected.items():
        registration = providers[name].get("registration", {})
        secret_name = f"{prefix}_PROVIDER_AUTHENTICATION_SECRET"
        secret = settings.get(secret_name, "")
        if (
            not registration.get("clientId")
            or registration.get("clientSecretSettingName") != secret_name
            or not secret.strip()
            or secret.lstrip().lower().startswith("@microsoft.keyvault(")
        ):
            raise DeliveryError(
                "Each login provider needs its own registration and a nonempty secret stored "
                "directly in its protected App Service setting, not a Key Vault reference."
            )
    aad = providers["azureActiveDirectory"]
    registration = aad["registration"]
    guid = r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
    tenant = settings.get("ASC_AUTH_MICROSOFT_TENANT_ID", "").lower()
    if (
        not re.fullmatch(guid, registration["clientId"])
        or not re.fullmatch(guid, tenant)
        or registration.get("openIdIssuer") != f"https://login.microsoftonline.com/{tenant}/v2.0"
        or aad.get("validation", {}).get("allowedAudiences") != [registration["clientId"]]
        or aad.get("validation", {})
        .get("defaultAuthorizationPolicy", {})
        .get("allowedApplications")
        != [registration["clientId"]]
        or settings.get("WEBSITE_AUTH_AAD_ALLOWED_TENANTS", "").lower() != tenant
        or aad.get("validation", {})
        .get("defaultAuthorizationPolicy", {})
        .get("allowedPrincipals", {})
        .get("identities")
    ):
        raise DeliveryError(
            "Microsoft sign-in requires the configured single-tenant v2 issuer, "
            "registered audience, this-application client policy, matching tenant allowlist "
            "and no user-specific allowlist."
        )


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
    sha = commit()
    if command("git", "rev-parse", "HEAD").strip() != sha:
        raise DeliveryError("Checkout does not match the reviewed merge commit.")
    if current_dev_commit() != sha:
        raise DeliveryError("This run is superseded by a newer dev commit; no deployment started.")
    agents = manifest()
    app, settings = target()
    if settings.get("SCM_DO_BUILD_DURING_DEPLOYMENT", "").lower() != "true" or settings.get(
        "WEBSITE_RUN_FROM_PACKAGE", ""
    ) not in {"", "0"}:
        raise DeliveryError(
            "Source ZIP delivery requires SCM_DO_BUILD_DURING_DEPLOYMENT=true "
            "and no WEBSITE_RUN_FROM_PACKAGE mount; ask the foundation owner to configure them."
        )
    identity = app["identity"]["userAssignedIdentities"]
    app_principals = [
        value["principalId"]
        for value in identity.values()
        if value["clientId"] == settings["AZURE_CLIENT_ID"]
    ]
    if len(app_principals) != 1:
        raise DeliveryError("App Service must use its configured user-assigned identity.")
    pins = {"ASC_AGENTS_ENABLED": "true" if agents else "false"}
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
                pins.update(release_agents(project, agents, app_principals[0]))
        except AzureError as exc:
            raise DeliveryError(f"Foundry agent release failed: {type(exc).__name__}.") from None
    pins["ASC_APP_WORKLOAD_PRINCIPAL_ID"] = app_principals[0]
    pins["ASC_READINESS_TIMEOUT_SECONDS"] = "10"
    archive = Path(os.environ["RUNNER_TEMP"]) / "asc-dev.zip"
    package(sha, archive)
    try:
        deployment = az(
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
            "false",
            "--async",
            "true",
            "--track-status",
            "false",
        )
        # Without runtime tracking, CLI polls the Kudu build even for an async upload.
        if not isinstance(deployment, dict) or deployment.get("status") != 4:
            raise DeliveryError(
                "App Service ZIP build did not report success; runtime settings were not changed."
            )
    finally:
        archive.unlink(missing_ok=True)
    # Settings updates restart the app; never apply them while Oryx is building.
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
    # Explicitly activate the new artifact even when the settings are unchanged.
    az(
        "webapp",
        "restart",
        "-g",
        required("ASC_RESOURCE_GROUP"),
        "-n",
        required("ASC_APP_NAME"),
        expect_json=False,
    )
    url = f"https://{app['defaultHostName']}"
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"url={url}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write(f"Deployed `{sha}` to [dev]({url}). Smoke checks must pass below.\n")
        summary.write(
            "Agents enabled: pinned agent versions published.\n"
            if agents
            else "Agents disabled: app-only release; AI features remain unavailable.\n"
        )
        for name, value in pins.items():
            if name.endswith("_AGENT_VERSION"):
                summary.write(f"- `{name}`: `{value}`\n")


def smoke() -> None:
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    agents = manifest()
    app, settings = target()
    if settings.get("ASC_AGENTS_ENABLED") != ("true" if agents else "false"):
        raise DeliveryError("The deployed agent feature flag does not match the reviewed manifest.")
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


def worker_target() -> dict[str, Any]:
    dev_event()
    group = required("ASC_RESOURCE_GROUP")
    worker: dict[str, Any] = az(
        "webapp", "show", "-g", group, "-n", required("ASC_FUNCTION_APP_NAME")
    )
    web = az("webapp", "show", "-g", group, "-n", required("ASC_APP_NAME"))
    for resource in (worker, web):
        if (
            resource.get("tags", {}).get("environment") != "dev"
            or resource.get("tags", {}).get("project") != "ai-sous-chef"
        ):
            raise DeliveryError("Worker and web targets must be tagged ai-sous-chef/dev.")
    if (
        worker["id"].lower() == web["id"].lower()
        or "functionapp" not in worker.get("kind", "").lower().split(",")
        or worker.get("httpsOnly") is not True
        or not re.fullmatch(
            r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.azurewebsites\.net",
            worker.get("defaultHostName", ""),
        )
    ):
        raise DeliveryError("Select a separate HTTPS Function App with an Azure hostname.")
    identities = worker.get("identity", {}).get("userAssignedIdentities", {})
    web_identities = web.get("identity", {}).get("userAssignedIdentities", {})
    if (
        worker.get("identity", {}).get("type") != "UserAssigned"
        or len(identities) != 1
        or not web_identities
        or web.get("identity", {}).get("principalId")
        or any(key.lower() in {item.lower() for item in web_identities} for key in identities)
    ):
        raise DeliveryError(
            "Worker must have one user-assigned identity, separate from the web app."
        )
    identity = next(iter(identities.values()))
    if (
        not identity.get("principalId")
        or not identity.get("clientId")
        or any(
            identity.get(field) == other.get(field)
            for other in web_identities.values()
            for field in ("principalId", "clientId")
        )
    ):
        raise DeliveryError("Worker and web managed identities must be distinct.")
    settings = {
        item["name"]: item["value"]
        for item in az(
            "webapp",
            "config",
            "appsettings",
            "list",
            "-g",
            group,
            "-n",
            required("ASC_FUNCTION_APP_NAME"),
        )
    }
    expected = {
        "ASC_ENVIRONMENT": "dev",
        "FUNCTIONS_EXTENSION_VERSION": "~4",
        "FUNCTIONS_WORKER_RUNTIME": "python",
        "AZURE_CLIENT_ID": identity["clientId"],
        "ASC_COSMOS_DATABASE": "sous-chef",
        "ASC_FEEDBACK_CONTAINER": "RecipeHistory",
        "ASC_LEASE_CONTAINER": "leases",
        "SCM_DO_BUILD_DURING_DEPLOYMENT": "true",
        "ENABLE_ORYX_BUILD": "true",
        "APPLICATIONINSIGHTS_AUTHENTICATION_STRING": (
            f"ClientId={identity['clientId']};Authorization=AAD"
        ),
    }
    for prefix in ("AzureWebJobsStorage", "FeedbackCosmos", "FeedbackLeases"):
        expected[f"{prefix}__credential"] = "managedidentity"
        expected[f"{prefix}__clientId"] = identity["clientId"]
        if settings.get(prefix):
            raise DeliveryError("Worker connections must not contain connection strings.")
    if (
        any(settings.get(key) != value for key, value in expected.items())
        or not re.fullmatch(r"[a-z0-9]{3,24}", settings.get("AzureWebJobsStorage__accountName", ""))
        or not re.fullmatch(
            r"https://[a-z0-9-]+\.documents\.azure\.com(?::443)?/?",
            settings.get("FeedbackCosmos__accountEndpoint", ""),
        )
        or settings.get("FeedbackCosmos__accountEndpoint")
        != settings.get("FeedbackLeases__accountEndpoint")
        or not settings.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
        or settings.get("WEBSITE_RUN_FROM_PACKAGE", "") not in {"", "0"}
        or settings.get("WEBSITE_CONTENTAZUREFILECONNECTIONSTRING")
    ):
        raise DeliveryError(
            "Worker runtime, keyless connections, monitoring or build settings mismatch."
        )
    config = az("webapp", "config", "show", "-g", group, "-n", required("ASC_FUNCTION_APP_NAME"))
    if (
        config.get("linuxFxVersion", "").lower() != "python|3.12"
        or config.get("alwaysOn") is not True
        or config.get("healthCheckPath") != "/api/health"
    ):
        raise DeliveryError("Worker requires Python 3.12, Always On and the host health check.")
    return worker


def worker_package(sha: str, output: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=zip", sha, "worker"],
        check=True,
        capture_output=True,
        cwd=ROOT,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(output, "w") as dest:
        for entry in source.infolist():
            relative = entry.filename.removeprefix("worker/")
            if not entry.is_dir() and relative != "release.json":
                dest.writestr(relative, source.read(entry.filename))
        dest.writestr("release.json", json.dumps({"commit": sha}))


def worker_deploy() -> None:
    sha = commit()
    if command("git", "rev-parse", "HEAD").strip() != sha or current_dev_commit() != sha:
        raise DeliveryError("Worker checkout must match the current reviewed dev commit.")
    worker_target()
    archive = Path(os.environ["RUNNER_TEMP"]) / "asc-worker-dev.zip"
    try:
        worker_package(sha, archive)
        deployment = az(
            "webapp",
            "deploy",
            "-g",
            required("ASC_RESOURCE_GROUP"),
            "-n",
            required("ASC_FUNCTION_APP_NAME"),
            "--src-path",
            str(archive),
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
        if not isinstance(deployment, dict) or deployment.get("status") != 4:
            raise DeliveryError("Worker ZIP build did not report success; inspect dev host logs.")
    finally:
        archive.unlink(missing_ok=True)
    az(
        "webapp",
        "restart",
        "-g",
        required("ASC_RESOURCE_GROUP"),
        "-n",
        required("ASC_FUNCTION_APP_NAME"),
        expect_json=False,
    )


def worker_smoke() -> None:
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPRedirectHandler, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    worker = worker_target()
    opener = build_opener(NoRedirect())
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            with opener.open(
                f"https://{worker['defaultHostName']}/api/health", timeout=15
            ) as response:
                if (
                    response.status != 200
                    or json.load(response) != {"status": "ok", "scope": "host-only"}
                    or response.headers.get("X-ASC-Commit") != commit()
                ):
                    raise DeliveryError("Worker health did not report the reviewed commit.")
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
                summary.write(
                    f"Worker `{commit()}` host health passed. "
                    "No feedback event processing or learning is implemented in this slice.\n"
                )
            print("Worker host-only smoke passed.")
            return
        except (HTTPError, URLError, TimeoutError, ValueError, DeliveryError):
            time.sleep(10)
    raise DeliveryError("Worker host health failed after five minutes; inspect dev host logs.")


def main() -> int:
    try:
        action = sys.argv[1] if len(sys.argv) == 2 else ""
        actions = {
            "reviewed": reviewed,
            "deploy": deploy,
            "smoke": smoke,
            "worker-deploy": worker_deploy,
            "worker-smoke": worker_smoke,
        }
        if action not in actions:
            raise DeliveryError("Choose reviewed, deploy, smoke, worker-deploy or worker-smoke.")
        actions[action]()
    except (DeliveryError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        message = str(exc) if isinstance(exc, DeliveryError) else type(exc).__name__
        print(f"Dev delivery failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
