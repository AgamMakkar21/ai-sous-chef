# Orchestrator routing and bounded revisions (ASC-25 / #23)

This slice implements the coordinator policy with mocked specialist/tools.
[ASC-26's Recipe + Nutrition specialist](recipe-specialist.md) now implements its
typed seam with a full structured recipe and explicitly injected tool interfaces.
ASC-38 / #39's [recipe-chat executor](conversations.md#executor-setup) attaches the
live grounding/nutrition tools and composes this service with durable chat
persistence and the final display gate. Release activation remains separately reviewed.

## Source and execution contract

`sous_chef.orchestrator_definition` contains the GPT-5.4-mini instructions and
the reproducible New Foundry `PromptAgentDefinition`. Its only tool is the
strict client-side `recipe_specialist` function. Runtime uses the existing pinned
New Foundry project Responses v1 integration, not classic agents, direct-model
calls, a second orchestration framework, or an invented connected-agent API.
The deployment name is separate from the model identifier.

`FoundryGateway.coordinate()` returns either one validated function call or
one structured answer (`clarification`, `unable`, `accept`). It rejects unknown
functions, parallel/multiple actions, malformed arguments, refused/incomplete
responses, and missing usage. The runtime tool list is restricted to that one
function. Provider automatic retries remain disabled.
The source definition selects the versioned blocking
[`asc-responsible-ai-v1` content-safety policy](responsible-ai.md). Tool arguments
also reject duplicate keys and excessive transport size; control answers require
one completed output-text part.

The backend dispatches the function through the typed `RecipeSpecialist` seam.
It always supplies the **same complete `RequestContext`**, including hard
restrictions, rather than trusting a model-generated context summary. Only
authorized backend context from ASC-23 belongs here; this service does not
accept a user ID, select a Cosmos partition, or read provider conversation history.

Function results are returned using native `function_call_output` items with
their original call ID. The next request includes the bounded context, that
call and its result, not a `previous_response_id` or provider conversation.
`store=false` and requested encrypted reasoning items support stateless reasoning
models. Only bounded opaque encrypted state is forwarded in memory; plaintext
reasoning summaries are discarded and never logged, exposed, or persisted.
Missing required opaque state fails explicitly.

The instructions require clarification for missing/contradictory requirements
or an impossible cooking-time request. Current explicit direction wins over
learned preferences and old history. A current request can override soft defaults,
but cannot override stored allergies/restrictions or modify profile state.
The original time budget selected during routing cannot be silently increased
on a revision. The Orchestrator owns fit review; it does not invent nutrition.

## Backend limits and candidate validation

The frozen policy in `domain/orchestrator.py` is version 2:

| Limit | Value |
| --- | --- |
| Total model invocations | 4 |
| Specialist invocations | 2: initial plus one revision |
| Total tool calls, including retries and specialist handoffs | 8 |
| Web IQ | At most once for justified current information; 10-second search ceiling |
| Whole-operation deadline | 60 seconds, or the earlier caller deadline |
| Input tokens per invocation / cumulative | 16,000 / 32,000 |
| Output tokens per invocation / cumulative, including reasoning | 1,500 / 6,000 |

`ExecutionBudget` is request-local and shared across both specialist attempts.
Adapters must reserve each external tool attempt using `budget.tool()` **before**
execution. Foundry IQ recipes, USDA nutrition and conditional Web IQ are the
specialist's allowlist; arbitrary operations and Cosmos are denied.
The live specialist implementation must honor the supplied input/output ceilings,
total deadline and budget, return provider-counted usage including reasoning, and
never hide nested model/tool calls. An autonomous managed specialist tool loop
that cannot be intercepted/accounted is not a valid implementation of this seam.
Neither slice supplies a fake production specialist or silently selects fixture tools.

Coordination input is preflighted with the existing `o200k_base` tokenizer,
source instructions, function schema, current function result/opaque state,
and framing allowance. The function result itself must fit the 4,000-token
aggregate tool-result allowance. Provider usage is required and cumulative usage is checked
after every invocation. The specialist receives the remaining input allowance
and output reservation before execution. Missing/mismatched accounting is an
implementation error, never a successful fallback. Native coordination has its
own 131,072-character transport ceiling so ASC-23's token-bounded context is not
rejected by the original gateway's 16,000-character limit. No hard constraint is
truncated to fit; oversized requests fail explicitly.

The specialist returns a strict, bounded **internal recipe projection**: title,
total minutes, ingredient IDs/names, instructions referencing those IDs, and
brief fit reasons. This is not the eventual full nutrition/provenance/persistence
schema. ASC-26 additionally carries the full structured candidate separately;
only the bounded projection is inserted into the coordinator's function result.
Duplicate/undeclared ingredient references are rejected. The backend
uses the existing deterministic ingredient validator against stored restrictions;
blocked/unknown ingredients and over-budget times cannot be accepted by the LLM.
Raw rejected candidates are not fed back; the Orchestrator gets a rejection
category and may clarify, stop, or request its one revision.

The normal path is route -> specialist -> fit review (three model calls).
For [conditional Web IQ](recipe-specialist.md#conditional-web-iq), the strict
routing function additionally supplies a minimized public `web_query`, or null
when `use_web=false`. The specialist's isolated web invocation counts toward the
same four-model limit; a successful web path leaves no additional revision call.
Native sources and claims remain in the full recipe, not its fit-review projection.
If a revision is requested, its specialist invocation is the fourth and last:
the backend rechecks its projection and returns either a candidate with brief
fit reasons or an explicit rejection, **not** another unbounded review loop.
`status=candidate` is intentionally not a durable safety certificate.
[ASC-36's final gate](recipe-specialist.md#final-recipe-gate-asc-36) now checks
every full structured candidate after enrichment and every revision, including
ingredient-bearing instructions and cooking policy. It rejects a projection
that does not match the full recipe. One rejection can use the existing single
revision, never a separate model budget; exhausted safety attempts return a fixed
`unable` response without a recipe. ASC-38 passes the full recipe into the
chat completion hook, which rereads the current owned profile before rendering
and persistence. Its executor rejects projection-only results rather than
turning them into a successful `ValidatedReply`.

Specialist failures are not retried. The executor preserves uncertain specialist
transport outcomes when the Orchestrator wraps them, instead of marking them
known retryable failures. Deadline/cancellation cancels local awaiting
work and starts no replacement run; it does not prove a remote operation stopped.
The existing chat outcome-unknown/reconciliation rules still apply. Safe errors
carry only an allowlisted code and opaque operation ID. Operational events never
contain requests, allergies, candidate text, tool payloads or hidden reasoning.

Coordination requires separate pinned Orchestrator, Recipe + Nutrition and
App Service principal IDs. `orchestrator_invoked` records the operation trace,
logical role, pinned name/version and configured Entra principal. This attributes
the intended identity; readiness/identity binding checks and an approved live
smoke remain necessary to prove the actual remote caller and downstream tool
identities. No development credential fallback is added to runtime.

## Reproducible dev source publication

No Azure changes occur merely by importing this module or merging this ticket.
The existing `deploy/agents.dev.json` remains agent-disabled until both source
agents and tools are ready. The same publisher also supports
`--agent recipe_nutrition`; see the specialist setup notes for its required baseline
RAI policy and schema.

### First-time creation when the project has no agents

Use `sous_chef.bootstrap_agents` rather than creating placeholder agents or
reconstructing their tool definitions in the portal. It uses the same definition
builders and policy/model/fingerprint metadata as the version publisher. In the
New Foundry object model, first creation provisions each agent's own Entra Agent
identity; the bootstrap reads both identities back and rejects missing, shared,
disabled, workload-conflicting or incorrectly pinned identities.

Before running, use the reviewed dev checkout and its locked Python 3.12
environment. Configure the existing ignored `.env.dev` (never commit it):

- Keep the existing valid dev authentication, live Cosmos and model settings;
  the command uses the application's settings validator but does not access user data.
- Set `ASC_FOUNDRY_PROJECT_ENDPOINT` and `ASC_FOUNDRY_PROJECT_RESOURCE_ID` to
  the same owner-approved dev project. Confirm the target resource is dev before
  running; naming a local environment `dev` does not prove the Azure target is dev.
- Choose two distinct runtime names in `ASC_FOUNDRY_ORCHESTRATOR_AGENT_NAME`
  and `ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_NAME`. For example,
  `sous-chef-orchestrator-dev` and `sous-chef-recipe-nutrition-dev`. The model
  deployment settings identify the already deployed GPT-5.4-mini and GPT-5.4 models.
- Set `ASC_APP_WORKLOAD_PRINCIPAL_ID` to the dev App Service workload principal,
  not its client ID. Initial agent versions/principal pins are not required.
  Existing principal pins, if supplied, must match the agents; do not remove a
  verified pin merely to suppress a mismatch.
- Supply all [Foundry IQ recipe knowledge settings](recipe-specialist.md), including
  the reviewed MCP connection. Set
  `ASC_FOUNDRY_RECIPE_NUTRITION_RAI_POLICY_NAME=asc-responsible-ai-v1` only after
  the owner has applied and verified the account policy. Bootstrap constructs both
  complete definitions before creating either agent; missing specialist setup
  must not leave a newly created Orchestrator.

Sign in with the approved project-scoped publisher account using Azure CLI, then
run this command in the activated Python environment. This administrative
`AzureCliCredential` is not an application runtime credential. Keep the App
Service's agent flag and the checked-in manifest disabled:

```powershell
$previousEnvironment = $env:ASC_ENVIRONMENT
$previousAgents = $env:ASC_AGENTS_ENABLED
try {
    $env:ASC_ENVIRONMENT = "dev"
    $env:ASC_AGENTS_ENABLED = "true"
    python -m sous_chef.bootstrap_agents --approve-dev-bootstrap
} finally {
    $env:ASC_ENVIRONMENT = $previousEnvironment
    $env:ASC_AGENTS_ENABLED = $previousAgents
}
```

The temporary process flag authorizes this command's configuration only.
The script creates the missing Orchestrator with its reviewed function declaration,
and the missing specialist with its tool-free generation configuration, strict
output schema and RAI references. Publish the specialist's retrieval and web
phase versions separately before enabling chat; see
[specialist source publication](recipe-specialist.md#source-publication-and-setup).
Publication
resolves each approved policy name to its full ARM resource ID on the account
identified by `ASC_FOUNDRY_PROJECT_RESOURCE_ID`, and requires that project ID to
match `ASC_FOUNDRY_PROJECT_ENDPOINT`. Keep the short policy name in
`ASC_FOUNDRY_RECIPE_NUTRITION_RAI_POLICY_NAME`; the command constructs the reference.
It does not
deploy models, grant RBAC, configure Search connections, invoke models/tools,
delete agents, change endpoint routing or update App Service/manifest settings.
The returned source agents should appear in the same Foundry project's agent
list after refresh. The portal cannot add/edit function declarations; they are
installed by the SDK definition, not by selecting A2A or Connected agents.

On success, retain the printed exact names/numeric generation source versions.
Publish and verify the two additional specialist phase versions, then include all pins in a
peer-reviewed agent-enabled manifest change as described in
[dev delivery](dev-delivery.md#release-stages); do not treat the displayed JSON
as an already activated release. The printed principal IDs let the owner assign
required downstream access. In particular, grant Search Index Data Reader to
the Recipe + Nutrition identity only and retain the connection's
`AgenticIdentityToken` authentication. Verify the required permitted/denied tool
access and a signed-in recipe request after the reviewed deployment. Source and
identity read-back alone are not proof of live model/tool execution.

Creation is not an atomic two-agent transaction and it is not protected against
concurrent publishers. Coordinate one owner run at a time. A failed or lost create
response can leave an agent behind; no automatic create retry or cleanup occurs.
Failures report the local request step, agent role, SDK exception type and HTTP
status when available, without printing provider messages or response bodies.
Use those diagnostics to distinguish request rejection, authentication/access
failure and transport failure; do not assume every failure is a permissions issue.
Inspect the project before rerunning. A later explicit run reuses an existing
active source version only if its actual definition and metadata exactly match
the current reviewed source, its numeric version is read back explicitly, and its
identity passes validation. It creates only the remaining missing agent and does
not append duplicate versions. An unrelated, changed, draft or disabled agent
fails before new creation; use the reviewed version publisher for intentional
updates rather than weakening bootstrap checks.

For a read-only comparison, use `python -m sous_chef.bootstrap_agents --inspect`
with the same temporary dev process settings. This reads each named agent and
its latest numeric version, then reports status, metadata mismatch keys and
definition difference paths without printing definition values. It never creates
or updates versions. Investigate any returned differences before retrying;
server normalization must not hide a missing policy, tool or instruction.

The curated recipe MCP allowlist uses the explicit filter object
`{"tool_names":["knowledge_base_retrieve"]}` rather than its array shorthand.
Foundry normalizes the shorthand on read-back, which would otherwise make the
stored definition differ from its submitted-source fingerprint. If an initial
specialist version was published with the old shorthand, retain the agent and
its identity, confirm the retrieval-only filter with `--inspect`, and have the
owner intentionally publish the corrected retrieval source with
`python -m sous_chef.publish_orchestrator --approve-dev-publication --agent recipe_nutrition --phase retrieval`.
Verify its exact version with `--phase retrieval --verify-version <source-version>`.
The default bootstrap inspection compares the latest version with the generation source;
use `--inspect --phase retrieval` only when inspecting a latest retrieval version.
Do not ignore fingerprint
drift or append replacement versions automatically during bootstrap.

An authorized owner can publish this source to an **existing**, enabled dev
Orchestrator without recreating/rebinding its Entra identity:

1. Use the reviewed dev checkout and locked Python dependencies. Configure the
   approved dev project endpoint and `ASC_FOUNDRY_PROJECT_RESOURCE_ID`, both
   existing agent names, the Orchestrator's GPT-5.4-mini
   deployment name and the App Service workload principal through the existing
   `.env.dev`/environment settings. Both agent identities must be distinct from
   each other and the app; configured principal pins, when present, must match.
   The command refuses local/prod targets, shared identities and disabled agents.
   Have the owner apply and verify `asc-responsible-ai-v1` in the target Foundry
   account before publication; compiling the policy is not deployment evidence.
2. Sign in with an authorized publisher account. This administrative command uses
   `AzureCliCredential`, just like the existing delivery publisher; it is **not**
   an app runtime credential. It needs project-scoped agent version publication
   rights, not subscription Contributor or new Cosmos privileges.
3. Explicitly approve publication in this process only:

   ```powershell
   $previous = $env:ASC_AGENTS_ENABLED
   try {
       $env:ASC_ENVIRONMENT = "dev"
       $env:ASC_AGENTS_ENABLED = "true"
       python -m sous_chef.publish_orchestrator --approve-dev-publication
   } finally {
       $env:ASC_AGENTS_ENABLED = $previous
   }
   ```

The temporary process setting permits source inspection/publication; it does
not change the App Service runtime flag or release manifest. The command appends
one version under the existing agent, with policy version, model identifier and
a SHA-256 fingerprint of the exact source definition. It reports the returned
numeric source version without printing the endpoint, credentials or user data.
It does not invoke models, attach the specialist's tools, or activate a release.
Publication failures are explicit; do not blindly retry an ambiguous create
without inspecting whether a version was already created.

When both agents/tool adapters are approved, pin their actual source versions
in a peer-reviewed agent-enabled manifest change. The existing
[dev delivery workflow](dev-delivery.md#release-stages) then copies those exact
definitions and updates runtime pins automatically. Do not edit prompts only in
the portal, use `latest`, invent version `1`, or flip a manual app runtime flag.
The usual one-time owner/OIDC/RBAC setup and approved managed-identity invocation
smoke still apply. Production promotion is not part of this ticket.

## Offline coverage

```powershell
python -m pytest tests\unit\test_orchestrator.py tests\unit\test_orchestrator_gateway.py tests\unit\test_foundry_gateway.py
```

`tests/fixtures/orchestrator.json` defines valid routing, clarification,
contradictory input, impossible time and explicit/learned-conflict contracts.
Tests use scripted model decisions, mocked specialists/tools, and the real pinned
SDK request/response transport. They cover native handoff, identity attribution,
bounded errors/rejections, conditional Web IQ, token/call limits, cancellation,
and reproducible source publication. These are contract/policy tests, not evidence
of live model interpretation quality or deployed tool access.
