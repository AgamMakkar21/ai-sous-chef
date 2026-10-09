# ai-sous-chef

AI Sous Chef combines current groceries, grounded recipe generation, and
feedback-based personalization.

## Local backend

The scaffold runs a FastAPI backend with offline readiness doubles. Authentication,
Cosmos access, agents and the React UI are not implemented yet.

Use **Ubuntu 24.04 in WSL 2**, CPython **3.12**, and a checkout in the Linux
filesystem. Windows supplies the editor and browser. Do not share a virtual
environment or `node_modules` between Windows and Linux.

Run these setup commands inside Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y python3.12-venv git
mkdir -p ~/repos
cd ~/repos
git clone https://github.com/AgamMakkar21/ai-sous-chef.git
cd ai-sous-chef
git switch ana/asc-03-python-scaffold
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install --no-build-isolation --no-deps -e .
cp .env.local.example .env.local
export ASC_ENVIRONMENT=local
python -m uvicorn sous_chef.app:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

GitHub authentication is required for a private checkout; use your approved
Git credential helper, never a token embedded in the clone URL.
Binding to `0.0.0.0` supports Windows-to-WSL localhost forwarding; use only on
a trusted development machine. Stop the server with Ctrl+C.

Open <http://localhost:8000/health> in the Windows browser. It returns exactly
`{"status":"ok"}`. <http://localhost:8000/ready> returns `{"status":"ready"}` in
explicit local-double mode. Local API documentation is at
<http://localhost:8000/docs>. The root URL returns a truthful 503 until a frontend
build is installed; it is not a cooking demo.

In another Ubuntu terminal, verify the scaffold:

```bash
cd ~/repos/ai-sous-chef
source .venv/bin/activate
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest
```

Tests use offline probes and generated frontend fixtures; no Azure account,
credentials or network access are needed.

## Configuration and readiness

Set `ASC_ENVIRONMENT` in the process environment to `local`, `dev`, or `prod`.
The app reads only `.env.<environment>` in the working directory, not a generic
`.env`. Process variables override that file. Startup rejects missing or invalid
settings with field names and setup guidance, never the rejected values.

| Setting | Purpose |
| --- | --- |
| `ASC_ENVIRONMENT` | Required explicit environment selection |
| `ASC_ADAPTER_MODE` | Required `local` or `live`; local doubles are forbidden in dev/prod |
| `ASC_READINESS_TIMEOUT_SECONDS` | Positive finite readiness deadline, at most 10 seconds; default 2 |
| `ASC_STATIC_DIR` | Optional frontend build directory; default `backend/src/sous_chef/static` |
| `ASC_COSMOS_ENDPOINT` | Required in live mode; HTTPS URL without credentials or query parameters |
| `ASC_COSMOS_DATABASE` | Required nonempty database name in live mode |
| `ASC_FOUNDRY_PROJECT_ENDPOINT` | Required in live mode; HTTPS URL without credentials or query parameters |

`.env.local.example`, `.env.dev.example` and `.env.prod.example` contain no live
endpoints or secrets. Empty live values must be supplied by deployment configuration.
Actual `.env` files are ignored by Git. Do not add service keys: hosted integrations
must use managed identity, and each interactive agent must retain a distinct
Entra Agent identity when those integrations are implemented.

`GET /health` checks only the web process. `GET /ready` uses a bounded, injectable
dependency probe. An unavailable dependency or timeout returns a generic 503
`application/problem+json` response; unexpected errors return a generic 500.
Errors include an opaque `traceId`. Logs do not include exception contents,
settings, endpoints or user data. API docs are disabled outside local mode.

Live mode deliberately returns **not ready** until real adapters are implemented.
Valid configuration alone is not proof of connectivity. Later integration tickets
must supply a real `ReadinessProbe` to the app factory; they must not substitute
the local double or claim the stub validates Azure connectivity.

## Extension boundaries

`backend/src/sous_chef/app.py` composes the application. HTTP routes and safe
responses live in `api/`, dependency interfaces in `domain/`, application behavior
in `services/`, and concrete probes in `adapters/`. Future user-data endpoints use
`/api/v1` and the contracts below; no authentication bypass or placeholder CRUD
is supplied.

The same FastAPI server mounts a built frontend when its directory contains
`index.html`. Future React/TypeScript/Vite work uses Node.js 24 LTS and npm;
copy its build into `backend/src/sous_chef/static` or set `ASC_STATIC_DIR`.
Generated output is ignored by Git. Frontend implementation and client-side
routing remain separate work.

## Reproducible dependencies

`pyproject.toml` pins direct dependencies. `pip-tools` generates exact-version-pinned
runtime and development lockfiles; the dev lock is constrained by the runtime
lock. New Foundry is pinned to `azure-ai-projects==2.8.0`, with
`azure-identity==1.26.0` and `openai==3.27.0`; no classic Agents client or second
orchestration framework is included. These are stable releases, not preview pins.
The offline SDK test verifies imports and the project OpenAI-client entry point,
not live agent execution or deployment readiness.

After changing the manifest, regenerate both locks inside the Python 3.12
environment and rerun the checks:

```bash
bash scripts/lock.sh
python -m pip install -r requirements-dev.txt
python -m pip check
```

## MVP contracts

- [Architecture and implementation decisions](docs/adr/0001-mvp-contracts.md)
- [API, schemas, limits, and lifecycle rules](docs/contracts/api-and-schemas.md)
- [Detailed technical overview](docs/references/technical-overview.md)
- [Architecture diagram](docs/references/architecture-diagram.png)

[![AI Sous Chef architecture: Entra authentication, App Service, Foundry agents and tools, Cosmos, asynchronous learning, and monitoring](docs/references/architecture-diagram.png)](docs/references/architecture-diagram.png)

These are implementation contracts, not a deployed application. Preference
learning and conditional Web IQ are both MVP requirements. Work starts from
`dev` on a feature/fix branch, enters `dev` through a peer-reviewed PR, and is
promoted to `main` through a separately reviewed PR. Integration into `dev`
does not by itself complete or close the delivery issue.