# Responsible AI and untrusted-tool guardrails (ASC-37 / #37)

Foundry content filters complement backend authorization and deterministic recipe
validation. Passing a filter is never permission to use a tool, change a profile,
relax an allergy, or display an unsafe recipe.

## Versioned platform configuration

`infra/modules/foundry.bicep` defines the account-scoped
`asc-responsible-ai-v1` policy using the supported `2025-06-01` ARM API.
It inherits `Microsoft.Default`, uses `Blocking` mode, and explicitly enables:

| Filter | Source | Blocking threshold |
| --- | --- | --- |
| Hate, Sexual, Violence, Selfharm | Prompt and Completion | Medium and High |
| Jailbreak (direct prompt attacks) | Prompt | Binary detection |
| Protected Material Text and Protected Material Code | Completion | Binary detection |

Every model deployment in the foundation references this policy and depends on
its creation. The source Orchestrator definition also selects it through the
pinned New Foundry SDK's `RaiConfig`. Recipe + Nutrition retains its explicit
approved-policy setting; set
`ASC_FOUNDRY_RECIPE_NUTRITION_RAI_POLICY_NAME=asc-responsible-ai-v1` after an owner
has applied and verified this policy. An existing approved custom policy name
remains supported, but its controls require separate review; the name alone does
not prove equivalence to v1. Publication resolves that name to the full RAI policy
ARM resource ID on the configured Foundry account; `rai_config.rai_policy_name`
must reference the resource ID, not the bare name. The configured project
resource ID and endpoint must identify the same project.

Use the [foundation validation and owner-approved deployment procedure](../infra/README.md#validate-preview-and-deploy).
The preview must include the new policy and deployment associations, not a
replacement account or broader access grants. No deployment occurs during
compilation, publication tests, or PR integration. Rejected policy configuration
must block rollout; never disable filters as a fallback.

After deployment, the owner must read back the policy's filters/mode and each
deployment's `raiPolicyName`, then
[publish the reviewed agent definitions](orchestrator.md#reproducible-dev-source-publication).
Review and pin the returned source versions through the normal disabled-by-default
release process. Agent-definition fingerprints include the selected policy resource ID,
not the remote policy body. Azure policies can be edited in place: preserve v1
and introduce a new reviewed name for control changes; verify remote drift before
activation. This change does not publish agents, change identities, enable a
manifest, provision Azure resources, or authorize production.

## Backend authority and untrusted data

| Input | Boundary that remains authoritative |
| --- | --- |
| Chat and history | Verified caller, owned backend context, fixed instructions; no caller-selected identity, model, tools or provider conversation |
| Retrieved recipes and MCP output | Pinned Search connection, exact backend query, source revision/URL validation, bounded evidence, deterministic ingredient and final cooking gates |
| Web results | One conditional public-topic search; no profile/history/IDs in the query, native HTTPS citations, no backend URL fetching |
| Learned/feedback context | Current owned evidence and explicit-over-learned precedence; free-form candidate prose is not an instruction or hard restriction |
| Tool names and arguments | Role-specific allowlists, strict schemas, shared call/time/token budgets; no Cosmos/SQL, shell, arbitrary URL or general-purpose tool dispatch |

The production feedback reader/worker remains separate ASC-49 work. This ticket
does not claim coverage of an unimplemented feedback API or add a new ingestion
path. Existing context validation and the fixed policy apply to any supported
learned-preference input.

Function and MCP arguments must be JSON objects without duplicate keys and fit
32,768 characters before decoding. Deeply nested malformed arguments fail safely.
Strict operation-specific schemas still reject additional fields, identity
overrides and unsupported operations. Retrieval arguments must equal the original
backend query, not a model-selected replacement.

MCP discovery plus call envelopes, including descriptions, schemas, arguments and
output, must fit 65,536 characters and the 4,000-token tool-result ceiling.
Only one discovery result and one retrieval call are accepted. Advertised tools
do not grant capabilities: the request's `allowed_tools` and returned call checks
still permit only `knowledge_base_retrieve`. Existing result-count, 16,000-character
retrieval-output, provenance, source-schema and combined specialist-evidence
limits remain in force. A control answer must contain exactly one completed
output-text part; fragments are not concatenated into a trusted control action.

USDA uses the packaged, app-owned FoodData Central API subprocess, exact
`search_foods` / `get_nutrition_profile` schemas and bounded JSON-RPC messages.
Only public ingredient terms and source IDs go to the fixed USDA HTTPS origin;
the API key stays in a protected HTTP header. Arbitrary URLs and SQL are not
exposed, and discovery of `execute_query` does not authorize its execution.
Neither source text nor a returned tool request can select a Cosmos partition
or mutate user data.

The final gate checks the full recipe, including declared ingredients, instruction
references, cooking methods and animal-food handling. The delivery hook rereads
the current owned restrictions before deterministic rendering and persistence.
Unsafe drafts use only the existing bounded revision allowance, never a new loop.
Rejected candidates and raw tool/provider errors are not public assistant text.
Browser errors use fixed codes and opaque operation/trace identifiers, not hidden
reasoning, stack traces, provider response IDs or payloads. Safe operational events
continue to omit those payloads.

## Limitations and acceptance evidence

Filters are probabilistic classifiers, not food-safety, authorization, privacy,
groundedness or tool-access proofs. The v1 policy does not enable indirect
document-attack detection: that feature requires supported document embedding and
formatting. Labeling JSON as untrusted is not evidence that document scanning
occurred. No new delimiter-based prompt wrapper or unsupported moderation API is
introduced.

This application uses New Foundry prompt agents through Responses v1, not hosted
agent `/invocations`. The pinned SDK's separate `invocations_moderation`
configuration is not claimed to protect that unimplemented path. Provider-managed
MCP/web operations may execute before the backend validates their returned actions
and payloads. Invalid results are rejected, but rejection cannot undo a remote
operation. The pinned read-only connection, approved tool configuration and
least-privilege identities remain required; returned-action validation is not a
network sandbox. The backend never follows arbitrary URLs or dispatches SQL.

Offline tests exercise the real pinned SDK transport with synthetic hostile
retrieval, duplicate/oversized/nested arguments, unauthorized authority fields,
excessive discovery metadata and fragmented control output. Signed HTTP/Cosmos
tests reject unsafe/jailbreak recipe instructions without changing preferences,
persisting a recipe, leaking internal payloads or regenerating on replay.
Existing web, USDA, final-gate and two-user authorization suites remain required.
These tests do not measure live filter detection rates or certify remote identity
bindings. Owner-applied policy read-back, approved live blocked/safe smoke tests
and peer-reviewed source activation remain release gates. ASC-38's live chat
composition is a separate PR, not included or enabled here.

References: [RAI ARM schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.cognitiveservices/2025-06-01/accounts/raipolicies),
[deployment association](https://learn.microsoft.com/en-us/azure/templates/microsoft.cognitiveservices/2025-06-01/accounts/deployments),
[filter configuration and document-format requirements](https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/content-filters).
The last reference describes classifier behavior and the classic portal UI; it
does not change this application's New Foundry SDK integration.
