# Recipe + Nutrition structured output (ASC-26 / #30)

This slice implements the GPT-5.4 specialist's version-1 instructions, strict
draft/full-recipe schemas, one-call New Foundry generation, backend enrichment,
and offline recipe/nutrition tool fixtures. It does not activate chat or an Azure
release, save/cook a recipe, attach live tools, implement Web IQ (#34), or replace
the later final full-recipe safety gate. No model/tool availability research or
alternative provider is required.

## Execution and tool boundaries

`RecipeSpecialistService` implements the existing `RecipeSpecialist` protocol.
Construct it with `FoundryGateway`, a `RecipeGroundingTool`, and a `NutritionTool`.
The interfaces are explicit dependencies; there is no default successful tool
implementation, fixture mode selected in dev/prod, API-key fallback, or Cosmos access.
Fixtures and offline doubles live only in `tests/fixtures/recipe-specialist.json`
and `tests/unit/test_recipe_specialist.py`. All fixture recipes, nutrition amounts
and food identifiers are synthetic, not real USDA facts.

Execution is deterministic around one model invocation:

1. Reserve an allowlisted `foundry_iq_recipes` call on the supplied request-local
   budget and retrieve up to three cited, versioned snippets (1,500 characters each).
   No grounding means `foundry_unavailable`, with no model or nutrition invocation.
2. Send the original complete `RequestContext`, specialist requirements and
   untrusted grounding to the pinned Recipe + Nutrition managed agent. The
   gateway requires the distinct agent/workload identity pins. It uses Responses
   v1, `agent_reference`, `store=false`, strict JSON Schema, `tools=[]` and
   `tool_choice=none`. It never uses provider-managed conversations, direct-model
   calls or an autonomous/unaccounted tool loop.
3. Validate the draft, its actual cited source IDs, time budget, declared skill
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
ingredient restrictions yield `foundry_rejected`. Service/auth/throttling failures
remain distinct. Tool adapters raise `RecipeToolError` with a safe `FoundryFailure`
category, not a raw upstream body. A returned `status=unavailable` nutrition result
is valid and explicit, not zero nutrition; an actual tool failure stops the request.

The supplied finite deadline covers retrieval, generation and nutrition together,
with a 60-second ceiling. Cancellation starts no replacement invocation. The
gateway preflights instructions, schema, full context, requirements and grounding
against the remaining per-call input allowance; grounding also must fit the
4,000-token tool allowance. Output is capped at the existing 1,500-token reservation,
including reasoning. Provider usage is mandatory and checked against both ceilings;
the Orchestrator accounts it cumulatively. Nothing truncates hard constraints to
fit a budget. A `use_web=true` request fails explicitly as unavailable until #34
wires the approved conditional integration; it is not silently treated as grounded.

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
| `schemaVersion` | Exactly `1` |
| `title`, `servings`, `totalMinutes`, `skill` | 160 characters; 1-20 servings; 1-1,440 minutes; existing skill enum |
| `ingredients` | 1-30 unique line IDs, display names, numeric quantity or null, supported unit or null |
| `substitutions` | Up to 30 original names/reasons referencing replacement lines actually used in the recipe |
| `instructions` | 1-30 sequential steps, text up to 500 characters, declared ingredient references; every ingredient is referenced |
| `fitReasons`, `adaptations` | Up to 10 entries each, 240 characters each; at least one fit reason; adaptations distinguish creative choices from cited source content |
| `sourceIds` | 1-3 unique curated source IDs actually supplied by retrieval |
| Enriched ingredients | Backend-resolved canonical ID or null, `available/missing/unknown`, and supporting grocery row IDs |
| `shoppingItems` | Exactly the missing ingredient lines, with shortage amounts where determinable |
| `nutrition` | `known/partial/unavailable`, serving basis/count, attributed nutrient amounts, exact ingredient mappings, USDA source/version/food IDs, informational-estimate/not-medical-advice notice |
| `sources` | Up to 12 references, copied from the cited recipe and nutrition tool evidence, never generated by the model |

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
no callable tools and an explicit `RaiConfig`. Supported baseline safety is
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
definition's SHA-256 fingerprint. Administrative publication uses the existing
explicit `AzureCliCredential` pattern; application runtime remains
managed-identity-first. An ambiguous publication error must be inspected before
retrying. The command changes no app runtime flags or release manifest.

Keep `deploy/agents.dev.json` disabled until live tool adapters and chat/final
safety composition are ready. Later work must inject those adapters, retain
budget enforcement and identity separation, and pin the approved source version
in a peer-reviewed release. No deployment or live grounding was performed by
implementing this slice.

## Focused offline checks

With the locked Python 3.12 environment from the repository setup:

```powershell
python -m pytest tests\unit\test_recipe_specialist.py tests\unit\test_orchestrator.py tests\unit\test_orchestrator_gateway.py tests\unit\test_foundry_gateway.py
```

These are schema/policy/transport contract checks using scripted model responses,
not evidence of GPT-5.4 recipe quality, deployed content-filter configuration or
live USDA/Foundry IQ access. They cover groceries, substitutions, time/skill,
missing/partial grounding, hard restrictions, exact nutrition mappings, strict
SDK output, identity/version publication, budgets, cancellation and safe errors.
