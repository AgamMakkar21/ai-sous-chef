# Orchestrator routing and bounded revisions (ASC-25 / #23)

This slice implements the coordinator policy with mocked specialist/tools. It
does **not** activate live recipe chat, implement Recipe + Nutrition, attach its
grounding/nutrition tools, persist a recipe, or approve a final recipe for display.
Those are separate tickets; ASC-38 / #39 composes the live request.

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

The frozen policy in `domain/orchestrator.py` is version 1:

| Limit | Value |
| --- | --- |
| Total model invocations | 4 |
| Specialist invocations | 2: initial plus one revision |
| Total tool calls, including retries and specialist handoffs | 8 |
| Web IQ | At most once, only when requested conditionally |
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
This ticket deliberately supplies no fake production specialist.

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
schema. Duplicate/undeclared ingredient references are rejected. The backend
uses the existing deterministic ingredient validator against stored restrictions;
blocked/unknown ingredients and over-budget times cannot be accepted by the LLM.
Raw rejected candidates are not fed back; the Orchestrator gets a rejection
category and may clarify, stop, or request its one revision.

The normal path is route -> specialist -> fit review (three model calls).
If a revision is requested, its specialist invocation is the fourth and last:
the backend rechecks its projection and returns either a candidate with brief
fit reasons or an explicit rejection, **not** another unbounded review loop.
`status=candidate` is intentionally not final recipe approval. ASC-38 must
validate the complete final recipe, including ingredient-bearing instructions,
nutrition/source contracts and the latest profile state, before display,
streaming or persistence. It must not treat this projection as `ValidatedReply`.

Specialist failures are not retried. Deadline/cancellation cancels local awaiting
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
agents and tools are ready.

An authorized owner can publish this source to an **existing**, enabled dev
Orchestrator without recreating/rebinding its Entra identity:

1. Use the reviewed dev checkout and locked Python dependencies. Configure the
   approved dev project, both existing agent names, the Orchestrator's GPT-5.4-mini
   deployment name and the App Service workload principal through the existing
   `.env.dev`/environment settings. Both agent identities must be distinct from
   each other and the app; configured principal pins, when present, must match.
   The command refuses local/prod targets, shared identities and disabled agents.
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
