# Recipe + Nutrition structured output and grounding (ASC-26 / ASC-30 / ASC-34)

This slice implements the GPT-5.4 specialist's version-3 instructions, strict
draft/full-recipe schemas, one-call New Foundry generation, backend enrichment,
identity-bound Foundry IQ recipe retrieval and conditional Web IQ. It does not activate chat
or an Azure release, save/cook a recipe, integrate live USDA nutrition, or replace
the later final full-recipe safety gate. No model/tool availability research or
alternative provider is required.

## Execution and tool boundaries

`RecipeSpecialistService` implements the existing `RecipeSpecialist` protocol.
For live retrieval, construct it with `RecipeSpecialistService(gateway, None, nutrition)`,
where `gateway` is the configured `FoundryGateway` and `nutrition` is an explicit
`NutritionTool`. Passing a `RecipeGroundingTool` instead of `None` retains the
backend-supplied evidence seam for offline fixtures.
The interfaces are explicit dependencies; there is no default successful tool
implementation, fixture mode selected in dev/prod, API-key fallback, or Cosmos access.
Fixtures and offline doubles live only in `tests/fixtures/recipe-specialist.json`
and `tests/unit/test_recipe_specialist.py`. All fixture recipes, nutrition amounts
and food identifiers are synthetic, not real USDA facts.

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
4,000-token tool allowance. Live preflight reserves the configured grounding
allowance in addition to the complete input. Output is capped at the existing 1,500-token reservation,
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

Validated evidence enters recipe generation as explicitly untrusted data.
It cannot authorize tools, replace curated provenance, supply USDA nutrition
or override deterministic allergy/restriction checks. The backend attaches
the claims, native citation URLs/titles and UTC retrieval time as `webGrounding`,
separate from curated `sourceClaims`, `sources` and USDA evidence. The recipe
draft schema remains version 2. This slice does not add a citation UI or change
the requirement for the later full-recipe safety gate before display/persistence.

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
| `schemaVersion` | Exactly `2`; publish and pin the version-2 source before consuming this draft schema |
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
free-text instructions: the final deterministic full-recipe gate must still
check the latest profile and ingredient-bearing prose before display/persistence.

## Source publication and setup

`recipe_definition.py` versions the specialist policy and builds the pinned SDK's
`PromptAgentDefinition` with GPT-5.4's configured deployment, strict draft schema,
the approved recipe MCP and native Web IQ tools, and an explicit `RaiConfig`.
The source definition defaults to `tool_choice=none`; only the gateway's
stage-specific override enables the appropriate tool. Supported baseline safety is
required now; broad Responsible AI evaluation remains separate work.

Use the existing dev-only publisher and existing identity-bound agents; do not
recreate agents, assume a source version, use `latest`, or edit prompts only in
the portal. In addition to the [existing dev publication setup](orchestrator.md#reproducible-dev-source-publication),
set `ASC_FOUNDRY_RECIPE_NUTRITION_RAI_POLICY_NAME` to the approved, existing
baseline content-safety policy in the target Foundry account/project. This is a
nonsecret policy name, not a new policy provisioning/evaluation task. Publication
refuses an absent name; an invalid remote policy must fail rather than disable
safety or substitute another policy. The `.env.dev.example` value is intentionally
empty rather than guessing an account-specific policy.

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

Refresh `--configure` after changing these values so the knowledge base's MCP
defaults match the runtime policy. The backend rejects results above either
bound, oversized excerpts, duplicate references/sources, missing metadata and
unexpected provenance. It never silently truncates returned citation metadata.
The service remains minimal/extractive, with no additional retrieval LLM.

Keep the connection on `AgenticIdentityToken` and grant Search Index Data Reader
only to the distinct Recipe + Nutrition principal. No Search administrative
roles or Search tokens are added to the web workload. Publish the new specialist
source and peer-review/pin its returned version in the later enabled release.
No setup, indexing, agent publication, RBAC change or dev rollout runs automatically.

For approved dev acceptance, use that version through the same gateway/service,
with the real nutrition adapter when available. Choose groceries present in the
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
```

These are schema/policy/transport contract checks using scripted model responses,
not evidence of GPT-5.4 recipe quality, deployed content-filter configuration or
live USDA/Foundry IQ/Web IQ access. They cover groceries, substitutions, time/skill,
missing/partial grounding, hard restrictions, exact nutrition mappings, strict
SDK output, identity/version publication, budgets, cancellation and safe errors.
Web fixtures also exercise zero-call pantry routing, minimized queries, native
citations, tool/query allowlists, web failures/deadlines, nested usage and allergy
precedence. No live search, agent publication or deployment is performed.
