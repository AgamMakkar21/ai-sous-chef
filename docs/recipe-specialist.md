# Recipe + Nutrition structured output and grounding (ASC-26 / ASC-30 / ASC-34)

This slice implements the GPT-5.4 specialist's version-11 combined source policy, strict
draft/full-recipe schemas, one-call New Foundry generation, backend enrichment,
identity-bound Foundry IQ recipe retrieval and conditional Web IQ. It does not activate chat
or an Azure release, save/cook a recipe, or replace
the final full-recipe safety gate.
[ASC-36's final gate](#final-recipe-gate-asc-36) now guards full candidates and the
chat completion/save-preparation boundaries. No model/tool availability research or
alternative provider is required.

## Combined recipe agent with native web search

The current dev design keeps recipe MCP retrieval and **native web search on the
same Recipe + Nutrition agent version**. Set
`ASC_FOUNDRY_RECIPE_NUTRITION_MODE=combined` and pin its reviewed
`ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_VERSION`. A separate retrieval pin is not
used in this mode. When USDA ingredient calories are missing, calorie fallback invokes
that same combined version; no separate web-phase pin is required or used.
The default `phased` mode remains available for
existing releases; the phase-specific sections below describe that legacy route.

The Orchestrator delegates once to this combined specialist. That one Responses
invocation retrieves curated evidence exactly once and may perform one native
web search for the backend's authorized public query. An ordinary request has
`webQuery=null` and permits only the recipe MCP call. Native web search is not a
backend function or an A2A connection, and no separate Web IQ model invocation
occurs. Current-information query authorization is unchanged; this does not
introduce general web-based recipe inspiration.

The combined strict output is `combined_recipe_nutrition_v2`, an envelope with
`recipe` and `calorieEstimates`. Recipe generation returns the existing draft
plus `webClaims` (at most three short claims with `sourceUrl`) inside `recipe`,
and `calorieEstimates=[]`. Calorie fallback returns `recipe=null` and attributed
estimates for the backend's missing ingredient lines. Nullable references remain
required JSON properties, including explicit null for AI knowledge. The backend
checks the native search query, single-search count, completion, retrieved URLs,
URL annotations, citation spans and web-result budget. It attaches validated web
grounding separately, then runs the existing USDA MCP calculation, source/quantity
mapping checks, Orchestrator review and deterministic final safety gate. Web
citations cannot become curated recipe source IDs, USDA evidence or safety approval.

**Privacy and enforcement boundary:** the same Foundry invocation receives the
private personalization context and has the native web tool available. Published
instructions require the public query unchanged and forbid private context in
searches. The backend validates queries only after Foundry has executed them;
it cannot pre-approve native searches or undo an outgoing query. A rejected
response is not proof that private data was never searched. This is intentionally
different from the isolated web phase; do not describe combined mode as providing
that phase's pre-search privacy isolation.

Combined source policy 12 has its own exact definition/phase/fingerprint checks.
Manually attaching web search to an older retrieval version is not sufficient:
the old instructions/output schema and stale fingerprint metadata do not meet
this contract. Preserve existing agent identities and versions, and publish a
reviewed combined source using `--phase combined`; retain its returned version
without assuming the next number. Native reasoning configuration changes also
require review rather than silently accepting source drift.

## Execution and tool boundaries

`RecipeSpecialistService` implements the existing `RecipeSpecialist` protocol.
For live retrieval, construct it with `RecipeSpecialistService(gateway, None, nutrition)`,
where `gateway` is the configured `FoundryGateway` and `nutrition` is an explicit
`NutritionTool`. `RecipeSpecialistService.from_settings(settings)` now constructs
that live gateway with `UsdaNutritionTool` and selects combined mode from settings;
manual combined composition also requires `combined=True`. See [USDA setup](#usda-nutrition-asc-32).
The normal chat executor also passes the configured mode to its specialist, so
authenticated conversation requests use the same combined route as direct composition.
Passing a `RecipeGroundingTool` instead of `None` retains the
backend-supplied evidence seam for offline fixtures.
The interfaces are explicit dependencies; there is no default successful tool
implementation, fixture mode selected in dev/prod, API-key fallback, or Cosmos access.
Fixtures and offline doubles live only in `tests/fixtures/recipe-specialist.json`
and `tests/unit/test_recipe_specialist.py`. All fixture recipes, nutrition amounts
and food identifiers are synthetic, not real USDA facts.

### Owner-approved dev retrieval check

After source/identity verification, the specialist's Search Index Data Reader
grant and corpus indexing, an owner can run
`python -m sous_chef.check_recipe_knowledge --approve-dev-check --version <verified-source-version> --principal-id <verified-specialist-principal-id>`
with the temporary dev/agents-enabled process settings used for bootstrap.
It uses `AzureCliCredential` explicitly as an administrative test initiator,
checks the selected source and distinct identities, and reads connection metadata
without requesting secrets. The connection must use agentic identity and the
configured knowledge-base MCP target.
The version must be the newly verified **combined** source in combined mode, or
the **retrieval** phase in legacy phased mode, not a manually changed old source
or a generation/web source. Phase, complete definition and
fingerprint checks fail before any Responses call when that pin is wrong.

The check makes one non-retried Responses request to the exact specialist
name/version, inherits its published capabilities, permits at most one
`knowledge_base_retrieve` call and uses only the fixed public query `rice dinner`.
Its exact arguments are `{"query_variants":["rice dinner"]}`, matching the
Search MCP input schema. The array must contain that one unchanged string;
extra variants, additional fields and the legacy `query` field are rejected.
The fixed check does not authorize web; any returned web call is rejected. With a
combined native-web source that is a post-call rejection, not a pre-execution
disable of the attached tool. No USDA call, source publication, role assignment, resource configuration
or runtime activation occurs. Provider storage is disabled. The response must
complete within the configured SDK timeout, use the expected
query/tool, and return nonempty recipe grounding that passes the existing
bounded provenance parser. The published strict draft format remains in force;
the resulting draft is not displayed, accepted as a recipe or saved.
Only a source count and correlation ID are printed.
Model and Search usage charges can apply; do not automatically replay a failed
or ambiguously completed invocation.

On provider failure, the check reports the exception class and HTTP status,
plus only allowlisted error-code and rejected-parameter identifiers when available.
Provider messages, response bodies and unknown identifiers remain withheld.

If the tool-argument check fails, do not weaken the fixed-query comparison or
blindly rerun the model. `python -m sous_chef.check_recipe_knowledge --inspect-tool-schema`
performs a bounded administrative MCP initialization and `tools/list` inspection,
with no model invocation, recipe retrieval or resource configuration change.
It uses the operator's `AzureCliCredential` directly for Search metadata, not
the specialist identity. It reports only bounded input field/type metadata,
never tokens, schema descriptions, examples or default values. A denial does
not prove that the specialist lacks access; do not grant wider permissions
based on it. Live query rejection diagnostics expose only presence/equality/extra-field
booleans, never argument values. Exact query equality remains required.
Correcting this local argument envelope does not change published source
instructions or their fingerprint; an already verified retrieval source
does not need republishing solely for that correction.

Grounding rejection reports a fixed stage such as `chunk_schema`, `recipe_schema`,
`provenance`, without raw provider data. The API failure code
and production acceptance limits remain unchanged. Dev recipe MCP retrieval uses
the larger full-envelope profile described below. Do not relax provenance or
treat administrator-side acceptance as workload or end-to-end chat acceptance.
`python -m sous_chef.check_recipe_knowledge --inspect-grounding-config` reads only
the configured index, knowledge source and knowledge-base definitions using the
operator's Azure CLI identity. It reports boolean comparisons for the dedicated
`grounding` field/semantic configuration, source binding, minimal/extractive mode
and bounded defaults. It performs no document query, MCP tool call, model invocation,
publication or configuration change. Metadata matching does not prove that the
indexed documents contain valid grounding or that the workload can retrieve them.

After explicit approval, `python -m sous_chef.check_recipe_knowledge --approve-dev-search-diagnostic`
rechecks that configuration and lists the actual MCP input contract before one
non-retried `knowledge_base_retrieve` call with `{"query_variants":["rice dinner"]}`.
It uses the operator's Search identity, not a Foundry agent or the workload.
No Foundry/GPT call, Web/USDA call, publication or configuration change occurs;
Search charges may apply. Configuration/schema drift blocks retrieval.
Redirects are disabled at both the HTTP-client and MCP-transport boundaries.
Only output counts, bounded type/presence metadata and allowlisted validation
codes/stages are printed. No recipe text, unknown field names, provider messages,
source IDs or URLs are displayed or saved. Rejected/empty grounding exits nonzero
after reporting that safe diagnostic; do not replay it. A pass establishes only
administrator-initiated MCP data compatibility, not agent or workload acceptance.
The diagnostic captures the native protocol result before the SDK's automatic
output-schema validation, which can embed provider values in exceptions. The
application's strict grounding parser still determines schema/provenance acceptance;
server output-schema and structured-content presence are reported only as booleans.
This Search-only dev diagnostic enforces a 65,536-character MCP limit.
Its `runtime_acceptance` object reports the configured environment's recipe MCP
profile separately. The dev runtime and pinned-agent check now use that same
larger character profile; local/production retain 16,000 characters.
MCP response token counts are diagnostic, not rejection limits.
No hosted setting, source definition or model request
allowance changes. This local code change does not publish or activate chat.
For oversized or multi-block results, it also inspects at most four native content
blocks with a combined 65,536-character inspection budget. Only fixed known-field
shapes, lengths/counts and isolated block validation stages are shown; unknown keys
and all values remain withheld. Each structural summary is capped at 4,096 characters.
Isolated block checks never select, trim or merge evidence for acceptance: each
profile checks the complete response, and source count/schema/provenance limits
remain authoritative.
A previous diagnostic approval is consumed by its attempt; another retrieval,
including one that prints only block metadata, requires new explicit approval.

A pass from `--approve-dev-check` proves the administrator-initiated pinned-agent/agentic-MCP retrieval
path. It does **not** prove App Service managed-identity invocation, USDA
enrichment, recipe quality/final safety, or signed-in end-to-end chat.

## USDA nutrition (ASC-32)

The backend runs an app-owned MCP stdio server that wraps the official
[USDA FoodData Central REST API](https://fdc.nal.usda.gov/api-guide/).
It replaces the earlier PostgreSQL/SR28 integration: no database, data download,
upstream executable download, SQL tool or database firewall is needed.
The server and its pinned MCP/HTTP dependencies ship in the normal Python package.

The only tools are `search_foods(keyword, limit=20)` and
`get_nutrition_profile(fdc_id)`. Identifiers are original **FDC IDs**, not padded
five-digit NDB numbers. Only Foundation and SR Legacy records are accepted;
their nutrient amounts use the per-100-g basis. Branded and Survey/FNDDS records
are intentionally excluded from this ingredient-oriented integration.
The normalization retains `long_desc`, `shrt_desc` and `food_group` for existing
candidate consumers: both description fields contain the API's actual description,
not an invented abbreviation, and group is its category or explicit data type.
`data_type` and an optional source `publication_date` accompany each candidate.

### Shared developer and Azure setup

1. Get a private key from [USDA's signup](https://fdc.nal.usda.gov/api-key-signup).
   Do not send it through chat, commit it, put it in a browser build, or use
   `DEMO_KEY` for normal app traffic.
2. Deploy the reviewed API-backed version through the normal dev delivery flow.
   No manual MCP process, external MCP host or APIM is required.
3. Configure these protected App Service settings (or Key Vault references):

| Setting | Value |
| --- | --- |
| `ASC_USDA_ENABLED` | `true` |
| `ASC_USDA_API_KEY` | The private FoodData Central key; plain secret value, not JSON |

Remove `ASC_USDA_MCP_SERVER` and `ASC_USDA_MCP_PYTHON` overrides before starting
the new build: hosted environments accept only the packaged API-backed server.
Custom launchers remain available only for explicit local development/tests.
Old `ASC_USDA_MCP_ENV` database configuration is ignored by the default server
and is never forwarded to its process. No database fallback exists, including
when the API key is missing or the API fails.

Keep the old database available only for rollback to the old build during the
reviewed rollout. After deploying the API-backed version, restart the app and
confirm startup, a candidate search, and a selected-food nutrition lookup succeed.
Then remove `ASC_USDA_MCP_ENV` and obsolete database credentials/firewall access.
The owner may delete the old USDA PostgreSQL database after accepting that rollback
to the old build will then require restoring it. Delete the PostgreSQL server only
if no other databases/applications use it. Do not delete Cosmos DB: it still stores
application profiles, groceries and conversations.

The host needs outbound HTTPS to `api.nal.usda.gov:443`, not PostgreSQL port 5432.
Save settings and restart the API-backed app. Startup checks real USDA search
and a returned candidate's validated profile; it does not silently choose that
candidate for a user's nutrition request. A missing/invalid key, exhausted quota
or unavailable API prevents enabled startup with a safe diagnostic.
App-only Foundry settings and browser authentication remain unchanged.

For local development, add `ASC_USDA_ENABLED=true` and `ASC_USDA_API_KEY` to the
existing ignored `.env.local`, without overwriting other settings. Use a secret
editor/prompt rather than placing the key in shell history. Install the normal
Python requirements and editable package; no separate USDA setup command exists.
Offline tests leave USDA disabled and use explicitly selected synthetic HTTP
transports. They never use a real API key.

### Runtime and search boundaries

The app's lifespan owns **one serialized MCP process per worker** and reuses it
after the startup check. Cold process creation, imports, MCP initialization and
tool-schema verification have a separate, bounded 30-second startup allowance.
Only after initialization succeeds does the real search/profile check begin,
under the normal `ASC_USDA_MCP_TIMEOUT_SECONDS` budget (ten seconds by default).
Initialization and schema verification must pass before the app serves requests.
If the subsequent API probe times out or is temporarily unavailable/throttled,
startup logs the failure and continues so recipe calorie fallback remains usable.
Malformed API responses and initialization/schema/authentication failures still
block startup; initialization is not retried.
An initialization timeout or cancellation closes and reaps the child.
The startup allowance does not extend normal operations, lock waiting,
standalone sessions or later process recovery.
It closes the child at shutdown; failed or cancelled
protocol exchanges are discarded before a later request starts a new process.
Requests waiting for the process lock consume their own deadline, and the 2 MiB
protocol budget resets for each operation. Well-formed quota/authentication
failures retain the process so its rate-limit cooldown is preserved.
Standalone specialist callers without an app lifespan retain isolated,
automatically closed sessions. No cross-request food/query cache is introduced.

Only OS essentials and the explicitly configured API key/import path enter the
default child environment. The key is sent to the fixed USDA HTTPS origin in an
`X-Api-Key` header, never a URL, browser response or model prompt. Redirects and
environment-proxy inheritance are disabled. HTTP responses are capped at 2 MiB;
requests have a five-second HTTP timeout inside the composite ten-second deadline.
Read-only HTTP calls retry timeout/unavailable failures at most twice, with
0.1/0.2-second backoffs, inside the existing operation deadline; HTTP
500/502/503/504 are retryable, but authentication, quota, no-results and invalid
responses are not. A 429 blocks subsequent API calls in that worker
for the numeric `Retry-After` duration (clamped to 1-3,600 seconds), or one hour
when the header is absent or invalid. It becomes a typed quota error and HTTP 429
for standalone ingredient lookups, not empty foods or fabricated USDA evidence.
Recipe requests can instead use the explicitly attributed calorie fallback. USDA currently
documents a default 1,000 requests/hour/IP; monitor the account's actual quota.

The MCP allowlist contains only the two nutrition tools; arbitrary SQL, URLs,
headers and caller-chosen API endpoints are not exposed. The existing managed
Foundry agent identities remain separate from this backend-owned subprocess.
Only ingredient names/FDC IDs leave the backend, not profile, allergy, history,
user or conversation metadata. The logical `usda_nutrition` tool is still one
reserved composite operation and makes no additional model calls.

Search sends all punctuation-separated terms to FDC as required terms. It reads
one relevance-ranked page of at most 20 Foundation/SR Legacy records, validates
it, preserves every term locally, then ranks before the requested candidate limit.
The smaller source page keeps ordinary broad searches such as rice and salt
within the unchanged 2 MiB response budget; requesting 200 verbose source records
can exceed that budget and abort chat. No additional pages or retries are fetched.
Preparation words match whole words (`raw` does not match `strawberry`).
Exact descriptions rank first, then raw ingredients, other entries and prepared
food categories. This prioritizes raw chicken without inventing a cut/skin.
`truncated=true` also reports an incomplete upstream page, even with fewer than
20 eligible candidates: refine the query rather than assume exhaustive results.
An unresolved broad search leaves that ingredient's nutrition explicitly
unavailable; it does not prevent delivery of an otherwise valid recipe.

A grocery name-only lookup requires one unambiguous description/short-description match:
case/whitespace/comma normalization or equivalent words in a different order.
The matcher shares generic noun normalization with recipe nutrition rather than
maintaining a separate list of supported ingredient plurals. It also accepts
USDA's optional `Spices,` category prefix. It preserves every
food-form qualifier: `raw apple with skin` can match `Apples, raw, with skin`,
and `ground cinnamon` can match `Spices, cinnamon, ground`, but generic `apple`
or `cinnamon` cannot choose a preparation or skin state.
It does not infer a cut, skin, preparation, synonym or food form from a generic
name. Empty searches never broaden by dropping terms; a saturated
20-row result cannot establish uniqueness and is never automatically selected.
An explicit selected FDC ID must belong to the current search results;
its detail ID, data type and descriptions must match before values are used.
Unknown/ambiguous foods have unavailable nutrition rather than an invented mapping.
Recipe enrichment explicitly enables approximate, source-backed matches. It removes
non-nutritional preparation words such as sliced, minced, drained, deveined,
boneless and generic pieces/chunks (without removing an actual breast/thigh cut),
but preserves raw/cooked, ground meat, fat, skin and other composition qualifiers.
Search terms retain the ingredient's identity instead of substituting a named
variety or adding a food-specific cut. Matching rejects unrelated products,
unrequested skin and incompatible cooking forms. Explicit cooked/ground states
and named cuts remain distinct; broth is not treated as raw meat.

Ambiguous user-supplied amounts can use a **quantity assumption**, separate from
the USDA food match. The Orchestrator interprets the request semantically and returns
`quantity_ingredients`: public food names needing cited comparable-recipe masses
and serving counts. The backend builds one bounded Web IQ query from that typed
array, with the same obvious-private-identifier check used for calorie fallback.
No ingredient catalog, vague-phrase regex, exact phrase or token matching decides
whether to search. Empty names mean no quantity search; an unknown pantry stock
or an ordinary request to compose a recipe does not automatically imply a missing
recipe portion. Only the classified food names leave the application, not the
surrounding request, other inventory entries, profile, allergies or history.
Current public information takes priority if both searches are requested. Existing
one-search, model-call and deadline limits still apply in combined and phased modes.
This routing requires the policy-7 Orchestrator source described in
[Orchestrator](orchestrator.md#backend-limits-and-candidate-validation).

The backend checks cited curated excerpts and native-cited web claims for an
explicit ingredient mass (g/kg/oz/lb) and source serving count. Compatible amounts
are scaled to this recipe's servings; the median is used when several cited
recipes agree within a factor of 1.5. Cooking form, ground meat and meaningful
cuts cannot be silently swapped. Missing serving counts, incompatible foods,
ambiguous or substantially conflicting amounts do not support an estimate.
Each source must supply a plausible mass between 0.001 and 1000 g per serving.
Conservative outlier caps are lower for salt (10 g), oil/butter (60 g), and
sugar/honey (100 g) per serving, so bulk brine or frying quantities are not
silently treated as consumed amounts. These bounds reject estimates, not
measured user inputs, and are not dietary recommendations.

These are **assumed whole-recipe amounts**, not a fabricated weight per piece.
Original ingredient quantities, shopping quantities and pantry ownership are
never rewritten. Supported measured mass/volume and USDA household portions take
precedence. Mappings disclose `quantityEstimate`, source quotes, source mass and
servings, and the assumed gram weight in `conversionBasis`. USDA alone supplies
nutrient values. When no reliable estimate exists, nutrition stays explicitly
partial/unavailable and the card asks the user to specify an amount or weight.
Empty comparable-recipe web results do not invent evidence; provider failures
remain explicit. Older snapshots without quantity assumptions remain readable.
Rollback to an older strict reader requires compatibility review before exposing
new nullable-quantity mappings or `quantityEstimate` fields to that reader.

There is no ingredient-specific synonym/substitution table in nutrition lookup.
Generic preparation normalization converts `uncooked` to `raw` and `skinless`
to `meat only`, but keeps variety, blend, dry/ground and composition wording.
Compatible USDA comma-separated identity/qualifier descriptions are supported;
an unrelated compound name or explicit added-ingredient wording is not a match merely because
it contains a requested word. Approximate matches reject unrelated prepared
dishes, organ meats and unrequested skin. Compatible candidates are ranked by exact word match,
raw form, SR Legacy data (which commonly includes household portions), then
specificity and stable source identity. A truncated search is not proof of
uniqueness, but can supply a clearly labeled approximate recipe match. Explicit
user-selected IDs and grocery lookup retain their existing source validation.

All 30 supported ingredient lines can be queried, with at most 60 read-only MCP
calls and 30 USDA sources. Normalized name/selection pairs share one profile
within a request without combining their quantity mappings. Unmatched ingredients
remain explicitly unmapped and the result cannot be `known`.

The backend converts these mass units exactly:

| Input unit | Grams per unit |
| --- | ---: |
| `g` | 1 |
| `kg` | 1000 |
| `oz` | 28.349523125 |
| `lb` | 453.59237 |

### Grams-first generated recipes

The specialist chooses gram quantities for newly composed recipes, with the
intended food form stated explicitly when it affects nutrition: raw/cooked,
skin, flavor and fat level. These are proposed recipe amounts, not measurements
of the user's inventory. Explicit user-provided quantities, units and food-form
constraints remain unchanged. The model must not invent a gram conversion for
cups, spoons, pieces or packages, or silently choose an unspecified product form.
Those ingredients can still have unavailable nutrition.

The backend resolves recipe names against real USDA candidates using the
approximation policy above and verifies the selected profile before calculating
values. No model-generated FDC IDs, nutrient amounts or extra model calls are
introduced. Supported matching does not guarantee that USDA has a compatible
record or all six nutrients.

This instruction change requires a newly published and verified specialist
source: combined policy **9**, or policy **8** for legacy phases. Policy numbers
are not Foundry source-version pins. After review and owner approval, publish
the required specialist phase(s), retain the actual returned versions and update
`deploy/agents.dev.json` through the reviewed delivery flow. The Orchestrator
policy is unchanged. Do not deploy the new verifier against old specialist pins:
readiness will reject their stale instructions/fingerprint.

Offline regression coverage uses the production MCP server with synthetic HTTP
data for an apple/yogurt/honey/lemon/cinnamon recipe, exact per-serving arithmetic,
source IDs and source-backed household-unit conversion. It does not prove that a
live model follows the new instructions. Signed-in acceptance must generate a new
recipe after the coordinated backend/source release and inspect its quantities,
food mappings, nutrition coverage and history reload. Existing saved snapshots
are not recalculated or rewritten.

### Nutrition arithmetic and coverage

For each supported nutrient, whole-recipe amount is the sum of
`source amount per 100 g * ingredient grams / 100`. Per-serving amount divides
that **unrounded** sum by the recipe's current serving count. Decimal arithmetic
is used throughout; whole-recipe and per-serving amounts are independently
rounded to two decimal places using round-half-up, only after aggregation.
Changing servings without changing ingredient quantities leaves whole-recipe
amounts unchanged. Changing quantities requires a new calculation.

Household volumes (`ml`, `l`, `tsp`, `tbsp`, `cup`) use the selected USDA food's
`foodPortions` gram weights, including the source portion's amount and unit.
Equivalent volumes scale using US customary measures (cup 236.5882365 mL,
tablespoon 14.78676478125 mL, teaspoon 4.92892159375 mL). Preparation-specific
portions are preferred where available. Recipe pieces/items use compatible
source portions for any identified food, not a whitelist of produce or eggs.
Explicit part names must match the source part; a whole fruit cannot establish
a wedge's weight. Medium size is preferred when no size was specified.

There are no ingredient-name density constants or assumed wedges-per-fruit
ratios. Source-backed volume weights work for any food; without usable source
portions, quantity estimates require cited comparable recipe evidence rather
than a universal mL-to-gram conversion. Unknown package/can sizes and cooking
yields remain unknown. Missing calories still enter the separately attributed
web/AI fallback below. Original recipe quantities/units are never rewritten.
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
  Additive `foodDescription`, `approximateMatch` and `conversionBasis` fields
  disclose the actual food and measured/source-portion/quantity-assumption basis in the
  recipe card's nutrition sources. Older snapshots may omit these fields.

A nutrient is `known` only if every ingredient contributes that nutrient;
`partial` retains attributed subtotals and lists every unknown component;
`unavailable` has no nutrient amount, not zero. Other nutrients remain usable
when one nutrient is missing. The overall status is `known` only when all six
nutrients cover every ingredient. An entirely `unavailable` result has no
amounts, mappings or citations, but still includes unknown-component coverage.
An absent/null API nutrient is **unknown, not zero**. An explicitly returned
zero is retained as source data. Nutrients are selected by FDC nutrient ID,
not a provider spelling guess. Energy uses kcal ID 1008 when present, otherwise
2048 (Atwater specific), then 2047 (Atwater general); kJ is never relabeled kcal.
Consumers must label partial amounts as subtotals and display the notice; none
of these states certifies a precise cooked-food measurement, diagnosis or
treatment.

These are additive response fields. Older stored recipe snapshots may lack
detailed coverage/weights; defaults preserve read compatibility without
inventing coverage for them. No new configuration is required beyond the
existing enabled USDA integration.

Provenance includes the unchanged FDC ID, a unique `usda:fdc:<fdc_id>` source,
the canonical `https://fdc.nal.usda.gov/food-details/<fdc_id>/nutrients` reference
and the source data type/publication date (explicitly unspecified when absent).
Older stored `usda:sr:` citations remain historical evidence and are not rewritten
or reinterpreted as FDC IDs. A fresh profile must match the selected candidate.
USDA amounts and mappings never come from model memory. Missing recipe calories
can instead use the explicitly attributed fallback below; other nutrients and the
standalone ingredient lookup retain the USDA-only contract.

### Recipe calorie fallback

USDA is attempted first. Successful ingredient mappings, calories and other
nutrients are preserved, including when a later ingredient fails. Missing
calories trigger a web-first invocation of the same combined Recipe + Nutrition
version (or the web phase in legacy phased mode). If it is rejected, incomplete,
empty or only covers some ingredients, the backend makes at most one independent,
tool-free AI-knowledge invocation for the remaining ingredient lines. This is
not a repeated search and never replaces accepted USDA or web estimates.
Only recipe ingredient names and quantities enter that invocation; only the
backend-built public food/calorie query may enter native web search. Safety
catalog groups are not nutritionally interchangeable: ingredient names are
preserved, not replaced by a group's first alias.
Private profile/history/allergy context is not included. Obvious private identifiers
in ingredient names are excluded using the shared public-query privacy check.
This pattern check is not a guarantee of detecting all private text.
Calorie lookup does not require membership in the allergy catalog or a separate
food/qualifier allowlist: unfamiliar ingredients retain their actual names and
food forms. This does not classify unfamiliar foods as allergen-safe or change
deterministic recipe safety checks.

The web-first calorie task searches once with Foundry's native `WebSearchTool`,
which uses [Grounding with Bing Search](https://learn.microsoft.com/azure/ai-foundry/agents/how-to/tools/web-search).
It does not call recipe MCP. It returns kcal for each missing ingredient's
entire recipe quantity, not per 100 g or per serving. A `web_iq` entry requires
an actual searched HTTPS URL with a native citation. If source evidence is
insufficient or the search tool fails, the model may return `ai_knowledge`
with no citation and explicit assumptions. It cannot claim `usda_mcp`.
The recovery request has `useWeb=false`. It must return only `ai_knowledge`
entries with null references, no native citations, and no search or recipe MCP
calls. A separately pinned immutable `calorie` source on the same specialist
identity has no tools and `tool_choice=none`. Normal recipe/web sources retain
`tool_choice=required`; recovery cannot weaken their grounding requirements.
The backend rejects any unrequested recovery tool call.
Web pages about USDA are still Bing-grounded web evidence, not a successful MCP lookup.
The backend sums ingredient calories and scales by servings; the card labels
each ingredient **USDA MCP**, **Grounding with Bing** or **AI knowledge**, links available
references, and warns that fallback values are approximate and not USDA-verified.
Missing protein/fat/etc. remain unknown. Unknown quantities remain unchanged in
the recipe; any calorie-portion assumption appears only in the estimate's basis.

The persisted `web_iq` source/tool identifiers are retained for compatibility
with existing recipe history; they identify Bing-grounded web evidence, not a
separate Web IQ provider.

The backend reserves 30 seconds for calorie fallback plus two seconds for final
completion, within the original request deadline. Both calorie invocations share
that single 30-second ceiling; recovery never resets it. Fallback leaves the final two seconds
and never extends the whole-operation deadline. No application input/output token
ceiling is imposed or sent to the provider, and high reported usage cannot invalidate
otherwise valid evidence. This does not permit another revision or search.
Calorie output is bounded by the 30-line schema, 65,536 text characters and
131,072 native-envelope characters. Up to 30 native citations/search sources
can support the ingredient estimates.
If the entire fallback invocation is itself unavailable, the recipe still
reports missing nutrition honestly; no hardcoded success or fabricated citation
is substituted. Valid subsets are merged without filling omitted lines or
turning an empty result into zero calories. Malformed/incomplete responses,
rejected optional calls and invalid attribution are discarded and logged as
`calorie_fallback_unavailable`; a failed web-first invocation still permits the
independent tool-free recovery. If that also fails, the backend must report the
remaining gap honestly rather than fabricate an amount. Neither failure discards the already-validated recipe
or existing USDA values. Authentication/configuration failures remain explicit.
Transient USDA API failure after successful MCP initialization is logged but
does not prevent startup; initialization/schema/authentication failures remain
fail-closed. Live release smoke still requires actual known USDA-backed nutrition.

Activation requires publishing and reading back the reviewed `--phase combined`
and `--phase calorie` sources, then pinning their returned versions in the version-5
dev manifest. Delivery verifies and copies both specialist definitions without changing
identities; readiness verifies their schemas and policy fingerprints. The live
release smoke requires both USDA-backed chat and complete tool-free calorie recovery.
Historical phased releases remain available for rollback.
No runtime tool/schema override is used.

`ASC_USDA_MCP_TIMEOUT_SECONDS` defaults to 10 (maximum 10) for ingredient lookups.
Recipe sessions scale that budget per nine ingredient lines, capped at 30 seconds
and always bounded by the existing shared specialist deadline. This prevents
removing the old nine-food cap from merely replacing omissions with a ten-second
timeout. Each response is limited to 128 KiB, a session to 2 MiB,
and a profile to 256 unique nutrient rows. Timeout, malformed protocol/data,
oversized output, child exit and source errors produce safe `RecipeToolError`
categories; recipe-only transient failures retain completed evidence and allow
the attributed calorie fallback. Exact ingredient lookup still raises errors. Cancellation
propagates and the child is terminated/reaped, with at most one second for
cleanup. Raw child stderr is not forwarded into app logs; operational events
contain only the safe outcome/category and duration.

`tests/unit/test_usda_nutrition.py` exercises matching, units, serving arithmetic,
source IDs, omissions, errors, schemas, limits, cancellation and credential
redaction. `tests/integration/test_usda_specialist.py` runs an actual child-process
MCP exchange through the recipe specialist and validates the final recipe.
Its protocol fixture contains **synthetic** foods and values; it is not a live
USDA API check or data certification, and is never selectable in deployment.
The startup integration checks run the actual production MCP server with a
synthetic HTTP transport and more than 20 prepared chicken products. They cover
word preservation/ranking, FDC attribution, source-ID revalidation, startup,
process reuse and shutdown. Unit checks cover malformed/oversized API payloads,
units, missing/zero values, authentication, rate limiting and timeouts.

## Ingredient calories in the app

The **Groceries** screen includes an **Ingredient calories** lookup. Enter a
food such as `chicken`, then select **Look up calories**. Exact or unambiguously
equivalent descriptions can resolve immediately. Otherwise choose the appropriate
USDA description from **Choose the USDA food**, checking its cut, skin and
preparation. Up to 20 real source candidates are shown; a full result set asks
you to refine the query. No first-hit or model-memory choice is made.
The result shows source-backed kcal **per 100 g**, the
unchanged FDC ID, source ID and data-type/publication provenance. It does not infer a
portion size or update groceries. Unknown/ambiguous matches and missing energy
values stay unavailable rather than becoming zero. Nutrition remains an
informational estimate, not medical advice.

The signed-in browser sends `POST /api/v1/nutrition/lookup` with
`{"name":"Carrots, raw"}` and its CSRF token. The authenticated endpoint returns
`{"data": <NutritionEvidence>}` for one 100 g ingredient and one serving, reusing
the enabled app's real USDA adapter, deadlines and allowlist. It sends only the
ingredient description to MCP, not user/profile data, and makes no model calls.
`POST /api/v1/nutrition/search` takes the same name and returns typed
`foods` (original `fdc_id`, normalized descriptions/category, `data_type` and
optional `publication_date`) and an honest
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
source candidates to agent composition. A resolver may select an FDC ID only
when the ingredient's cut/form supports it; generic `chicken` cannot authorize
choosing breast, skin or raw/cooked state. Otherwise ask for clarification or
leave nutrition unavailable. No source values are generated by the model.

Pass justified selections in `NutritionRequest.selected_food_ids`, keyed by
ingredient line ID. `RecipeSpecialistService.generate` also accepts those
`selected_food_ids` for a composing caller; unknown line IDs or malformed FDC
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
   selects the retrieval source pin, which declares only `knowledge_base_retrieve`
   on the configured MCP connection and requires a tool call. The request sets
   `max_tool_calls=1` without overriding the source's tools or selection policy.
   Foundry authenticates Search with the specialist's `AgenticIdentityToken`,
   not the web workload, Orchestrator or a shared project identity. Retrieval
   and generation share one Responses invocation and its provider-counted usage;
   there is no separate hidden retrieval model call. The fixture seam retains
   a generation source with `tools=[]` and `tool_choice=none`.
3. Supply exactly `{"query_variants":[<unchanged backend query>]}` as untrusted
   tool-argument data, then validate one completed, approved MCP call with that
   exact object. No splitting, rewriting, extra variants, resource selectors
   or retrieval-limit overrides are accepted. Validate retrieved evidence and the draft.
   The backend attaches the actual retrieved curated source IDs and retains only
   exact source quotes from their verified snippets. Missing, malformed or
   unverifiable model-generated attribution is replaced/omitted, with a
   `recipe_attribution_normalized` operational event containing no source text.
   Optional display attribution cannot abort an otherwise valid recipe; invalid
   retrieval evidence still fails closed. Validate the time budget, declared skill
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
gateway leaves input/output token limits to the model provider. Dev recipe MCP retrieval allows
up to 65,536 characters for its complete result, including every
reference record. Local/production retain the 16,000-character limit.
Native MCP call/discovery envelopes retain their character bound; neither parsed
MCP results nor native envelopes are rejected by token count.
Schema, character, attribution, model-call and deadline guards remain enforced.
Azure Search requires a minimum 5,000-token retrieval-output setting, distinct
from chat model input/output limits; that service-required setting is retained.
Provider usage is mandatory and recorded, never rejected for exceeding an
application token threshold. Conditional web requests use the stage below.

## Conditional Web IQ

The separate Web IQ invocation described in this section is the legacy
`phased` route. Combined mode uses the native search in its recipe invocation,
the same authorized public-query rules, and the post-execution checks above.

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
selects the separately pinned web source, which stores the isolated web instructions,
the native `WebSearchTool` (`web_search`, external access, low context),
required tool selection and strict `WebDraft` schema. The request limits native
tool calls to one and disables provider storage without overriding source configuration.
No approximate location is inferred.
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
request-local budget; actual usage is accounted immediately. Web input/output and
validated evidence are not rejected by token count. Native evidence still obeys
its schema, citation and character bounds, and incomplete provider output is not accepted.
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
| `sourceIds` | 1-3 unique curated source IDs attached by the backend from verified retrieval evidence, not trusted from generated metadata |
| `sourceClaims` | Up to 10 exact quotes (240 characters each) and their cited source IDs; backend retains only quotes verified against their actual retrieved snippets. Unverifiable/malformed optional quotations are omitted and logged, not grounds for discarding the recipe. These describe the source, not proof about an adaptation. |
| Enriched ingredients | Backend-resolved canonical ID or null, `available/missing/unknown`, and supporting grocery row IDs |
| `shoppingItems` | Exactly the missing ingredient lines, with shortage amounts where determinable |
| `nutrition` | `known/partial/unavailable`, serving basis/count, attributed nutrient amounts, exact ingredient mappings, USDA source/version/food IDs, informational-estimate/not-medical-advice notice |
| `sources` | Up to 33 references (3 recipe + 30 USDA), copied from cited recipe and nutrition tool evidence, never generated by the model |
| `webGrounding` | Null on the ordinary path; otherwise 1-3 bounded claims, native citation URLs/titles and UTC retrieval time, validated by the backend |

Inventory comparison is conservative. It uses matching normalized display names
in the same unit, including unfamiliar foods absent from the safety catalog, without implicit
raw/cooked equivalence, density conversion or LLM-minted IDs. Known quantities are
allocated across repeated ingredient lines, not counted repeatedly. A shortage
uses the positive difference, not the full recipe quantity. Unknown stock
quantities, incompatible units or omitted stock rows
cannot prove ownership/absence and remain `unknown` when the visible rows do not
already prove enough stock. With a complete inventory and no matching row, a
ingredient is missing. Canonical catalog IDs remain optional metadata, not
eligibility checks. Generating a recipe never mutates groceries.

USDA nutrient totals are computed from tool mappings with the recipe's
quantity/unit and verified USDA source, never model-provided nutrient values.
Separately attributed calorie estimates may complete energy coverage without
inventing USDA mappings or filling missing macros.
Partial values remain explicitly partial, not complete totals. Unavailable
nutrition contains no amounts, mappings or fake citations. Nutrient units are
fixed (`kcal` energy, `mg` sodium, `g` protein/carbohydrate/fat/fiber). The source
dataset/version and serving basis are preserved. No assertion here guarantees
medical suitability or that every consumed ingredient has been extracted from
free-text instructions: the final deterministic full-recipe gate independently
checks the latest profile and ingredient-bearing prose before display/persistence.

## Final recipe gate (ASC-36)

`domain/recipe_safety.py` policy **3** revalidates the actual serialized
`StructuredRecipe`, not just its nominal Python type or fit projection. It uses
the reviewed ingredient catalog and ASC-35's deterministic hard-constraint checks.
Dislikes and learned preferences never weaken explicit allergies. Dietary
restrictions remain generation preferences, not another hard recipe-safety gate.

The owner-approved policy change removes deterministic **ordinary cooking-safety
classification**. There is no thermometer template, temperature/rest/order rule,
restricted verb grammar, metadata vocabulary whitelist or blanket exclusion of
eggs, dairy, shellfish, flour, dry pulses, leftovers or preparation methods.
Natural instructions and descriptive dish names are accepted. This deliberately
removes the `recipe_cooking_rejected`/`recipe_prose_unsupported` gates that exhausted
both attempts in the observed dev chat. A pass is not cooking-safety certification.

- Stored allergies remain mandatory. Under those allergies,
  declared ingredients must still resolve unambiguously; conflicting,
  possible-allergen and unknown products remain blocked or need clarification.
  Without allergies, catalog coverage alone does not reject a recipe.
- Instructions, titles, fit reasons, adaptations and substitution reasons are
  scanned using the same catalog traits/custom-allergy matching. Ordinary prose
  words are not treated as unknown ingredient names. Known forbidden ingredients
  and relevant ambiguous products cannot be hidden in prose. Name specific oils,
  milks, stocks and sauces rather than ambiguous generic products.
  In instruction steps, a complete recognized alias may refer to exactly one
  already-validated ingredient linked by that step's `ingredientLineIds`.
  This prevents shorthand from turning a declared specific product into an
  unknown product. Multiple matches, cross-step borrowing, explicit conflicting
  foods and unrecognized declared products still fail closed; metadata has no
  such reference binding.
- The catalog is English, not a universal language/ingredient classifier.
  Untranslated non-Latin letters in ingredients or recipe prose fail closed under
  stored allergies; accented Latin text is supported. Every consumed ingredient
  must still be declared by the specialist; free prose cannot prove exhaustive
  extraction of every possible food.
- A substitution references its actual replacement line, which must pass the
  current allergies. `Use olive oil instead of butter` / `Olive oil replaces
  butter` describes an excluded original, not consumption. The optional suffix
  `to respect the dairy restriction` is also descriptive. These phrases do not
  excuse butter used elsewhere as a consumed food.
- Schema/reference integrity, current-profile rereads, projection equality,
  output size limits, source attribution and honest USDA coverage remain enforced.
  Unknown foods do not acquire fabricated nutrition because cooking checks changed.

The backend-authored clean/separate/chill notice remains general guidance, based
on the CDC's [Preventing Food Poisoning](https://www.cdc.gov/food-safety/prevention/index.html).
It is not an enforced cooking test or certification of handling, labels,
cross-contact, recalls or medical fitness.

### Regeneration, delivery and save hooks

`OrchestratorService` checks each full candidate **after all tool enrichment** and
again on every specialist revision. A projection must match the full recipe.
Rejected text never enters the coordinator function result: it receives only
`safety_rejected`, an allowlisted `rejection_code` and the existing revision
allowance. The same code appears in privacy-safe `recipe_safety_rejected`
operational events, without ingredient names, user text or rejected recipes.
The Orchestrator includes the corresponding correction in its one revision.
An ingredient-list allergy conflict detected by the specialist is a typed,
completed-draft rejection, not an ambiguous provider failure. Its usage is
accounted, unsafe text is withheld, and the same revision requests substitutions
before any USDA enrichment of that rejected draft. Nutrition is calculated from
the replacement draft's actual ingredients, amounts and servings. Only a
replacement that passes the current allergies can be delivered or saved.
The request still has at
most four regular model calls and two specialist invocations (one regeneration), shared
with nested tool/model work. Exhausting the final-gate attempt/call budget returns
a fixed honest `unable` result with no recipe or projection. A model cannot accept
a rejected candidate. Transport/provider failures remain explicit; they are not
replayed as regeneration.

### Flexible chat input and dev activation

Chat input is not the final recipe grammar. Users can supply sentences,
comma-separated ingredients, newline/bullet lists or JSON ingredient objects,
including amounts such as `200 g chicken breasts`, `0.5 kg chicken` and
`1/2 cup rice`. The specialist normalizes explicit amounts into the structured
`name`, `quantity` and `unit` fields; it must retain every food and preparation
qualifier, preserve stored restrictions, and leave genuinely ambiguous
quantities/inventory unknown. Neither users nor the specialist must write a
thermometer template to pass the backend.

The specialist and Orchestrator source instructions describe natural recipe
output and the remaining profile/integrity checks. Source policy versions are
Orchestrator **7**, Recipe + Nutrition **8**, combined Recipe + Nutrition **11**,
and isolated web/calorie **10**. These are
policy versions, **not** Foundry agent-version pins. Existing immutable agents
do not acquire the new instructions when only backend code changes.

Before deploying this change to dev, an authorized owner must publish and verify
new Orchestrator and combined-specialist sources with the existing reviewed
publisher, then update `deploy/agents.dev.json` with the exact returned numeric
source versions. Do not guess those versions or edit old published versions.
Use the reviewed dev delivery workflow for the backend and source changes
together. Finish acceptance in a signed-in UI with `Chicken dinner` and the
input formats above; confirm recipe instructions, honest USDA coverage and
history reload. Offline transport/browser fixtures do not prove live agent
interpretation, credentials, tool identity or deployment acceptance.

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
never displayed or persisted, including on the text-only path. ASC-38's executor
selects the corresponding control kind rather than rendering coordinator prose.

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
now **5**, with unchanged draft schema **2**; publish/pin the reviewed phase sources
before live composition so the specialist knows the supported output grammar.
No agents, runtime flags, deployment pins or Azure resources change automatically.
The gate is adapter-independent and applies equally to Web IQ-informed and
ordinary non-web recipes.

## Source publication and setup

`recipe_definition.py` versions the specialist policy and builds the pinned SDK's
`PromptAgentDefinition` with GPT-5.4's configured deployment and an explicit
`RaiConfig`. Each phase persists its entire configuration:

| Phase | Published tools / selection | Output | Runtime pin |
| --- | --- | --- | --- |
| `generation` | No tools / `none` | Strict `RecipeDraft` | `ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_VERSION` |
| `retrieval` | Only recipe MCP / `required` | Strict `RecipeDraft` | `ASC_FOUNDRY_RECIPE_NUTRITION_RETRIEVAL_AGENT_VERSION` |
| `web` | Native Bing search / `required` | Strict `WebDraft` | `ASC_FOUNDRY_RECIPE_NUTRITION_WEB_AGENT_VERSION` |
| `calorie` | Tool-free AI calorie recovery / `none` | Strict `WebDraft` | `ASC_FOUNDRY_RECIPE_NUTRITION_CALORIE_AGENT_VERSION` |

These are immutable source versions of the same specialist identity.
Do not enable all tools on a single source and rely on prompt instructions to
isolate phases. Pinned Responses calls inherit source configuration; they cannot
send request-time tool overrides alongside `agent_reference`.
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

An authorized owner can explicitly publish each specialist phase. Coordinate one
owner and run one command at a time; do not replay a failed or ambiguously completed
publication. Publish retrieval and web first, generation last, so default bootstrap
inspection compares the latest version with the generation source:

```powershell
$previousEnvironment = $env:ASC_ENVIRONMENT
$previous = $env:ASC_AGENTS_ENABLED
try {
    $env:ASC_ENVIRONMENT = "dev"
    $env:ASC_AGENTS_ENABLED = "true"
    # Select exactly one phase per owner-approved run.
    python -m sous_chef.publish_orchestrator --agent recipe_nutrition --phase retrieval --approve-dev-publication
} finally {
    $env:ASC_ENVIRONMENT = $previousEnvironment
    $env:ASC_AGENTS_ENABLED = $previous
}
```

For the current combined design, publish `--phase combined` and `--phase calorie`
in separate approved runs. Verify the combined version with
`--phase combined --verify-version <printed-version>`. This keeps both native
web search and recipe MCP on the same agent, with a separate tool-free recovery
source. Verify the recovery version with `--phase calorie --verify-version <printed-version>`.

For legacy phased releases, repeat as separate approved runs with `--phase web` and `--phase generation`.
Retain each printed numeric version; publication does not assume the next number.
Read back every selected phase without writing or invoking a model:
`python -m sous_chef.publish_orchestrator --agent recipe_nutrition --phase <phase> --verify-version <printed-version>`.
Use the same temporary dev process settings for read-back. It verifies both
identities and the exact active source definition, phase metadata and fingerprint.

The shared publisher retains its historical module name and default Orchestrator
behavior. It verifies both distinct, enabled identities, checks configured pins,
then appends exactly one source version with policy/model metadata and the
definition's SHA-256 fingerprint. Recipe publication now requires the existing
Foundry IQ connection/index settings as well as the approved RAI policy.
Administrative publication uses the existing
explicit `AzureCliCredential` pattern; application runtime remains
managed-identity-first. An ambiguous publication error must be inspected before
retrying. The command changes no app runtime flags or release manifest.

The current `deploy/agents.dev.json` candidate pins read-back-verified combined
source 49 (policy 12), tool-free calorie source 51 (policy 2), and Orchestrator
source 39 (policy 7) in an enabled schema-version-5 release
(schema version 3 remains the legacy phased format). Live adapters and final
safety are composed by the backend; publishing a source or preparing this manifest
does not deploy that backend or prove end-to-end UI acceptance. Existing manually
modified combined-tool versions are retained, but do not use them unless they match
the reviewed combined contract. Deploy the compatible backend and runtime pins
together before testing a real UI request.

## One-time Foundry IQ setup for ASC-30

Reuse the [recipe knowledge settings and owner-operated refresh](../README.md#foundry-iq-recipe-knowledge).
After review, run `index_recipes refresh --configure` with the approved corpus
to add the `grounding` field and update the dedicated semantic configuration.
Existing full recipe text remains searchable and the complete normalized record
remains in `recipe_json`. The semantic grounding field contains only a bounded
excerpt plus stable source ID, pinned dataset revision and reference URL.
Search MCP can return an overview array in its first text block, using bounded
nonnegative integer `ref_id` values and generic `content` fields, followed by one
reference text block per overview entry containing `sourceData`. The decoder also
retains compatibility with string IDs and named `grounding` fields. Only reference
IDs are normalized; recipe source IDs, revisions and citations are not rewritten.
Conflicting aliases are rejected. Every attached reference must uniquely match an
overview entry and the same validated curated recipe. Supplied document keys,
source IDs, source URLs and titles are cross-checked; supplied full `recipe_json`
must reproduce the same indexed excerpt and provenance. The complete envelope,
including all reference data, counts toward its character budget.
Native Foundry invocation can instead normalize the same response into
`documents`, each containing an opaque `id`, a `url`, and JSON `content` with
the indexed grounding and source fields. This shape is normalized into the same
overview/reference validation path. Each document must include its complete source
metadata and corpus JSON. Transport URLs and display titles are opaque provider
metadata, not recipe titles or citations; provenance comes only from the validated
indexed content. Duplicate IDs/sources, conflicting source fields,
ambiguous envelopes and excessive results still fail. Budgets count the original
complete response, including transport metadata, not just the normalized excerpt.
Old or malformed MCP results fail explicitly, rather than becoming guessed citations.

Optional nonsecret bounds, also used by the setup command:

| Setting | Default / range |
| --- | --- |
| `ASC_FOUNDRY_IQ_MAX_RESULTS` | 3 / 1-3 |
| `ASC_FOUNDRY_IQ_MAX_CONTEXT_TOKENS` | 4,000 / 256-4,000 |

`ASC_ENVIRONMENT=dev` selects a separate recipe-MCP envelope allowance of 65,536
characters. MCP results are no longer rejected by token count in any environment.
The setting above still controls Search configuration and the REST retrieval
request; do not set
it to 16,000. The dev envelope allowance does not change the stored knowledge
source/base, published source fingerprint, result count, snippet limits or
provenance checks. It needs no source republication or Search refresh by itself.

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
