# Consumer authentication and authorization (ASC-13 / ASC-14)

## Boundary and behavior

There are two explicit authentication modes:

| `ASC_AUTH_MODE` | Sign-in | Backend verification |
| --- | --- | --- |
| `external_id` (default, unchanged) | Entra External ID customer tenant | Signed External ID JWT, exact issuer/audience/JWKS |
| `app_service` (hosted only) | Google, GitHub, single-tenant Microsoft Entra ID | Opaque Easy Auth session validated at the configured app's `/.auth/me` endpoint |

The new `app_service` mode does **not** require creation of an External ID tenant.
Microsoft uses an authorized registration in an existing tenant with supported
account type **Accounts in this organizational directory only**
(`signInAudience=AzureADMyOrg`) and the
`https://login.microsoftonline.com/<tenant-id>/v2.0` authority. No new tenant is needed.
The project's tenant policy disallows the broader personal/multitenant Microsoft
registration. Personal Microsoft accounts and other directories are not accepted;
authorized tenant members/guests use Microsoft, and personal users can use Google
or GitHub instead. Do not change the authority to `common` to bypass that policy.
A personal Google or GitHub account can test those providers, subject to their consent/test-user rules.
Tenant and organization policies still apply; this does not bypass restrictions.
The login registrations are separate from the App Service workload identity and every Entra Agent identity.
Cosmos and Foundry continue to use managed identities; this change does not grant
data-plane access or implement onboarding.

### App Service multi-provider mode

`GET /auth/login` displays three provider links. `/auth/login/aad`,
`/auth/login/google` and `/auth/login/github` redirect to the matching Easy Auth
endpoint with a fixed return `/`; caller-supplied queries/redirects are rejected.
These three exact routes join the five public paths listed below. Unknown
provider routes stay protected; no wildcard auth exclusion is permitted.

Set `ASC_AUTH_MODE=app_service` and `ASC_AUTH_ORIGIN` to the app's canonical HTTPS
`*.azurewebsites.net` origin. Remove `ASC_AUTH_ISSUER`, `ASC_AUTH_JWKS_URI` and
`ASC_AUTH_CLIENT_ID` from runtime settings in this mode. Microsoft client ID and
issuer belong in the platform provider registration, not the old JWT settings.
Set `ASC_AUTH_MICROSOFT_TENANT_ID` to the approved directory GUID. Both infrastructure
paths also set `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` to that exact tenant for Easy Auth.
Custom domains, direct bearer-token API clients and local cookie sign-in are not
supported by this mode. Use the original mode for local signed-token testing.

For every protected request, Python sends **only** the caller's single
`AppServiceAuthSession` cookie to the fixed configured origin's `/.auth/me`.
It never derives the destination from Host/Forwarded headers, follows redirects,
forwards Authorization/identity headers, or accepts an explicit Authorization
header as a fallback. This independent round-trip adds latency and requires
outbound access to the canonical App Service endpoint; no identity cache is used.
The token store must be enabled. A five-second timeout, 64-KiB response limit,
one allowed provider identity and an unambiguous mapped `nameidentifier`/`sub`
claim are required. Raw response bodies and provider tokens are never logged or
returned. Invalid/expired sessions return 401; transport/policy-response failures
return 503. The platform validates session expiry; provider-token expiry is a
different lifetime, and the backend does not invent refresh/revocation guarantees.

Ownership uses a hash of the configured app origin, provider and stable subject.
For Microsoft, the backend additionally requires the verified `tid` (or its mapped
tenant-ID claim) and `iss` claims. The actual issuer must be exactly
`https://login.microsoftonline.com/<tid>/v2.0`, and the GUID tenant ID must equal
`ASC_AUTH_MICROSOFT_TENANT_ID`. Other tenants and personal Microsoft accounts are
rejected even if a platform session is otherwise valid. Tenant ID is included in the ownership
namespace, so equal subjects from different tenants cannot share data.
Email, display name, GitHub username and `user_id` display hints are not ownership
keys. Identical email addresses or subjects across different providers remain
**different app accounts**; there is no automatic linking. Switching authentication
mode, app hostname or provider registration requires an explicit identity
migration if existing data must remain accessible. Google/GitHub ownership IDs
are unchanged by Microsoft tenant restrictions. CSRF/Origin enforcement and
Cosmos ownership checks below apply unchanged to all three providers.

