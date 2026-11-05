# Recipe + Nutrition structured output and grounding (ASC-26 / ASC-30 / ASC-34)

This slice implements the GPT-5.4 specialist's version-4 instructions, strict
draft/full-recipe schemas, one-call New Foundry generation, backend enrichment,
identity-bound Foundry IQ recipe retrieval and conditional Web IQ. It does not activate chat
or an Azure release, save/cook a recipe, or replace
the final full-recipe safety gate.
[ASC-36's final gate](#final-recipe-gate-asc-36) now guards full candidates and the
chat completion/save-preparation boundaries. No model/tool availability research or
alternative provider is required.

## Execution and tool boundaries

`RecipeSpecialistService` implements the existing `RecipeSpecialist` protocol.
For live retrieval, construct it with `RecipeSpecialistService(gateway, None, nutrition)`,
where `gateway` is the configured `FoundryGateway` and `nutrition` is an explicit
`NutritionTool`. `RecipeSpecialistService.from_settings(settings)` now constructs
that live gateway with `UsdaNutritionTool`; see [USDA setup](#usda-nutrition-asc-32).
Passing a `RecipeGroundingTool` instead of `None` retains the
backend-supplied evidence seam for offline fixtures.
The interfaces are explicit dependencies; there is no default successful tool
implementation, fixture mode selected in dev/prod, API-key fallback, or Cosmos access.
Fixtures and offline doubles live only in `tests/fixtures/recipe-specialist.json`
and `tests/unit/test_recipe_specialist.py`. All fixture recipes, nutrition amounts
and food identifiers are synthetic, not real USDA facts.

## USDA nutrition (ASC-32)

The backend uses the supplied
[HemantaPatil/mcp_postgres](https://github.com/HemantaPatil/mcp_postgres)
integration, inspected at revision `2a313bc599a477bc2b2df6dcc43857e4002ab22f`.
Its actual transport is stdio. Its `search_foods(keyword, limit=20)` returns
`ndb_no`, `long_desc`, `shrt_desc`, `food_group`; `get_nutrition_profile(ndb_no)`
returns nutrient rows with those descriptions, `nutrdesc`, `units`, `nutr_val`
and `sr_order`. These are **USDA SR NDB numbers**, including leading zeros,
not FoodData Central FDC IDs. Amounts use the SR per-100-g edible-food basis.
The typed raw profile retains the original nutrient names, units and values.

The application pins the imported runtime libraries (`mcp==1.30.0`,
`asyncpg==0.32.0`) in its normal Python 3.12 dependency locks. It fetches only the
upstream server module at setup/build time, verifies its fixed SHA-256, and
packages it with the app. It does not install the upstream project metadata or
its unused plotting/psycopg dependencies. The source module and its actual MCP
protocol are exercised on Python 3.12; a second developer-specific interpreter
or checkout is no longer required. Runtime never downloads or installs code.

### Shared developer and Azure setup

Both collaborators use the same reviewed application branch and the same Azure
database, but separate read-only database accounts. Create each account using
pgAdmin's password field and grant only CONNECT to `usda`, USAGE on `public` and
SELECT on `fd_group`, `food_des`, `nutr_def`, `nut_data`. Do not grant the
administrator account or share its password. Add each developer's current public
IP to the Azure database firewall; restrictive work Wi-Fi/VPNs may still block
outbound port 5432. The Azure app later needs its own allowed network path.

From the repository root, using the app's Python 3.12 virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
.\.venv\Scripts\python.exe -m sous_chef.usda_dependency
```

The final command downloads exactly revision
`2a313bc599a477bc2b2df6dcc43857e4002ab22f` from GitHub, checks SHA-256
`1edc1d9d3fbc5458bb45020ed985c8cd14c3051633165ecaf8d382b393eda379`,
and writes ignored `_usda/main.py` inside the application package. An existing
file is verified, not silently replaced or upgraded. No secrets or Azure access
are required for this step. Both developers run it once after dependency setup;
CI runs the same command before creating deployment ZIPs and wheels. Missing or
modified source fails setup/startup rather than downloading a fallback.

Keep the existing `.env.local` settings and add `ASC_USDA_ENABLED=true` to enable
the real startup check. Do not copy over an existing file or commit credentials.
For a password-free command history, use a PowerShell credential prompt and pass
the connection configuration to that process:

```powershell
$env:ASC_ENVIRONMENT = "local"
$env:ASC_USDA_ENABLED = "true"
$credential = Get-Credential -Message "Your own read-only USDA PostgreSQL login"
try {
    $env:ASC_USDA_MCP_ENV = @{
        USDA_POSTGRES_HOST = "<server>.postgres.database.azure.com"
        USDA_POSTGRES_DATABASE = "usda"
        USDA_POSTGRES_USER = $credential.UserName
        USDA_POSTGRES_PASSWORD = $credential.GetNetworkCredential().Password
    } | ConvertTo-Json -Compress
    .\.venv\Scripts\python.exe -m uvicorn sous_chef.app:create_app --factory `
        --host 127.0.0.1 --port 8000 --no-access-log
}
finally {
    Remove-Item Env:\ASC_USDA_MCP_ENV -ErrorAction SilentlyContinue
    Remove-Variable credential -ErrorAction SilentlyContinue
}
```

The local adapter/auth/profile settings remain unchanged. `ASC_ADAPTER_MODE=local`
does not turn enabled USDA into a fixture; this is a real external database call.
Python loads `.env.local` because `ASC_ENVIRONMENT=local`. Alternatively store
`ASC_USDA_MCP_ENV` as single-quoted JSON in that untracked file. Never put real
credentials in examples, screenshots, command arguments, source or shared logs.

On **every app start** with USDA enabled, FastAPI starts the real stdio MCP child,
validates its tools and performs an attributed lookup for `Carrots, raw` using
100 g and one serving. It accepts known/partial source-backed nutrition, not an
empty lookup. Broken source/dependencies, credentials, data, TLS or connectivity
prevent startup with a safe error. The startup check does not invoke Foundry,
change data, enable chat or bypass authentication/release gates. An enabled local
app must pass exactly the same startup check as Azure.

The MCP child is **request-scoped**, not a permanent network daemon: the startup
check closes/reaps its child; each recipe nutrition lookup starts its own child
inside that backend host and closes it afterward. The app owns the process and
there is no manual MCP server command or developer laptop endpoint. Simultaneous
requests do not share protocol state or user data. `app.state.nutrition` holds the
configured real adapter after the startup check; the specialist factory uses the
same settings. Existing chat activation remains its own deliverable.

Offline unit/API tests leave `ASC_USDA_ENABLED=false` (the default) and require
no database. Startup integration tests explicitly enable it and use subprocess
fixtures. A separate upstream contract test uses the real pinned source and MCP
SDK with synthetic database rows; it does not certify Azure connectivity.

For **Azure App Service**, merge through the normal reviewed dev delivery flow.
CI fetches/verifies the dependency before cloud login; the source ZIP and wheel
include it, and Oryx installs the locked MCP/asyncpg dependencies with the app.
Set these protected App Service environment variables:

| Setting | Value |
| --- | --- |
| `ASC_USDA_ENABLED` | `true` |
| `ASC_USDA_MCP_ENV` | JSON with the four database connection keys above, using an app-specific read-only account; store securely/directly or through a Key Vault reference |

Do not set developer interpreter/source paths in Azure. The app automatically
uses its own Python interpreter, included launcher and checksum-pinned source.
Existing infrastructure/deployment retains these owner-managed app settings.
Allow the app's actual outbound addresses or approved VNet path through the
PostgreSQL firewall, rather than broadly allowing all Azure services. Ensure
TLS certificate trust and port 5432 connectivity from the app host. Only enable
the startup check once those settings and the reviewed artifact are in place.
Restart the app; failed startup means the integration is not ready. The supplied
database password flow does not claim managed-identity database authentication.

Advanced operator overrides `ASC_USDA_MCP_PYTHON` / `ASC_USDA_MCP_SERVER` remain
available for a separately managed interpreter/entrypoint. They are unnecessary
for this shared setup and must not reference another developer's machine.

Configure the dependency's database connection securely **before using it**:
the upstream sample contains hardcoded local database credentials and is not a
production configuration. Point `ASC_USDA_MCP_SERVER` at the included
`sous_chef/usda_mcp_server.py` entrypoint in the installed application package
(selected automatically by the shared setup above).
It loads the supplied module, replaces its `db_config` from protected
configuration, and runs its original async main/cleanup. It requires
`USDA_MCP_SOURCE` (automatically resolved to the pinned source in the normal setup),
`USDA_POSTGRES_HOST`, `USDA_POSTGRES_DATABASE`, `USDA_POSTGRES_USER`,
`USDA_POSTGRES_PASSWORD`, and optionally `USDA_POSTGRES_PORT` (default 5432).
The wrapper enforces certificate/hostname-verified TLS, a five-second connection
and statement deadline, a one-to-two-connection pool and read-only transactions.
TLS uses Python's default system-trusted CA certificates; no separate PostgreSQL
`root.crt` file is required. Certificate and hostname verification remain mandatory.
The isolated MCP socket event loop uses asyncio Happy Eyeballs with a 250 ms
address-family fallback, so an unreachable IPv4 or IPv6 route cannot exhaust the
connection deadline before another resolved address is attempted. It keeps the
original database hostname for TLS verification and changes no system networking
settings.
Do not copy sample credentials
or database secrets into this repository. `ASC_USDA_MCP_ENV` is a JSON object of
explicit server-only environment variables; store it as a protected App Service
setting/Key Vault reference. Pass those server configuration keys through that
object. Values are `SecretStr`, redacted in settings/config
representations, and never logged. Use this launcher rather than directly running
upstream `main.py`: passing environment variables alone does not override its
hardcoded dictionary. Use a database role limited to SELECT on the
required USDA tables. No consumer data belongs in that database.

Stdio itself has no remote token-authentication mechanism. This implementation
does not impersonate an Entra Agent identity or replace the existing managed
Foundry identity bindings. The gateway still invokes the pinned specialist
identity with managed-identity-first credentials. Database authentication stays
inside the configured dependency; use managed identity there if the deployment
supports it, otherwise protected server-only credentials. Backend/application
credentials and the rest of the parent environment are **not** inherited by the
child: only OS essentials and explicitly configured server variables are passed.
Keep that entrypoint read-only/operator-controlled and its stdout MCP-only.

Each recipe nutrition lookup starts one isolated stdio session, negotiates MCP
`2024-11-05`, validates both advertised input schemas and executes only
`search_foods` and `get_nutrition_profile`. Other advertised tools, including
`execute_query`, are never callable. Only food names and selected NDB numbers
leave the backend, not profiles, allergies, history, groceries owned by the user,
user IDs or conversation identifiers. The logical `usda_nutrition` tool remains
one reserved composite operation in the existing request budget; it makes no
additional model calls.

The app-owned launcher replaces the dependency's literal-substring search tool
registration with an ingredient-oriented read-only query; the checksum-pinned
upstream file, tool names/input schemas, pool lifecycle and profile tool remain
unchanged. Search splits punctuation-separated words and requires every term
across the source descriptions, independent of order. Preparation terms such as
`raw` and `roasted` match whole words, not substrings such as `strawberry`.
All terms are bound parameters, not SQL fragments. Filtering and ranking happen
in the database **before** the 20-row limit: exact descriptions first, then raw
ingredient entries, other entries, and finally baby foods, fast/restaurant foods,
prepared meals and soups. Thus `chicken, raw` retains the raw requirement even
when the source description includes intervening cut/skin details; generic
`chicken` prioritizes raw poultry without selecting a cut or inventing a form.
Ranking is a convenience for candidate presentation, not evidence of uniqueness.

A name-only lookup requires one unambiguous description/short-description match:
case/whitespace/comma normalization or identical words in a different order.
It does not infer a cut, skin, preparation, synonym or food form from a generic
name. Empty searches never broaden by dropping terms; a saturated
20-row result cannot establish uniqueness and is never automatically selected.
An explicit selected NDB number must belong to the current source search results;
its profile descriptions/category must match that candidate before values are used.
Unknown/ambiguous foods have unavailable nutrition rather than an invented mapping.
At most nine distinct name/selection pairs are queried per recipe, reusing
profiles only within that request; this bounds execution to 18 read-only MCP
calls (one search and one profile per pair) and nine sources. Additional/unmatched ingredients remain explicitly
unmapped and the result cannot be `known`.

The backend converts only these mass units:

| Input unit | Grams per unit |
| --- | ---: |
| `g` | 1 |
| `kg` | 1000 |
| `oz` | 28.349523125 |
| `lb` | 453.59237 |

For each supported nutrient, whole-recipe amount is the sum of
`source amount per 100 g * ingredient grams / 100`. Per-serving amount divides
that **unrounded** sum by the recipe's current serving count. Decimal arithmetic
is used throughout; whole-recipe and per-serving amounts are independently
rounded to two decimal places using round-half-up, only after aggregation.
Changing servings without changing ingredient quantities leaves whole-recipe
amounts unchanged. Changing quantities requires a new calculation.

Volume, pieces, packages, unknown quantities, food density, cooking yield and
edible-portion weights are never guessed.
Amounts describe the selected food form/ingredient inputs, not an asserted
laboratory measurement after cooking. Nutrients are energy (kcal), protein,
carbohydrate, fat, fiber (g) and sodium (mg). An unexpected source unit fails;
an additional energy-in-kJ row is ignored, never relabeled as kcal. A missing
kcal row therefore leaves energy unknown.

Every newly calculated response includes:

- `isEstimate: true` and `notice: "Informational estimate, not medical advice."`,
  including partial/unavailable ingredient lookups.
- `nutrients`: per-serving amounts for supported nutrients with any source
  coverage. In a partial result these may be **known-component subtotals**, not
  complete recipe totals.
- `coverage`: all six nutrient names, each with its own status, rounded
  `wholeRecipeAmount` (or `null` when no component is known),
  `coveredIngredientLineIds` and `unknownIngredientLineIds`. Coverage counts
  ingredient lines, not a percentage of recipe mass or nutritional completeness.
- `unknownComponents`: ingredient line ID, affected `unknownNutrients` and one
  reason: `missing_quantity`, `missing_unit`, `unsupported_unit`,
  `food_unavailable`, `lookup_limit` or `missing_nutrients`.
- `mappings`: the requested quantity/unit, converted `weightGrams`, unchanged
  USDA food ID and source reference for every contributing ingredient.

A nutrient is `known` only if every ingredient contributes that nutrient;
`partial` retains attributed subtotals and lists every unknown component;
`unavailable` has no nutrient amount, not zero. Other nutrients remain usable
when one nutrient is missing. The overall status is `known` only when all six
nutrients cover every ingredient. An entirely `unavailable` result has no
amounts, mappings or citations, but still includes unknown-component coverage.
The upstream query omits zero-valued nutrients, so an absent nutrient is
**unknown, not zero**. An explicitly returned zero is retained as source data.
Consumers must label partial amounts as subtotals and display the notice; none
of these states certifies a precise cooked-food measurement, diagnosis or
treatment.

These are additive response fields. Older stored recipe snapshots may lack
detailed coverage/weights; defaults preserve read compatibility without
inventing coverage for them. No new configuration is required beyond the
existing enabled USDA integration.

Provenance includes the unchanged NDB number, a unique `usda:sr:<ndb_no>` source,
the USDA SR dataset reference and `version="SR (release unspecified)"`: the
dependency does not return a dataset release and the backend never fabricates
one. A nutrition profile must match the selected food descriptions/category.
No nutrition output from model memory is accepted.

`ASC_USDA_MCP_TIMEOUT_SECONDS` defaults to 10 (maximum 10), inside the shared
specialist deadline. Each response is limited to 128 KiB, a session to 2 MiB,
and a profile to 256 unique nutrient rows. Timeout, malformed protocol/data,
oversized output, child exit and source errors produce safe `RecipeToolError`
categories and abort enrichment, not successful fallback data. Cancellation
propagates and the child is terminated/reaped, with at most one second for
cleanup. Raw child stderr is not forwarded into app logs; operational events
contain only the safe outcome/category and duration.

`tests/unit/test_usda_nutrition.py` exercises matching, units, serving arithmetic,
source IDs, omissions, errors, schemas, limits, cancellation and credential
redaction. `tests/integration/test_usda_specialist.py` runs an actual child-process
MCP exchange through the recipe specialist and validates the final recipe.
Its protocol fixture contains **synthetic** foods and values; it is not a live
database check or USDA data certification, and is never selectable in deployment.
The startup integration checks also run the actual registered search SQL against
an in-memory SQLite fixture with more than 20 prepared chicken products, raw cuts
and cooked entries. They verify term preservation, pre-limit filtering/ranking,
word-boundary preparation matching and selected-ID revalidation through the real
pinned server and MCP SDK; they do not certify a live PostgreSQL import.

## Ingredient calories in the app

The **Groceries** screen includes an **Ingredient calories** lookup. Enter a
food such as `chicken`, then select **Look up calories**. Exact or unambiguously
equivalent descriptions can resolve immediately. Otherwise choose the appropriate
USDA description from **Choose the USDA food**, checking its cut, skin and
preparation. Up to 20 real source candidates are shown; a full result set asks
you to refine the query. No first-hit or model-memory choice is made.
The result shows source-backed kcal **per 100 g**, the
unchanged SR NDB number, source ID and dataset provenance. It does not infer a
portion size or update groceries. Unknown/ambiguous matches and missing energy
values stay unavailable rather than becoming zero. Nutrition remains an
informational estimate, not medical advice.

The signed-in browser sends `POST /api/v1/nutrition/lookup` with
`{"name":"Carrots, raw"}` and its CSRF token. The authenticated endpoint returns
`{"data": <NutritionEvidence>}` for one 100 g ingredient and one serving, reusing
the enabled app's real USDA adapter, deadlines and allowlist. It sends only the
ingredient description to MCP, not user/profile data, and makes no model calls.
`POST /api/v1/nutrition/search` takes the same name and returns typed
`foods` (original `ndb_no`, `long_desc`, `shrt_desc`, `food_group`) and an honest
`truncated` flag. Selecting a candidate sends the original query plus `foodId`
to `/lookup`; the backend revalidates membership in the search results and the
source profile. IDs outside that candidate set cannot produce nutrition.
Unknown properties, wildcard names, query parameters and invalid names are
rejected. Errors use the normal problem contract: 502 for invalid tool data,
503 for unavailable/unconfigured integration and 504 for timeouts. Neither
successes nor errors are cached. With USDA disabled, there is no fixture fallback.

The UI requires the existing authenticated session and completed onboarding.
Local startup alone does not configure browser authentication: use the existing
approved signed-in environment once these changes are deployed. Do not bypass
authentication to test the lookup.

### Agent food resolution

`UsdaNutritionTool.search(name, deadline=...)` exposes the same typed, bounded
source candidates to agent composition. A resolver may select an NDB ID only
when the ingredient's cut/form supports it; generic `chicken` cannot authorize
choosing breast, skin or raw/cooked state. Otherwise ask for clarification or
leave nutrition unavailable. No source values are generated by the model.

Pass justified selections in `NutritionRequest.selected_food_ids`, keyed by
ingredient line ID. `RecipeSpecialistService.generate` also accepts those
`selected_food_ids` for a composing caller; unknown line IDs or malformed NDB
numbers are rejected, and the adapter validates candidate membership and profile
provenance. Different selections of the same ingredient name never share a cached
profile. Existing specialist name lookups automatically support equivalent word
ordering without another model invocation.

This supplies the source-search/selection capability; it does not add an
unbudgeted model round-trip, publish new Foundry tool definitions or enable chat.
The live orchestration in #39 must expose these candidates as untrusted tool
data, account for search/model calls in its existing budget, and request
clarification or retain unavailable nutrition for unresolved ambiguity. Keep
deterministic final ingredient safety checks independent of nutrition selection.

## Specialist request flow

The ordinary non-web path uses one specialist model invocation:

1. Reserve an allowlisted `foundry_iq_recipes` call on the supplied request-local
   budget. Shape a bounded query from specialist requirements, groceries, explicit
   allergies/restrictions, skill and time. Never include conversation history,
   learned evidence or user IDs. Queries over 2,000 characters fail rather than
   silently drop constraints.
2. Send the original complete `RequestContext`, specialist requirements and
   untrusted grounding to the pinned Recipe + Nutrition managed agent. The
   gateway requires the distinct agent/workload identity pins. It uses Responses
   v1, `agent_reference`, `store=false` and strict JSON Schema. Live retrieval
   attaches only `knowledge_base_retrieve` on the configured MCP connection,
   requires a tool call, disables parallel calls and sets `max_tool_calls=1`.
   Foundry authenticates Search with the specialist's `AgenticIdentityToken`,
   not the web workload, Orchestrator or a shared project identity. Retrieval
   and generation share one Responses invocation and its provider-counted usage;
   there is no separate hidden retrieval model call. The fixture seam retains
   `tools=[]` and `tool_choice=none`.
3. Validate exactly one completed, approved MCP call with the unchanged backend
   query and no argument overrides. Validate retrieved evidence and the draft,
   its actual cited source IDs, exact source quotes, time budget, declared skill
   ceiling and ingredient restrictions. Hard allergies/restrictions take priority
   over current requests, taste and nutrition goals. Explicit preferences outrank
   learned state. A blocked or ambiguous ingredient list is rejected.
4. Reserve an allowlisted `usda_nutrition` call for exactly those ingredient
   amounts/units and servings. Attach only validated tool evidence. The model
   cannot supply nutrition, source metadata or grocery ownership.
5. Assemble inventory status and missing purchases from the supplied groceries,
   then return the full `StructuredRecipe`, its bounded `RecipeCandidate`
   projection and provider-counted usage. The Orchestrator retains the full
   recipe but reviews only its projection; it never receives another copy of all
   nutrition/tool data in its function result.

The caller reserves the single specialist model invocation before entering this
service (as `OrchestratorService` already does). Both tool calls share the same
budget. The specialist does not make hidden repair calls, create another budget,
or retry failed/ambiguous operations. Invalid JSON, schema, references, provider
usage or enrichment yields explicit `foundry_invalid_result`; unfit time/skill or
ingredient restrictions yield `foundry_rejected`. An empty index/retrieval is
`foundry_no_results`, distinct from an unavailable service. Missing/malformed
source metadata is `foundry_invalid_result`; no generation from memory replaces
failed grounding. Live MCP may already have generated a draft when an empty or
invalid result is detected, but that draft is discarded before nutrition or return.
The fixture path rejects missing grounding before invoking the model.
Service/auth/throttling failures
remain distinct. Tool adapters raise `RecipeToolError` with a safe `FoundryFailure`
category, not a raw upstream body. A returned `status=unavailable` nutrition result
is valid and explicit, not zero nutrition; an actual tool failure stops the request.

The supplied finite deadline covers retrieval, generation and nutrition together,
with a 60-second ceiling. Cancellation starts no replacement invocation. The
gateway preflights instructions, schema, full context, requirements and grounding
against the remaining per-call input allowance; grounding also must fit the
4,000-token tool acceptance allowance. Azure Search requires a minimum 5,000-token
output limit, which is higher than that acceptance limit. Live preflight reserves
the full 5,000-token service allowance in addition to the complete input; oversized
MCP results fail post-validation rather than being silently truncated. This check
occurs after the provider's retrieval/generation call, not before the model sees
the tool result. Output is capped at the existing 1,500-token reservation,
including reasoning. Provider usage is mandatory and checked against both ceilings;
the Orchestrator accounts it cumulatively. Nothing truncates hard constraints to
fit a budget. Conditional web requests use the additional bounded stage below.

## Conditional Web IQ

Ordinary pantry recipes and USDA-backed nutrition amounts do not enable web tools.
The Orchestrator's strict routing function supplies `web_query=null` with
`use_web=false`. For explicitly current information, such as a recall, seasonal
information or recent food news, it supplies `use_web=true` and one public query
of at most 240 characters. This is not permission to search for ordinary nutrition
facts already covered by USDA.

Before any external call, the backend requires a current-information cue in the
original request, rejects contact details, URLs, identifiers and personal phrasing,
and limits query words to that request plus generic public food/search terms.
Profile-only topics cannot be added to a query. This is query minimization, not
a general-purpose personal-data detector; the model must still select only the
necessary public topic. Unsupported or sensitive queries fail explicitly instead
of silently deleting words or sending the full request.

The specialist makes one isolated Responses invocation through its existing pinned
Recipe + Nutrition identity. Only the minimized query enters that invocation:
no profile, groceries, history, user key or complete cooking context. The gateway
overrides the instructions, attaches only the pinned SDK's native `WebSearchTool`
(`web_search`, external access, low context), requires one unchanged search and
disables parallel calls and provider storage. No approximate location is inferred.
The search stage has a 10-second ceiling inside the shared 60-second deadline.

The backend accepts at most three short claims. Each requires an HTTPS URL present
in both the native search sources and URL-citation annotations, valid annotation
bounds and a bounded native source title. Missing citations, changed/multiple
queries, extra searches, page-open/follow-up tools, unfinished responses and
excessive evidence fail explicitly. Empty attributable evidence is
`foundry_no_results`; failure/timeout retains its safe error category. The request
does not retry, fabricate web facts or present a non-web result as web-grounded.
Independent non-web requests continue through the unchanged ordinary path.

The additional model invocation and `web_iq` tool attempt are reserved in the same
request-local budget; actual usage is accounted immediately. The web invocation
has at most 2,000 input tokens and the existing 1,500-token output reservation.
Validated web evidence must fit the 4,000-token tool-result limit and the subsequent
recipe input preflight. Token headroom is rechecked before recipe generation.
The normal web path therefore uses four total model invocations, leaving no extra
allowance for a revision. The global one-web-call limit still spans revisions.
Final-gate rejection cannot create an extra revision budget.

Validated evidence enters recipe generation as explicitly untrusted data.
It cannot authorize tools, replace curated provenance, supply USDA nutrition
or override deterministic allergy/restriction checks. The backend attaches
the claims, native citation URLs/titles and UTC retrieval time as `webGrounding`,
separate from curated `sourceClaims`, `sources` and USDA evidence. The recipe
draft schema remains version 2. Web-informed candidates pass the same final gate
before display/persistence; this slice does not add a citation UI.

Operational events contain only allowlisted categories, opaque operation IDs,
durations, and validated agent identity metadata for `recipe_invoked`. Profiles,
allergies, snippets, recipe text, nutrition payloads and hidden reasoning are never
logged. These pins attribute the intended identity; existing readiness checks and
an approved managed-identity smoke are still required to prove remote bindings.

## Recipe contract

`domain/recipes.py` is the executable schema; models use the existing camelCase
aliases, forbid extra fields and non-finite numbers, and bound every collection.
`RecipeDraft.model_json_schema()` is used directly in the source definition and
the gateway request. All draft properties are required; unknown quantities/units
are explicit `null`, not omitted or guessed.

| Field | Contract |
| --- | --- |
| `schemaVersion` | Exactly `2`; publish and pin source policy 4 for conditional Web IQ and the final gate's supported instruction grammar |
| `title`, `servings`, `totalMinutes`, `skill` | 160 characters; 1-20 servings; 1-1,440 minutes; existing skill enum |
| `ingredients` | 1-30 unique line IDs, display names, numeric quantity or null, supported unit or null |
| `substitutions` | Up to 30 original names/reasons referencing replacement lines actually used in the recipe |
| `instructions` | 1-30 sequential steps, text up to 500 characters, declared ingredient references; every ingredient is referenced |
| `fitReasons`, `adaptations` | Up to 10 entries each, 240 characters each; at least one fit reason; adaptations distinguish creative choices from cited source content |
| `sourceIds` | 1-3 unique curated source IDs actually supplied by retrieval |
| `sourceClaims` | Up to 10 exact quotes (240 characters each) and their cited source IDs; backend checks each quote against the retrieved snippet. These describe the source, not proof about an adaptation. |
| Enriched ingredients | Backend-resolved canonical ID or null, `available/missing/unknown`, and supporting grocery row IDs |
| `shoppingItems` | Exactly the missing ingredient lines, with shortage amounts where determinable |
| `nutrition` | `known/partial/unavailable`, serving basis/count, attributed nutrient amounts, exact ingredient mappings, USDA source/version/food IDs, informational-estimate/not-medical-advice notice |
| `sources` | Up to 12 references, copied from the cited recipe and nutrition tool evidence, never generated by the model |
| `webGrounding` | Null on the ordinary path; otherwise 1-3 bounded claims, native citation URLs/titles and UTC retrieval time, validated by the backend |

Inventory comparison is conservative. It uses catalog-recognized ingredients
and matching normalized display names in the same unit, without implicit
raw/cooked equivalence, density conversion or LLM-minted IDs. Known quantities are
allocated across repeated ingredient lines, not counted repeatedly. A shortage
uses the positive difference, not the full recipe quantity. Unknown stock
quantities, incompatible units, unresolved ingredients or omitted stock rows
cannot prove ownership/absence and remain `unknown` when the visible rows do not
already prove enough stock. With a complete inventory and no matching row, a
recognized ingredient is missing. Generating a recipe never mutates groceries.

Nutrition is never computed by the model. Known totals require every ingredient
to have a tool mapping with exactly its recipe quantity/unit and a USDA source.
Partial values remain explicitly partial, not complete totals. Unavailable
nutrition contains no amounts, mappings or fake citations. Nutrient units are
fixed (`kcal` energy, `mg` sodium, `g` protein/carbohydrate/fat/fiber). The source
dataset/version and serving basis are preserved. No assertion here guarantees
medical suitability or that every consumed ingredient has been extracted from
free-text instructions: the final deterministic full-recipe gate independently
checks the latest profile and ingredient-bearing prose before display/persistence.

## Final recipe gate (ASC-36)

`domain/recipe_safety.py` policy **1** revalidates the actual serialized
`StructuredRecipe`, not just its nominal Python type or fit projection. It uses
the reviewed ingredient catalog and ASC-35's deterministic hard-constraint checks.
Every food must resolve unambiguously even when the profile has no restrictions.
Dislikes and learned preferences never weaken explicit allergies/restrictions.
Catalog group IDs do not authorize a different ingredient in prose: carrot does
not authorize onion, nor rice authorize quinoa.

The gate intentionally supports **controlled English instructions**, not general
natural-language safety classification. Unsupported text fails closed and can
consume the existing single revision; no LLM safety decision or keyword blacklist
approves unknown directions. This can reject otherwise reasonable recipes.

- Instructions start with Cook, Boil, Simmer, Steam, Saute, Bake, Roast, Soften,
  Heat, Chop, Slice, Dice, Peel, Mix, Combine, Stir, Add, Garnish, Top or Serve.
  Follow with declared food names, optionally joined with and/with/in/into/over/to.
  Singular/plural and catalog preparation descriptors are normalized, but food
  identity is not inferred from a coarse catalog ID. References must exactly
  match every food mentioned in each step.
- Optional suffixes are `for N minutes`, `until tender`, or `until soft`;
  clauses may join with `, then`. Unknown foods, pronouns, unspecified sauces,
  extra sentences, negations, thawing, storage and unsupported techniques are
  rejected. Titles, fit reasons, adaptations and substitution reasons also have
  a restricted descriptive vocabulary; they cannot hide extra cooking directions.
- A substitution references its actual replacement line, which must pass all
  checks. The excluded original is not consumed. Supported descriptions include
  `Use olive oil instead of butter.` or `Olive oil replaces butter`; they do not
  exempt butter from restrictions if it appears elsewhere as a consumed food.
- Supported animal foods require the exact thermometer form:
  `Cook the chicken to an internal temperature of 165 F, measured with a food thermometer.`
  Poultry needs at least 165 F, ground/minced beef/pork/lamb/veal 160 F, and finfish 145 F.
  Whole beef/pork/lamb/veal/steak cuts need at least 145 F plus
  `, then rest for 3 minutes`. Raw animal foods cannot be combined/served before
  their temperature step. Cooked/roasted plant ingredients require a 165 F
  thermometer step for reheating. Cooking temperatures use integer Fahrenheit.
- Animal ingredient qualifiers are limited to fresh, raw, whole, boneless,
  skinless, lean, ground and minced. Other catalog preparation descriptors cannot
  silently inherit whole-cut cooking rules. Ground/minced finfish is unsupported.
- Eggs, dairy, shellfish, flour/dough, uncanned pulses, processed animal products,
  frozen/previously cooked animal foods, preservation, fermentation, microwave
  and arbitrary free-form handling are not approved by this policy version.
  ASCII food/metadata text is required; the degree sign is supported in the
  thermometer template. Extending coverage requires reviewed rules and tests.

Cooking rules and the backend-authored clean/separate/chill notice are grounded
in the CDC's [Preventing Food Poisoning](https://www.cdc.gov/food-safety/prevention/index.html)
(page dated November 24, 2025). Its temperature table specifies 145 F plus
three-minute rest for whole cuts, 160 F for ground meats, 165 F for poultry and
leftovers, and 145 F for finfish. It advises against washing raw chicken and
countertop thawing; perishables must be refrigerated within two hours (one hour
above 90 F), with a refrigerator at 40 F or below. No network lookup occurs at
validation time. This is bounded application checking, not certification of
real-world handling, ingredient labels, cross-contact, recalls or medical fitness.

### Regeneration, delivery and save hooks

`OrchestratorService` checks each full candidate **after all tool enrichment** and
again on every specialist revision. A projection must match the full recipe.
Rejected text never enters the coordinator function result: it receives only
`safety_rejected` and the existing revision allowance. The request still has at
most four model calls and two specialist invocations (one regeneration), shared
with nested tool/model work. Exhausting the final-gate attempt/call budget returns
a fixed honest `unable` result with no recipe or projection. A model cannot accept
a rejected candidate. Transport/provider failures remain explicit; they are not
replayed as regeneration.

`ValidatedReply` requires a full candidate whenever it carries a
`generatedRecipeId`. `ChatService` reads the current owned profile immediately
before **each completion attempt**, runs the gate, and generates recipe text with
the backend renderer instead of trusting executor-authored text. It atomically
persists that text, the checked recipe, profile ETag and policy version in the
completed turn. Rejected recipes create neither a successful assistant message
nor a generated/cookable snapshot. There is no draft streaming.
Source quotes and raw tool evidence are attribution, not recipe directions;
the renderer does not display them or append unvalidated model commentary.
Projection-only orchestration remains an internal test seam, not a public reply.
Non-recipe replies select a typed `clarification` or `unable` control result,
never public free-form text. Clarifications select one backend-authored request,
ingredient, time-budget or constraint question; inability uses the fixed safe
response. Executor `content` is retained as a private compatibility input but is
never displayed or persisted, including on the text-only path. ASC-38 must select
the corresponding control kind/code rather than render coordinator prose.

`services/recipe_safety.py` supplies `check_current_recipe` for delivery, edits
and reuse, and `prepare_recipe_save` for a future save endpoint. The latter loads
an **owned completed turn** by turn/generated-recipe ID, never a client recipe
body, and rechecks the full stored recipe against the latest profile. A previously
approved recipe or old safety ETag cannot bypass a newly added allergy.
Neither helper writes RecipeHistory or marks anything cooked; full save/history
API composition is separate work. Every edit and later saved-recipe reuse must
load owned data and call the current-policy hook again. A `CheckedRecipe` is
read-bound evidence, not a cacheable authorization token.

Users and ChatHistory/RecipeHistory are separate Cosmos containers. The profile
ETag records the state checked, not a cross-container transaction or a lock.
A profile change **after** the final read can race a completion/save; callers
must recheck on subsequent saves, revisions and reuse. No atomic cross-container
guarantee is claimed.

No new environment settings, packages, feature flags or provider adapters are
required. The gate cannot be disabled by a request. Generation source policy is
now **4**, with unchanged draft schema **2**; publish/pin that reviewed source
before live composition so the specialist knows the supported output grammar.
No agents, runtime flags, deployment pins or Azure resources change automatically.
The gate is adapter-independent and applies equally to Web IQ-informed and
ordinary non-web recipes.

## Source publication and setup

`recipe_definition.py` versions the specialist policy and builds the pinned SDK's
`PromptAgentDefinition` with GPT-5.4's configured deployment, strict draft schema,
the approved recipe MCP and native Web IQ tools, and an explicit `RaiConfig`.
The source definition defaults to `tool_choice=none`; only the gateway's
stage-specific override enables the appropriate tool.
[ASC-37 versions the platform controls and untrusted-tool guardrails](responsible-ai.md);
passing platform filters never replaces the deterministic ingredient/cooking gates.

Use the existing dev-only publisher and existing identity-bound agents; do not
recreate agents, assume a source version, use `latest`, or edit prompts only in
the portal. In addition to the [existing dev publication setup](orchestrator.md#reproducible-dev-source-publication),
set `ASC_FOUNDRY_RECIPE_NUTRITION_RAI_POLICY_NAME` to the approved, existing
content-safety policy in the target Foundry account/project. Use
`asc-responsible-ai-v1` after the owner applies and verifies the reviewed foundation
policy; an approved custom policy still requires separate control review. Publication
refuses an absent name; an invalid remote policy must fail rather than disable
safety or substitute another policy. The `.env.dev.example` value is intentionally
empty because source configuration does not prove that the policy is deployed.

An authorized owner can explicitly publish the specialist source:

```powershell
$previous = $env:ASC_AGENTS_ENABLED
try {
    $env:ASC_ENVIRONMENT = "dev"
    $env:ASC_AGENTS_ENABLED = "true"
    python -m sous_chef.publish_orchestrator --agent recipe_nutrition --approve-dev-publication
} finally {
    $env:ASC_AGENTS_ENABLED = $previous
}
```

The shared publisher retains its historical module name and default Orchestrator
behavior. It verifies both distinct, enabled identities, checks configured pins,
then appends exactly one source version with policy/model metadata and the
definition's SHA-256 fingerprint. Recipe publication now requires the existing
Foundry IQ connection/index settings as well as the approved RAI policy.
Administrative publication uses the existing
explicit `AzureCliCredential` pattern; application runtime remains
managed-identity-first. An ambiguous publication error must be inspected before
retrying. The command changes no app runtime flags or release manifest.

Keep `deploy/agents.dev.json` disabled until the remaining live tool adapters and chat/final
safety composition are ready. Later work must inject those adapters, retain
budget enforcement and identity separation, and pin the approved source version
in a peer-reviewed release. No deployment or live grounding was performed by
implementing this slice.

## One-time Foundry IQ setup for ASC-30

Reuse the [recipe knowledge settings and owner-operated refresh](../README.md#foundry-iq-recipe-knowledge).
After review, run `index_recipes refresh --configure` with the approved corpus
to add the `grounding` field and update the dedicated semantic configuration.
Existing full recipe text remains searchable and the complete normalized record
remains in `recipe_json`. The semantic grounding field contains only a bounded
excerpt plus stable source ID, pinned dataset revision and reference URL.
This is necessary because Search MCP returns extracted semantic fields, not the
REST retrieval `references/sourceData` envelope. Old, unrefreshed MCP results
fail explicitly; they are not guessed into valid citations.

Optional nonsecret bounds, also used by the setup command:

| Setting | Default / range |
| --- | --- |
| `ASC_FOUNDRY_IQ_MAX_RESULTS` | 3 / 1-3 |
| `ASC_FOUNDRY_IQ_MAX_CONTEXT_TOKENS` | 4,000 / 256-4,000 |

Refresh `--configure` after changing the result count so the knowledge base's MCP
document limit matches the runtime policy. Its service output-token limit remains
5,000, Azure Search's minimum, even when the local grounding acceptance limit is
smaller. Keep `ASC_FOUNDRY_IQ_MAX_CONTEXT_TOKENS` at or below 4,000; setting it to
5,000 is not supported. Knowledge-base configuration sends that service limit
as `retrieveDefaults.maxOutputSizeInTokens`, while diagnostic `/retrieve`
requests use `maxOutputSize`; sending `maxOutputSizeInTokens` to `/retrieve`
causes HTTP 400 on the deployed service. The backend rejects results above either local
bound, oversized excerpts, duplicate references/sources, missing metadata and
unexpected provenance. It never silently truncates returned citation metadata.
The service remains minimal/extractive, with no additional retrieval LLM.

Keep the connection on `AgenticIdentityToken` and grant Search Index Data Reader
only to the distinct Recipe + Nutrition principal. No Search administrative
roles or Search tokens are added to the web workload. Publish the new specialist
source and peer-review/pin its returned version in the later enabled release.
No setup, indexing, agent publication, RBAC change or dev rollout runs automatically.

For approved dev acceptance, use that version through
`RecipeSpecialistService.from_settings(settings)` after configuring the USDA
dependency above. Choose groceries present in the
approved corpus, capture the opaque operation ID and confirm structured output
has the expected source IDs/revisions/reference URLs, exact `sourceClaims` and
separate `adaptations`. Verify empty and unavailable retrieval separately.
Inspect only approved sanitized operational traces; never turn on raw SDK
prompt/tool-body logging or include profiles/groceries in shared evidence.
Offline fixtures do not prove Azure relevance, remote identity binding or live access.

## Focused offline checks

With the locked Python 3.12 environment from the repository setup:

```powershell
python -m pytest tests\unit\test_recipe_specialist.py tests\unit\test_recipe_indexing.py tests\unit\test_orchestrator.py tests\unit\test_orchestrator_gateway.py tests\integration\test_recipe_knowledge.py tests\integration\test_grounded_specialist.py tests\integration\test_web_grounding.py
python -m pytest tests\unit\test_usda_nutrition.py tests\integration\test_usda_specialist.py
```

These are schema/policy/transport contract checks using scripted model responses,
not evidence of GPT-5.4 recipe quality, deployed content-filter configuration or
live USDA/Foundry IQ/Web IQ access. They cover groceries, substitutions, time/skill,
missing/partial grounding, hard restrictions, exact nutrition mappings, strict
SDK output, identity/version publication, budgets, cancellation and safe errors.
Web fixtures also exercise zero-call pantry routing, minimized queries, native
citations, tool/query allowlists, web failures/deadlines, nested usage and allergy
precedence. No live search, agent publication or deployment is performed.
