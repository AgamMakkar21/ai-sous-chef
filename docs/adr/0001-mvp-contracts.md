# ADR 0001: MVP architecture and implementation contracts

- Date: 2026-10-02
- Status: Proposed
- Scope: MVP architecture, implementation conventions, and product behavior.

## Context and references

AI Sous Chef combines a three-tab cooking experience with grounded recipe
generation and feedback-based personalization. These contracts keep application
state, agent execution, and asynchronous learning distinct.

The product baseline is *[AI Sous Chef - Detailed Technical Overview](../references/technical-overview.md)*,
sections 1-59, and the [AI Sous Chef architecture diagram](../references/architecture-diagram.png).
The diagram is retained as a PNG rendered from the supplied single-page PDF.
Its components, model assignments, and preference-read boundary are reflected
below. Diagram alignment is not evidence of deployed service availability.

Memory design follows Microsoft's
[Agent Memory in Azure Cosmos DB for NoSQL](https://learn.microsoft.com/en-us/azure/cosmos-db/gen-ai/agentic-memories)
and [partitioning guidance](https://learn.microsoft.com/en-us/azure/cosmos-db/partitioning)
as reviewed on 2026-10-02: one bounded document per complete exchange, with
retrieval and partitioning chosen for the application's access patterns.
These sources do not prescribe the
product-specific confidence formula, numeric collection/context ceilings,
or 180-day learning window.

This ADR and the [API/schema outline](../contracts/api-and-schemas.md) define the
proposed design, not a claim that the application or integrations are implemented.
The overview summarizes product intent with its original section numbering;
the architecture diagram is included as an image. These contracts govern the
precise implementation choices.

## 1. Architecture that implementation must preserve

The authenticated browser communicates with one Python Azure App Service for all
product data. The same App Service serves the frontend and API. Browser redirects
to Microsoft Entra External ID for authentication are not direct product-data
access. Never give the browser Cosmos, Foundry, or USDA credentials.

| Component | Required responsibility |
| --- | --- |
| Microsoft Entra External ID and App Service authentication | Consumer identity; backend derives trusted user scope |
| Python Azure App Service | Browser boundary, authorization, CRUD, frontend assets, context construction, agent invocation, validation, persistence |
| Cosmos DB for NoSQL `Users` | One bounded current-state profile per user, including groceries and separate explicit/learned preferences |
| Cosmos DB for NoSQL `ChatHistory` | Separate conversation metadata, bounded complete-exchange turn documents, and send-operation records; never a lifetime array in `Users` or one ever-growing conversation document |
| Cosmos DB for NoSQL `RecipeHistory` | Separate immutable recipe snapshots, cooking state, feedback revisions, behavioral events, and processing provenance |
| Cosmos change feed and Azure Functions | Asynchronous, note-led preference extraction from eligible `RecipeHistory` events using GPT-5.1 nano; separate lease storage |
| Microsoft Foundry (new) Agent Service and Python Foundry SDK 2.x | Versioned managed agents, approved tool connections, grounding, evaluations, tracing; no Foundry classic integration |
| Orchestrator Agent | Interpret request, coordinate the specialist, check fit, request bounded revision |
| Recipe + Nutrition Agent | Ground and adapt recipes; identify missing ingredients; obtain nutrition |
| Foundry IQ | Retrieve curated recipe knowledge from the specified Hugging Face dataset |
| USDA MCP | Authoritative nutrition through the specified PostgreSQL-backed integration |
| Web IQ | Conditional current/external grounding, not mandatory for every request |
| Application Insights, Log Analytics, Azure Monitor, OpenTelemetry | Correlated operations, dependencies, performance, failures, and resource utilization |
| Managed identities, Entra Agent ID, and Azure RBAC | Secretless authentication by default, separate workload identities, and a distinct identity for each interactive agent |

Required recipe source:
<https://huggingface.co/datasets/untitledwebsite123/food-recipes/viewer/default/train?p=5225>.
Required USDA integration: <https://github.com/HemantaPatil/mcp_postgres>.
These links identify intended sources, not approved licenses, pinned revisions,
hosted endpoints, or proven remote transport.

There is no second orchestration framework, separate frontend hosting service,
Container Apps migration, substitute nutrition API, or model-memory nutrition
fallback in this architecture.

Grocery, profile, onboarding, and explicit-preference CRUD never invokes AI or
starts a learning Function. Saving feedback finishes when its database write
succeeds; the response never claims that learning has already completed.

Explicit allergies and strict restrictions are non-negotiable. Current requests
may override soft defaults such as time or taste, but not safety constraints.
Explicit user direction overrides conflicting learned preferences. A model never
creates, removes, or modifies explicit preferences or allergies.

The complete final recipe, including substitutions, garnishes, and ingredient-
bearing instructions, passes a deterministic safety gate using current profile
state. Unknown or ambiguous ingredients fail closed. Revalidate after a
mid-request safety change and on revisions or reuse of saved recipes. Do not
stream unvalidated recipe content.

### New Foundry and SDK contract

Microsoft Foundry **(new)** is required throughout provisioning, agent execution,
model access, tools, evaluation, and tracing. This is an SDK/API and resource-model
requirement, not just a portal preference. Use the current Foundry resource/project
and new agent object model, not a classic hub-based or legacy Agent Application
implementation.

Use Python `azure-ai-projects` **2.x**, at least 2.3.0, with `AIProjectClient`,
`azure-identity`, and a compatible `openai` package (at least 3.0.0). Manage versioned
agents through the project client and use its `get_openai_client()` integration
for Responses and, where needed, Conversations. The learning Function uses the
same New Foundry client/authentication baseline for its model invocation; it is
not a separately provisioned interactive agent.

Do not use Python `azure-ai-projects` 1.x, the classic Python `azure-ai-agents`
client, or Assistants/Threads/Runs APIs. Do not silently replace managed agents
with direct model-only Chat Completions or add a second orchestration framework.
Pin tested exact SDK/package and API versions during normal dependency setup and
scaffolding. The New Foundry SDK and required capabilities are assumed to work;
do not add feasibility research or capability-discovery gates. Record preview
dependencies, test the implementation wiring, and surface runtime failures
without downgrading to classic Foundry.

This baseline follows Microsoft's [SDK overview](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview),
[Python client reference](https://learn.microsoft.com/en-us/python/api/overview/azure/ai-projects-readme?view=azure-python),
and [new agent object model](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/migrate-agent-applications),
reviewed on 2026-10-02. It does not claim these integrations are already deployed.

### Workload and agent identities

Managed identity and least-privilege Azure RBAC are the default for Azure-hosted
service-to-service access. App Service and the learning Function have separate
identities and role assignments; deployment/admin access is separate from runtime
data-plane access. The Function's learned-state-only write boundary must also be
enforced in code: Cosmos RBAC does not restrict writes to individual JSON properties.

The Orchestrator and Recipe + Nutrition agents must each have a distinct Entra
Agent ID from the new agent object model, not a shared legacy project agent
identity. Record each agent's Foundry identifier/version and Entra principal ID
in deployment configuration. Foundry's managed identity can authenticate its
agent identity blueprint without an application-managed secret; the resulting
agent principal is distinct from that managed identity.

Use the agent's identity for supported downstream tool authentication and grant
only required access. Verify the actual caller for each connection rather than
assuming a logical agent name proves identity isolation. Any connection that
requires a shared workload identity needs an explicit, reviewed exception.
Neither interactive agent receives direct Cosmos product-data access.

Production credential selection must use the intended managed identity, with no
fallback to developer credentials or stored service keys. WSL development uses
the developer's Entra sign-in; deployment automation uses workload identity
federation. An integration that cannot support identity-based authentication needs
a documented, reviewed exception with an owner, least-privilege scope, protected
secret storage, and rotation. Never place secrets in source, prompts, or logs.

Correlate the actual calling principal with the logical agent/version, model
deployment, provider response ID, application operation ID, and trace ID. Link Function
extraction traces to their feedback revision and Function identity. Identity audit
logs complement, rather than replace, agent/tool execution traces. Do not log
tokens or raw user content; unavailable attribution is an explicit verification
gap, not an inferred principal. See Microsoft's [agent identity guidance](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-identity).

### Agent memory and the diagram's read boundary

The diagram's agents-to-Cosmos arrow labeled "Read User Preference" expresses a
logical dependency. App Service reads fresh,
authorized user state and supplies bounded context to the agents. It is not a
direct agent database connection or permission to invent a `userId` tool argument.
Do not add a separate context-reading tool or agent Cosmos credentials.

| Memory purpose | Application representation |
| --- | --- |
| Short-term working context | Current request, up to 10 recent completed exchanges from this conversation, and bounded relevant tool results |
| Durable episodic history | Separate `ChatHistory` turns and `RecipeHistory` cooking/feedback records, retained until user-requested deletion |
| Current explicit state | Bounded `Users` groceries and user-declared preferences; authoritative over learned state |
| Consolidated learned memory | Bounded, evidence-linked `Users.learnedPreferences`, derived asynchronously from validated recipe behavior |
| External grounding | Foundry IQ, USDA MCP, and conditional Web IQ; not user preference memory |

"Short-term" describes what enters the working context, not a requirement to
delete the stored conversation. Chat has no automatic TTL.
Microsoft presents TTL, vector/full-text/hybrid retrieval, and summarization as
choices for different workloads, not mandatory services or universal defaults.

Follow the article's recommended complete-exchange storage unit: persist a
pending user turn before invocation, then conditionally complete that same item
with the validated assistant response. Never create an unbounded message/turn
array inside a conversation or profile. Keep individual items bounded, and do
not persist internal model/system prompts, hidden reasoning, or unrestricted
tool payloads as user-visible memory. User-authored chat messages are the
intentional exception and remain part of each turn. Store only necessary
bounded source/tool references.

For the MVP, retrieve recent complete exchanges with partition-scoped ordered
queries and point-read current state. Add no embedding model, vector index,
full-text index, semantic cache, or automatic conversation summarizer. Those
would need demonstrated retrieval benefit, user-isolation/deletion controls,
and evaluation. This does not remove the separately required
Foundry IQ recipe retrieval.

Recipe feedback extraction is our long-term-memory consolidation step. Its
outputs are derived candidates with source record/revision references, not new
explicit facts. Chat text, retrieved content, and remembered notes remain
untrusted data rather than instructions. Corrections, deleted evidence, and
explicit conflicts must invalidate affected learned context. Metadata-only
conversation summaries are not AI-generated memory summaries.

Cosmos is the application's durable memory authority. Foundry Responses/Conversations
state, if needed for execution, must be isolated by user/conversation and must
not bypass the app's context-selection, token, safety, or deletion policies.
Implementation tests must demonstrate that the chosen SDK/API wiring controls
provider-side history and removes retained user content; do not assume the
application enforces these policies merely because it uses that SDK.

Use `/userId` as the MVP partition key: dominant operations
are user-scoped profile reads, conversation lists, and history queries, and dev
has a small bounded concurrent workload. It is not an assertion of infinite
per-user capacity. A single logical partition currently has a 20-GB storage
limit and can become throughput-hot. Monitor partition size, RU consumption,
and throttling. Require a reviewed migration before a
history partition reaches that limit; evaluate hierarchical user/conversation
or user/history grouping if measured growth requires it. Do not solve growth
by silently deleting chat or weakening authorization. Changing a partition key
requires migration, not an in-place configuration flip.

## 2. Implementation stack and layout

| Choice | Decision |
| --- | --- |
| Development platform | Windows host with WSL 2 as the Linux development/runtime environment |
| Backend framework | FastAPI with Pydantic request/response validation |
| Frontend | React + TypeScript, built with Vite; static build served by FastAPI from the same origin |
| Production web process | Python ASGI application using Uvicorn; no production Node.js server |
| Python baseline | CPython 3.12.x for backend and learning worker |
| Foundry baseline | New Foundry only; Python SDK 2.x and Responses/Conversations as defined above |
| Frontend build baseline | Node.js 24 LTS; npm |
| Python dependencies | `pip` installation; `pip-tools` generates exact-version-pinned runtime and development requirements from `pyproject.toml`; no `uv` |
| JavaScript dependencies | Committed `package-lock.json`; reproducible installation with `npm ci` |
| Python quality | Ruff formatting/linting, mypy, pytest and HTTPX |
| Frontend quality | ESLint, TypeScript checking, Vitest and React Testing Library |
| End-to-end quality | Playwright; deterministic offline fixtures plus separately opted-in live checks |
| Styling | Plain CSS with shared tokens/components; no additional UI framework required |
| Package versions | Pin compatible exact package and tool versions in dependency manifests and lockfiles when establishing the application scaffold |

Baseline runtime compatibility must be verified against the selected Azure
hosting and SDK versions before deployment. An incompatibility requires a
reviewed contract change, not an automatic runtime/framework substitution.

Run Python, Node.js, pip/pip-tools, npm, local services, and test commands inside
WSL 2, matching the Linux CI and Azure App Service runtime. The Windows host
provides the editor and browser. Keep the normal development checkout and
dependency environments in the WSL Linux filesystem; do not share a Python
virtual environment or `node_modules` between Windows and Linux. Native Windows
execution of the backend or worker is not a required development path.

Document WSL shell setup/startup commands and how the Windows browser reaches
the local server through localhost forwarding. Tool installation and startup
must not depend on an interactive Windows-only shell. The supported environment
does not change the browser-facing API or require a different production host.

The target layout is:

```text
backend/
  src/sous_chef/
    api/             HTTP routes and DTO boundaries
    domain/          schemas, policies, safety, and service interfaces
    services/        use cases, context, generation, and learning policies
    adapters/        Cosmos, identity, Foundry, and telemetry implementations
    static/          generated frontend output; not hand-maintained source
frontend/
  src/               three tabs, onboarding/settings, shared API client
worker/
  preference_learning/ Azure Functions entry point; reuse domain contracts
tests/
  unit/
  integration/       offline adapters by default; live tests explicitly opted in
  e2e/
infra/               environment-specific infrastructure
scripts/             repeatable development, ingestion, and operational commands
docs/
  adr/
  contracts/
pyproject.toml
requirements.txt
requirements-dev.txt
```

The scaffold provides an app factory, injectable adapters, lockfiles, and
documented WSL/Linux CI commands. Add modules as their behavior is implemented
rather than creating unused implementations in advance.

## 3. Required MVP and deferred features

| Surface | Required MVP |
| --- | --- |
| Authentication and settings | Sign-in, first-time onboarding, edit explicit preferences, allergies, restrictions, nutrition goals, skill, and time |
| Groceries tab | List/add/delete groceries; known quantity increase/decrease; explicit unknown amounts; truthful pending/failure states |
| Sous Chef tab | New conversation, conversation sidebar/history, chat, personalized structured recipes, ingredients/instructions, shopping needs, nutrition status/provenance, save/cook/reject/revision actions |
| Recipe History tab | Saved-versus-cooked state, mark cooked, cooked meals awaiting rating, `liked`/`fine`/`disliked`, prominent optional notes explaining what to repeat or change, paginated title/rating history |
| Shared UX | Responsive mobile and laptop layouts, touch and keyboard access, fresh cross-tab state, and loading/empty/error states |
| Learning | Asynchronous nano-model interpretation of user notes into actionable, evidence-linked preferences; replay-safe confidence and evidence counts |

### Mobile and laptop compatibility

All primary journeys, including onboarding/settings and recipe notes, must work
on mobile phones and laptops. Use responsive layouts rather than separate mobile
and desktop applications. On narrow screens, collapse the conversation sidebar
into an accessible drawer/menu and keep all three tabs reachable. On laptop
screens, show the sidebar beside the active conversation.

Verify layouts at 375px and 390px mobile widths, an intermediate 768px width,
and 1280px and 1440px laptop widths, in CSS pixels. Content must reflow without
page-level horizontal scrolling or clipped recipe instructions. Support mobile
portrait/landscape changes and an on-screen keyboard without hiding the note
field, composer, or submit controls or losing unsaved input.

Support touch, mouse, and keyboard without hover-only actions. Use visible labels,
focus states, and at least 44-by-44 CSS-pixel primary touch targets. Ratings must
not rely on color or emoji alone. Support current stable Chrome, Edge, Firefox,
and Safari on laptops, plus Safari on iOS and Chrome on Android. Browser-engine
and mobile-emulation checks complement, rather than replace, representative
real-phone and laptop checks.

Deferred: receipt scanning/uploads, recipe images, voice, sharing, timers,
multiple-recipe comparison, expiration dates, grocery categories, automatic
quantity/unit/alias normalization, favorites, advanced history filtering/search/
statistics, advanced preference explanations, and rich history-detail screens.
The recipe-detail API remains required; a rich detail UI is not.
Do not implement a new self-service account-management UI for operational deletion.

## 4. Grocery and recipe lifecycle decisions

Grocery entries have server-issued stable IDs independent of ingredient names.
An unknown quantity is `null`, not zero or an assumed one. A known quantity is
strictly positive. Unknown amounts require an explicit amount before +/- is used.

If decrement would reach zero or below, ask for removal confirmation. Confirmation
sends a conditional delete; cancellation makes no mutation. The API rejects a
zero/negative quantity update; it never silently deletes or stores zero.

An exact duplicate is the same Unicode-casefolded, trimmed name and the same
selected unit, including `null`. Reject it with a conflict and identify the
existing owned row. Different units remain separate. Do not merge quantities,
equate synonyms, convert units, or silently relabel user input.

Generated suggestions are not automatically cooking history or preference
evidence. `Save to cook` creates an immutable owned snapshot and records intent,
without a cooked timestamp or rating. `Mark cooked` records the user's explicit
confirmation and server timestamp. Only cooked, unrated records appear in
`Needs your rating`. Ratings never infer cooking from an earlier save.

Saved records remain visible in history. Rejection and revision requests are
deliberate behavioral signals, not cooked meals. Abandonment is not rejection.
Revision suggestions follow the same generation and safety checks as initial
suggestions; browser-supplied recipe JSON is never trusted as a snapshot.

## 5. Learning policy

Learned personalization requires at least **two supporting distinct cooked meals**
and **confidence at least 0.65**. Saves, rejections, and revision requests
contribute **one-third** of a cooked-feedback weight.

Recipe notes are the primary source of specific, actionable learned preferences.
The Azure Function invokes GPT-5.1 nano to interpret the current note alongside
the recipe, overall rating, explicit constraints, and bounded relevant prior
feedback. The rating supplies broad context; it must not drown out a more
specific statement in the note. Notes remain optional, but the UI prominently
invites what the user liked and what they would change.

Preserve the full current note within its validated input limit. If that note
and mandatory context cannot fit the extraction request, report an explicit
processing failure instead of silently dropping the note or reverting to a
rating-only inference. Extract actionable preferences with source/revision
references and supporting note excerpts, not just a generic sentiment label.
Ambiguous, contradictory, or unsupported text produces no directional evidence
for that insight rather than an invented preference.

| Overall rating and note | Interpretation for the specific preference |
| --- | --- |
| `disliked`: "Disliked the meal, but loved the lime sauce." | Support a citrus-forward preference; the negative whole-meal rating does not reverse that specific evidence |
| `fine`: "Fine, but too salty." | Support lower-salt preparations despite the neutral overall rating |
| `liked`: "Loved the citrus, but wanted more heat." | Support citrus and greater heat as separate insights, once per insight for this occasion |
| `disliked`: "I burned the rice." | Do not infer a dislike of rice from an execution problem |

A note-derived insight remains learned state: it cannot change explicit profile
fields, infer an allergy or medical condition, bypass safety constraints, or
skip the two-cooked-occasion/0.65 eligibility gate.

Implementation policy:

1. Extract candidate insights and supporting/opposing references; validate every
   reference against owned, current-version history. Model-proposed counts and
   confidence are not authoritative.
2. Use the most recent 100 independent recipe occasions with eligible behavioral
   evidence in the preceding 180 days. Order by evidence timestamp, then ID.
   This is a bounded learning window, not a product-history deletion policy.
3. For a specific insight, attributable feedback from a cooked occasion supplies
   3 support or opposition units. A clear note determines direction for that
   insight even if the overall rating is `fine` or points the other way.
   Without specific note evidence, `fine` is neutral and an overall rating can
   support only an attributable broader conclusion, not every ingredient/flavor.
4. An attributable intentional save, rejection, or revision request supplies 1
   support or opposition unit. A save is acceptance evidence, not proof that
   every ingredient is liked. Reject/revision direction must be tied to the
   relevant preference, not guessed from an unexplained action.
5. Allow only one contribution per insight per recipe occasion. Combine note
   interpretation and rating into that one cooked-feedback contribution; do not
   add a note vote on top of a rating vote. Current cooked feedback supersedes
   weak actions, including when it has no directional signal. Otherwise use the
   latest attributable intentional action. Repeated clicks and revisions of the
   same proposed meal do not manufacture independent occasions.
6. Calculate `confidence = (3 + supportUnits) / (6 + supportUnits + oppositionUnits)`.
   This fixed heuristic includes a neutral prior; it is not a calibrated
   probability or medical assessment. Keep full precision for eligibility.
7. Include an insight in future recipe context only when at least two current
   supporting cooked occasions exist, confidence is at least 0.65, it has no
   explicit-preference conflict, and all counted evidence is still eligible.

The constants are a symmetric smoothing prior: the numerator's 3 adds virtual
support, and the denominator's 6 is that same 3 plus 3 virtual opposition units.
This starts the score at 0.5 before evidence, rather than making one positive
observation appear completely certain. With cooked feedback weighted at 3 units,
the prior has the weight of one supporting and one opposing cooked observation.
These are not real meals: they never increase `evidenceCount` or
`supportingCookedCount`. The exact smoothing strength is a proposed heuristic
to evaluate, not a Microsoft-prescribed or calibrated probability.

Boundary examples for a single insight:

| Distinct evidence | Confidence | Eligible |
| --- | --- | --- |
| No real evidence | 0.50 | No: no cooked supporters |
| One supporting cooked meal | 0.666667 | No: only one cooked supporter |
| Two supporting cooked meals | 0.75 | Yes |
| Two supporting cooked meals and one opposing weak action | 0.692308 | Yes |
| Two supporting cooked meals and two opposing weak actions | 0.642857 | No |
| Two supporting cooked meals and one opposing cooked meal | 0.60 | No |
| Weak support alone, regardless of quantity | Calculated normally | No: lacks two cooked supporters |

`evidenceCount` counts distinct contributing occasions, never deliveries or
clicks. Store supporting-cooked count separately. Corrections replace an
occasion's contribution; they do not add another vote. A late worker must not
restore an obsolete revision. Persist provenance outside `Users`, and recompute
from current evidence rather than incrementing scores on each event.

An intentional save event is an allowed weak acceptance signal. A bare history
upsert without that signal, generated suggestion, status-only update, or already
processed revision does not call extraction. Non-attributable behavior can
produce an explicit no-insight result, not invented preferences.

Keep at most 50 learned candidates in `Users`. First remove expired/invalid
candidates, then evict in ascending confidence, oldest evidence time, and insight
key order. Suppress explicit conflicts from context without editing explicit
state. Recheck time-window eligibility when constructing context, even when no
new learning event has arrived.

## 6. Bounds, targets, and retention

Detailed size ceilings and context selection appear in the API/schema outline.
Limits are versioned configuration with the values below as the dev baseline.
They protect reliability, context quality, and fair use rather than imposing
financial gates. Changing them requires review of safety and runtime behavior.

| Operational limit | Baseline decision |
| --- | --- |
| Non-AI latency | p95 at most 1 second; 5-second server deadline |
| Full recipe latency | p95 at most 30 seconds; 60-second server deadline |
| Learning execution | 30 seconds per attempt; at most 3 attempts per revision |
| Recipe model work | At most 4 total model invocations, including at most one specialist regeneration |
| Recipe tool work | At most 8 total calls, including retries; Web IQ at most once |
| Recipe tokens | At most 16,000 input tokens per invocation; 32,000 cumulative input and 6,000 cumulative output tokens across the request |
| Learning tokens | At most 6,000 cumulative input and 1,000 cumulative output tokens per revision, including retries |
| Generation admission | One active send per conversation and user; two active generations globally in dev; no unbounded queue |
| User rate limits | 3 generation admissions/minute; non-AI mutations 60/minute with a burst of 10 |
| Performance acceptance workload | Five authenticated synthetic users, two concurrent generations, five concurrent CRUD clients over 10 minutes |

Account for provider-counted input, output, and reasoning tokens within the
execution limits. Reserve sufficient output capacity before an invocation.
If token accounting, nested calls, or cancellation cannot be bounded reliably,
report an implementation failure and fix the accounting or cancellation wiring.
Never automatically replay an ambiguously
completed request. These are design targets, not measured results; record actual
performance and reliability evidence before release.

| Data class | Retention |
| --- | --- |
| Conversation metadata, complete-exchange turns, and durable send deduplication | Until user-requested deletion; no automatic chat TTL |
| Profile and groceries | Current bounded state until user-requested deletion; removed groceries leave current state immediately |
| Saved/cooked snapshots, feedback, behavioral provenance | Until user-requested deletion; independent of source chat |
| Learned candidates | At most 50; only current 180-day-window evidence can qualify for context |
| Ordinary operational telemetry | 30 days; no raw email, notes, allergies, chats, prompts, responses, or tool payloads |
| Sensitive AI trace content | Disabled by default; explicit diagnostic opt-in only with protected RBAC, at most 7 days |
| Product-data backups | Target maximum 7-day recovery retention; verify supported configuration and recovery behavior before release |

An authenticated deletion request is handled through a documented operator
procedure, completed in primary stores within seven calendar
days. Revoke access to selected data as soon as the request is validated.
Conversation-only deletion preserves independently saved recipe snapshots.
Full-user removal covers profile, chat, recipe history, learning artifacts, and
pending work, plus associated provider-side response/conversation content and application caches
where used. Deleted evidence must no longer qualify learned insights.
Backup expiration is not immediate erasure; document its additional maximum
seven-day horizon and reapply deletions before restoring access after recovery.

## 7. Model assignments and implementation checks

| Responsibility | MVP model |
| --- | --- |
| Orchestrator | GPT-5.4-mini |
| Recipe + Nutrition Agent | GPT-5.4 |
| Asynchronous preference extraction | GPT-5.1 nano |

Use these models and the New Foundry SDK as agreed. Foundry IQ, USDA MCP, and
Web IQ are assumed to exist and work. Do not investigate model availability,
regional support, SDK viability, or alternative tools as prerequisites.
Deployment configuration keeps deployment names separate from model identifiers.
Preference learning and conditional Web IQ are both required MVP features.

Implementation tests must exercise distinct agent principal IDs and actual tool
callers, managed-identity access without secret fallback, managed-agent handoff,
the configured Foundry IQ/Web IQ/MCP connections, structured output,
authentication, evaluation/tracing, provider-side history isolation/deletion,
and token/call/deadline enforcement. Record the USDA repository revision,
dataset provenance/license, PostgreSQL host/importer, read-only parameterized
tools, remote transport, authorization, and operational ownership during normal
integration setup. Runtime failures and wiring bugs remain explicit errors to
fix, not successful fallbacks or a reason to introduce a discovery phase.

No alternative-model evaluation is required for this contract or the initial MVP.
A future model change requires evaluation evidence and collaborator approval;
alternative models are not an implicit fallback.