### External ID mode (existing behavior)

App Service built-in authentication (Easy Auth, `aad`) handles the authorization
code callback and secure session cookie. `authsettingsV2` requires HTTPS and returns
401 rather than redirecting API requests. The token store supplies the signed ID
token in `X-MS-TOKEN-AAD-ID-TOKEN`. The backend independently verifies its RS256
signature against the configured external tenant's JWKS, exact issuer, single
audience, subject, expiry, not-before and issued-at claims. JWT header key URLs are
not used. Keys are cached for one hour; a missing key triggers a JWKS refresh.

The backend accepts an explicit `Authorization: Bearer <signed-token>` for local
API testing too. It does not trust `X-MS-CLIENT-PRINCIPAL`, principal IDs, email,
`X-User-ID`, request-body IDs or query IDs. Local mode never accepts the platform
ID-token header. No environment has a bypass. Missing auth configuration cannot
authenticate anyone. A supplied token with unavailable configuration or
unreachable signing keys returns 503 `authentication_unavailable`, not success.

In External ID mode, all HTTP routes, including frontend assets and unknown API paths, are protected
except `/health`, `/ready`, `/auth/login`, `/auth/logout` and `/auth/signed-out`.
Local-only `/docs` and `/openapi.json` remain public for contract inspection.
WebSockets are rejected until an authenticated streaming implementation exists.
Both modes store an immutable `AuthenticatedUser` in `request.state.user`.
In External ID mode its opaque `user_id` is SHA-256 of the exact verified issuer, a NUL separator and
subject; future handlers must use it for Cosmos ownership and partition scoping,
never a supplied ID. Changing registration/issuer requires an explicit identity
migration, not matching users by email.

The platform ID-token header is not proof by itself: the contained signed token
is always verified independently. Raw client-principal headers never establish
identity, even in dev/prod. An explicitly supplied but empty/invalid Authorization
header cannot fall back to the cookie session, and duplicate credential headers
are rejected. Deploy only behind the configured Easy Auth boundary; do not expose
an alternate direct-to-worker ingress or disable authentication on product paths.

| Operation | Response / caller behavior |
| --- | --- |
| `GET /auth/login` | Hosted: 307 to `/.auth/login/aad`, fixed return `/`. Local: 503 with testing guidance. |
| `GET /auth/logout` | Hosted: 307 to `/.auth/logout`, fixed return `/auth/signed-out`; clears the CSRF cookie. Local: 503, discard bearer token and sign out on dev. |
| `GET /auth/signed-out` | Public informational JSON with a `/auth/login` link; does not automatically sign in again. |
| `GET /auth/session` | Authenticated, no-store JSON `{ "userId": "...", "csrfToken": "..." }`; no profile creation or raw claims/tokens. |
| Missing, invalid or expired token at the backend | 401 `application/problem+json`, code `unauthenticated`, `WWW-Authenticate: Bearer`, no-store and trace ID. |
| Missing or expired Easy Auth session at the platform | HTTP 401 before Python; body is platform-owned and is **not** guaranteed to follow the problem schema. |
| Invalid cookie-session mutation | 403 problem, code `csrf_failed`. |
| Signing-key/configuration outage at the backend | 503 problem, code `authentication_unavailable`; retry later, do not treat as a successful login. |

On **any 401**, the UI must stop/reject the current action, clear displayed private
state and offer a top-level navigation to `/auth/login`. Do not parse a 401 body
before deciding to reauthenticate or replay a mutation automatically. Hosted
cookies expire after one hour without a refresh grace period; earlier token expiry
also requires sign-in. Signing out ends the application's cookie session, not every
Microsoft/browser session. A copied bearer token remains usable until expiry; it
is not a revocable server session.

Cookie-authenticated mutations require the exact configured `Origin` and
`X-CSRF-Token` from `/auth/session`, matching its secure, HttpOnly, SameSite=Strict,
host-only `__Host-asc-csrf` cookie. Keep the token in memory, not local storage.
Refreshing the session rotates it; other tabs may need to fetch a new session on
403. Explicit bearer requests do not use ambient browser credentials and do not
require this cookie. Do not add permissive CORS.

## Shared backend authorization

Protected handlers declare the `CurrentUser` FastAPI dependency from
`sous_chef.api.authentication`. It returns the immutable `AuthenticatedUser` that
middleware derived from the verified identity; if middleware context is missing,
it fails closed with the same 401 problem response. Do not construct this context
from route, query, body, tool arguments, email, or client-principal headers.
The domain type lives in `sous_chef.domain.authorization`; the authentication
module re-exports it for existing callers.

Every data operation must use `user.user_id` as its Cosmos partition key and
server-selected ownership field:

```python
@router.get("/recipes/{recipe_id}")
async def read_recipe(recipe_id: str, user: CurrentUser) -> dict[str, object]:
    record = await repository.read(user, ProductContainer.RECIPE_HISTORY, recipe_id)
    owned = user.require_owned(record.body if record is not None else None)
    return {"data": to_public_recipe(owned)}
```

This illustrates the [shared repository](cosmos-access.md), not a new recipe endpoint.
`ProductContainer` comes from `sous_chef.domain.cosmos`.
Repositories normalize an absent point read to `None`; they must not normalize
database/network failures to absence. `require_owned` returns the original typed
record only when its stored `userId` matches the verified user. Missing, ownerless,
and foreign records raise `OwnedResourceNotFound`, rendered as the same no-store
404 `not_found` problem with a trace ID and no record details.

Read within the authenticated partition **before** checking the record; never
search globally to discover an owner. Apply the guard before returning private
content, reporting revisions/conflicts, or mutating/deleting a record. Updates and
deletes must also use that same partition. Creation sets `userId` on the server;
mutation schemas must reject unknown fields, including `userId`. Collection queries
must stay within the verified user's partition; checking individual returned
records does not replace partition-scoped queries. No new product API, database
adapter, or unpartitioned lookup is added by ASC-14.

Local tests use ephemeral signed tokens and a replaced JWKS fetch, not a synthetic
principal header, request-selected user, or runtime test-auth switch. The focused
two-user fixture covers reads and mutations with colliding resource IDs, foreign
IDs, forged identity hints, and wrong-owner records returned from storage. The
comprehensive per-product-route matrix remains ASC-18; each new endpoint must
still ship focused ownership tests.

## Multi-provider owner setup (existing app; no new tenant)

Use authorized accounts for each registration. The Azure subscription identity
does not automatically grant permission to create Microsoft, Google or GitHub
login applications. If an organization blocks app registration, its administrator
must create/approve that provider; do not use an unauthorized corporate directory.

1. In the existing project tenant, create a **Microsoft Entra web app registration**
   with supported account type **Accounts in this organizational directory only**.
   For an existing registration, change its
   **Authentication → Supported account types** to this option; its manifest
   `signInAudience` must be `AzureADMyOrg`. Add Web redirect URI
   `https://<app>.azurewebsites.net/.auth/login/aad/callback`. Record its application
   client ID and directory tenant ID, and create a client secret with an expiry.
   In App Service's Microsoft provider, use issuer URL
   `https://login.microsoftonline.com/<tenant-id>/v2.0`, not `common`, `organizations`
   or `consumers`. Under **Additional checks**, restrict tenants to this same
   directory and allow its authorized users rather than a specific user-ID list.
   Keep **Which client applications can call this app? → This application only**;
   the shared policy retains that client-ID restriction required by this tenant.
   Personal Outlook/Hotmail accounts and users from unrelated tenants cannot use
   this provider; use Google/GitHub for personal-account testing. Organization
   consent, guest access, Conditional Access and registration policies still apply.
   The deployment identity cannot
   change or verify the registration's `signInAudience`; an authorized owner must
   set it. The Azure workload/deployment tenant does not change.
2. In an authorized Google Cloud project, configure OAuth consent/audience and
   create an **OAuth client ID, Web application**. Set JavaScript origin to
   `https://<app>.azurewebsites.net` and redirect URI to
   `https://<app>.azurewebsites.net/.auth/login/google/callback`. If the consent
   app is in Testing, explicitly add the Google accounts used for acceptance.
   Publication/verification requirements depend on the app's audience/scopes.
3. In GitHub Developer settings, create an **OAuth App**, not a GitHub App.
   Set homepage to `https://<app>.azurewebsites.net` and authorization callback to
   `https://<app>.azurewebsites.net/.auth/login/github/callback`. Record its OAuth
   client ID and create a client secret. Organization OAuth restrictions may
   require approval. Login requests `read:user`, not repository access.
4. In App Service **Settings → Environment variables → App settings**, retain or
   enter each provider's client-secret **value** under its exact setting name:

   | Provider | Protected App Service setting |
   | --- | --- |
   | Microsoft | `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` |
   | Google | `GOOGLE_PROVIDER_AUTHENTICATION_SECRET` |
   | GitHub | `GITHUB_PROVIDER_AUTHENTICATION_SECRET` |

   Adding the providers through App Service Authentication already creates these
   settings. Keep those values; do not replace them with Secret URIs or Key Vault
   references. Enter any missing value through the portal, not chat, commands,
   parameter files or logs. A first foundation deployment creates the app but does
   not invent credentials; its owner must set them before login/release acceptance.
   The deployment guard rejects blank/missing secrets and Key Vault references.
   Restrict access to app settings: Website Contributor can read these values,
   including the scoped GitHub delivery identity. Rotation requires updating the
   setting, testing a fresh sign-in and only then revoking the previous credential.

   Key Vault is not required. The previously created dev vault remains unused:
   organization policy disables its public network and private connectivity is
   outside this MVP change. Do not override that policy or delete the existing
   vault/roles as part of auth setup. See the
   [contracts decision](adr/0001-mvp-contracts.md#browser-identity-and-oauth-credentials-mvp-decision).
5. Copy `infra/environments/dev-auth.parameters.example.json` to an ignored local
   path such as `.venv/dev-auth.parameters.json`. Replace **all** synthetic IDs,
   and client IDs; its example GUIDs are not real registrations.
   The file contains nonsecret registration IDs only. Preview resource changes
   without showing app-setting diffs, then apply
   the targeted template using an authorized operator:

   ```powershell
   az deployment group what-if --subscription <subscription> --resource-group test-2 --template-file infra\app-service-auth.bicep --parameters "@.venv\dev-auth.parameters.json" --result-format ResourceIdOnly
   az deployment group create --subscription <subscription> --resource-group test-2 --name ai-sous-chef-dev-auth --template-file infra\app-service-auth.bicep --parameters "@.venv\dev-auth.parameters.json" --output none
   ```

   In WSL Bash, use `/` for local file paths instead of `\`. Do not print secret
   values, all app settings, or the token-bearing `/.auth/me` response to logs.
   This template reads and preserves existing provider secrets and other app
   settings, removes the three old
   JWT settings, pins both the backend and Easy Auth to `socialAuth.tenantId`,
   sets the canonical origin/mode, and
   applies the shared three-provider auth policy. It does **not** create login
   registrations, grant RBAC, enable Foundry agents, or redeploy models/hosting.
   Do not change app settings concurrently while applying its read/merge/write.
6. Check App Service **Environment variables** for the three required nonempty
   settings without copying their values, and **Authentication** for all three enabled providers,
   require authentication, HTTP 401, token store enabled, HTTPS, fixed one-hour
   sessions and no refresh grace/external redirects. The deployment guard checks
   the ARM `GET .../config/authsettingsV2/list` response's `properties` directly rather than depending on
   Azure CLI's version-dependent `webapp auth show` response shape.
7. Merge the reviewed backend change to `dev` and let the deployment run. Applying
   the new runtime settings before this code is deployed may temporarily make
   the old app unavailable; do not report completion until rollout succeeds.
   Open `/auth/login` in a private browser window and exercise each provider,
   `/auth/session`, sign-out, expiry/401, wrong-Origin mutation rejection and
   cross-user data isolation. For Microsoft, test an authorized account from the
   configured directory and confirm that personal Microsoft accounts and accounts
   from other directories are rejected. Google/GitHub personal sign-in remains
   available independently. Confirm the runtime tenant pin, platform issuer and
   tenant allowlist all match.
   A protected root returning `frontend_unavailable`
   after sign-in means the separate frontend build is absent, not failed login.

For later **foundation** deployments, set `ASC_AUTH_MODE=app_service`,
`ASC_AUTH_CLIENT_ID` to the Microsoft client ID,
`ASC_AUTH_ORIGIN` to the canonical app origin, and
`ASC_SOCIAL_AUTH_CONFIG` to a JSON object with the same three `socialAuth` fields
from the example. Omit the old issuer/JWKS environment inputs. These are deployment
inputs; the foundation intentionally does not emit `ASC_AUTH_CLIENT_ID` as a
runtime setting in this mode. Do not rerun the default `external_id` configuration
over a multi-provider app. Both deployment paths share `modules/auth-policy.bicep`.

Cloud registrations, credential setup/rotation and browser acceptance are **operator
actions still required**; offline checks do not complete their acceptance.

## External ID admin actions (each environment)

Provisioning the consumer directory, app registration and user flow requires
approved tenant-admin access. This repository does **not** create those directory
objects or grant roles automatically.

1. Have the owner approve a **consumer external tenant** and the dev application.
   In that tenant, create a single-tenant **Web** app registration for AI Sous Chef
   dev. Record its directory ID and client ID in the approved environment inventory,
   not personal-account details. Use a distinct registration for prod.
2. Create a sign-up/sign-in user flow (email/password or email one-time passcode),
   associate this registration with it, and configure approved branding/consent.
   Enable only the claims needed for sign-in. Do not enable implicit token grants,
   wildcard replies or public-client flows for this web registration.
3. Register the exact Web reply URI and front-channel logout URI below. For each
   approved custom domain or slot, register its exact callback separately; do not
   copy prod callbacks into dev. Set `ASC_AUTH_ORIGIN` to the canonical browser
   HTTPS origin, using the existing App Service's actual `defaultHostName` output or
   an approved custom domain. Do not construct a hostname from the resource name.
4. From the registration's external-tenant OIDC metadata, record the **exact**
   `issuer` and `jwks_uri` as `ASC_AUTH_ISSUER` and `ASC_AUTH_JWKS_URI`.
   Metadata/authority aliases can differ from the canonical token issuer; do not
   guess these values from a tenant display name. Both must be HTTPS
   `*.ciamlogin.com` endpoints. Custom authentication domains need a separate
   reviewed configuration change; they are intentionally not accepted here.
5. Create a short-lived confidential-client credential only for this consumer
   provider. An external tenant is separate from the workload tenant, so this
   template uses the provider's supported client secret, not a made-up managed
   identity federation. Store its value directly in the protected App Service
   setting `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` through the portal.
   Never paste it into source, `.env` files,
   ARM parameters, commands, logs or issue comments.
6. Restrict App Service settings access to approved operators and the scoped
   deployment identity. Foundation redeployments preserve the existing provider
   credential; they take neither its value nor a Secret URI as an input.
   No secret values are outputs. A Key Vault reader role is not required.
7. Perform required admin consent/reauthorization using the approved external
   tenant account. Changes to claims, consent, identity providers or the user flow
   require signing out and a new interactive authorization. For a rotated secret,
   update the App Service setting and confirm fresh login before
   retiring the old credential. Record expiry/rotation ownership in the approved
   inventory. Do not record credentials or consumer identifiers in the checklist.

| Environment | Web reply URI | Front-channel logout URI | App return after logout |
| --- | --- | --- | --- |
| Dev | `https://<dev-host>/.auth/login/aad/callback` | `https://<dev-host>/.auth/logout` | `https://<dev-host>/auth/signed-out` |
| Prod (not approved for deployment here) | `https://<prod-host>/.auth/login/aad/callback` | `https://<prod-host>/.auth/logout` | `https://<prod-host>/auth/signed-out` |
| Local | None for this hosted web registration | None | Use the dev login for interactive testing |

Only same-origin returns are allowed (`allowedExternalRedirectUrls: []`).
The application login/logout wrappers reject all supplied query parameters; callers
cannot choose a return URL. The tenant registration independently rejects an
unregistered OAuth `redirect_uri`. Easy Auth owns `/.auth/*`; do not proxy those
paths straight to Uvicorn or expose a second unprotected application port.

## Reproducible deployment

Set these **nonsecret** process variables in addition to the existing foundation
deployment variables, then use the reviewed what-if/deploy procedure in
[`infra/README.md`](../infra/README.md). Both dev and prod parameter files require
them; neither selects a tenant or credential implicitly:

```text
ASC_AUTH_ISSUER=<exact issuer from approved external-tenant metadata>
ASC_AUTH_JWKS_URI=<exact jwks_uri from approved external-tenant metadata>
ASC_AUTH_CLIENT_ID=<consumer web registration UUID>
ASC_AUTH_ORIGIN=https://<actual-approved-dev-host>
```

The hosting module sets these app values, including the approved `ASC_AUTH_ORIGIN`.
Existing provider credentials remain in App Service settings across foundation
redeployments; there is no Secret URI input. Set the Microsoft credential through
the portal before attempting login, not through `.env` or parameter files.
Do not disable Easy Auth to work around missing/expired credentials.
Application logging excludes tokens, cookies, claims and raw provider errors.
Keep platform HTTP/debug logs off: authorization callback query strings contain
codes and must not be retained in telemetry or screenshots.

## Local and offline testing

Health/readiness and local docs work without authentication. All other routes
return 401 until a valid signed token is supplied. Configure the four commented
`ASC_AUTH_*` runtime settings in `.env.local.example` using approved dev metadata.
For a manual local API check, obtain only a **synthetic account's** current signed
ID token from the approved dev Easy Auth session (`/.auth/me`) using the browser's
developer tools. Treat it as a credential: do not copy it into scripts, shell
history, URLs, local storage or logs. Enter it through a hidden prompt in a local
test client and send it only to the local API as an Authorization header. Discard
it after testing; never use a real consumer's token or data. Interactive callback
testing runs on dev, not a localhost authentication bypass.

Automated tests generate ephemeral RSA keys and synthetic, signed JWTs. Only the
JWKS fetch is replaced in tests; signature and claims verification still execute.
There is no test key in application configuration or production code. Run:

```text
python -m pytest tests/integration/test_authentication.py tests/integration/test_authorization.py tests/integration/test_application.py tests/integration/test_privacy_logging.py tests/unit/test_user_scope.py tests/unit/test_settings.py
python -m unittest discover -s tests/infrastructure -p test_foundation.py -v
```

## Dev acceptance record (operator/peer review required)

Offline coverage includes signature/algorithm checks, incorrect issuer/audience,
expiry, not-before, forged identity headers, stable isolated user scope, CSRF,
fixed login/logout targets, signing-key outage and compiled Easy Auth settings.
The following **live checks remain pending** until the approved tenant/registration,
provider credentials and dev deployment exist. Do not mark issue 17 complete from
offline tests alone:

- [ ] Record approved directory/client IDs, user-flow association, canonical host,
  callback/logout URLs, consent and credential-rotation owner in the environment inventory.
- [ ] Sign up/sign in with a synthetic account via `/auth/login`; confirm return
  to `/`, then authenticated `/auth/session` (the absent UI still returns 503).
- [ ] Sign out via `/auth/logout`; confirm `/auth/signed-out` and subsequent
  `/auth/session` 401. Return through its login link.
- [ ] Request `/api/v1/me` anonymously: 401, not 302 or HTML login. Let the
  one-hour session expire and confirm 401; verify the client's reauthorization behavior.
- [ ] Try a signed token with the wrong issuer and wrong audience: both fail.
  Attempt an unregistered OAuth reply URI and an external post-login/logout return:
  neither reaches the unapproved host. Do not include tokens/codes in evidence.
- [ ] Confirm direct protected provider settings and reauthorization after rotation;
  ask the other collaborator to review the feature PR into `dev`. Do not deploy
  prod, self-approve or auto-close the issue on dev integration.

References: [App Service external configuration](https://learn.microsoft.com/azure/app-service/configure-authentication-provider-aad),
[App Service authentication API](https://learn.microsoft.com/azure/app-service/configure-authentication-customize-sign-in-out),
[App Service settings security](https://learn.microsoft.com/azure/app-service/configure-common#configure-app-settings).
