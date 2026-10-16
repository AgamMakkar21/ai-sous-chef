# Profile and onboarding APIs

These authenticated backend APIs create one current profile, complete explicit
onboarding and edit explicit preferences. They do not invoke Foundry, tools,
models or the preference-learning pipeline.

| Route | Behavior |
| --- | --- |
| `GET /api/v1/me` | Retrieve the current profile, creating it if absent |
| `PUT /api/v1/me/onboarding` | Supply all explicit fields and complete onboarding |
| `PATCH /api/v1/me/preferences` | Change only supplied explicit fields after onboarding |

## Setup and ownership

Use the existing [consumer authentication](authentication.md) and
[Cosmos access](cosmos-access.md) setup. Set `ASC_ADAPTER_MODE=live`,
`ASC_COSMOS_ENDPOINT` and `ASC_COSMOS_DATABASE`; configure the existing live-mode
settings described in the environment examples. These APIs require Cosmos even
when local readiness doubles work. Missing configuration returns 503, not a
temporary successful profile.

Hosted access uses the workload's managed identity. Local live access uses the
explicit developer credential described in the Cosmos setup. No additional
provider, secret, environment variable or infrastructure resource is required.

The verified issuer/subject determines the internal user ID. The profile's
Cosmos ID and `/userId` partition are both that server-derived ID. Clients cannot
choose either. Query parameters, ownership fields, learned fields, confidence,
evidence, timestamps and unknown properties are rejected. Identity-hint headers
do not override authentication. Email is not copied from sign-in claims.
Hosted session mutations retain the existing CSRF-token and exact-Origin checks.

Repeated initialization reads the same deterministic ID. Simultaneous first
logins reconcile a create conflict with a point read. An ambiguous write is not
automatically repeated; a subsequent `GET /me` reconciles the same ID.

## First login and completion

Responses use `{"data": ...}` and return `Cache-Control: no-store` and an opaque
`ETag`. The public view contains only `userId`, `onboardingCompleted`,
`explicitPreferences`, `createdAt` and `updatedAt`. It does not expose raw Cosmos
metadata, stored groceries, learned data or private processing fields.

A new profile has `onboardingCompleted: false` and
`explicitPreferences: null`. This is **not** a declaration of no allergies.
Its stored groceries and learned preferences start as empty arrays.

Read the profile first. Send its exact ETag in `If-Match` when submitting all
nine fields to `PUT /api/v1/me/onboarding`:

```json
{
  "allergies": [],
  "dietaryRestrictions": [],
  "likes": ["citrus-forward flavors"],
  "dislikes": ["bitter flavors"],
  "preferredCuisines": ["Italian"],
  "spicePreference": "mild",
  "cookingSkill": "beginner",
  "typicalTimeBudgetMinutes": 30,
  "nutritionGoals": ["balanced eating"]
}
```

All fields are required. An explicitly supplied `[]` confirms no selected
allergies/restrictions; omission does not. Completion is server-controlled.
An incomplete profile cannot use PATCH as an alternate completion path.

Future recipe-execution endpoints must apply
`domain.profiles.require_onboarding` to the current stored profile before
starting generation. This change does not implement recipe execution or replace
the deterministic final ingredient safety check.

## Validation and subsequent edits

PATCH accepts any nonempty subset of the same nine fields. Omitted fields remain
unchanged. Use an empty array to clear a selection; explicit `null` is invalid.
Flavors belong in likes/dislikes, not a new inferred or dedicated flavor field.
Both complete onboarding and later edits validate against the same types.

| Field | Accepted values or limit |
| --- | --- |
| `allergies` | At most 50 food/allergen entries |
| `dietaryRestrictions` | At most 20 supported strict restrictions |
| `likes`, `dislikes` | At most 50 entries each |
| `preferredCuisines`, `nutritionGoals` | At most 20 entries each |
| Each selection entry | Nonblank string; at most 100 Unicode code points after trimming |
| `spicePreference` | `none`, `mild`, `medium`, `hot` |
| `cookingSkill` | `beginner`, `intermediate`, `advanced` |
| `typicalTimeBudgetMinutes` | Integer 5 through 240; strings, booleans and floats rejected |

Safety-input support comes from the existing deterministic validator, not an AI
inference. Recognized allergen exclusions and strict diets such as vegetarian,
vegan and pescatarian are supported. Custom food allergies such as peaches use
the validator's existing literal-food matching rules; this does not claim a
complete allergy catalog. Unsupported strict restrictions, such as keto, return
422 with `unsupported_constraint`. They are never silently treated as enforced.
Nutrition goals are soft personalization, not medical or strict safety rules.

Invalid input returns a safe RFC 9457 problem with bounded field/code entries,
for example `{"field":"allergies.0","code":"unsupported_constraint"}`.
Errors never echo rejected values or arbitrary client-supplied property names.
Malformed JSON returns 400; other schema failures return 422.
Requests are limited to 128 KiB, including streamed bodies, and projected stored
records to 256 KiB.

## Concurrency and storage boundaries

Both mutations require `If-Match`: missing returns 428; stale returns 412.
Wildcards, weak validators and duplicate revision headers are invalid.
Reload after a conflict, review the current state and resubmit only the intended
edit with its current revision. There is no retry using a newer revision.

The revision is the whole profile's Cosmos ETag, shared with grocery and
learned-state writes. A conditional patch changes only explicit preferences,
server `updatedAt` and, for onboarding, server completion state. It never replaces
the document or clears groceries, learned candidates or future unrelated fields.
An intervening independent write must win or cause an explicit conflict.
Stored learned data remains unchanged even when an explicit preference
contradicts it; explicit-over-learned precedence belongs to context composition.

Each operation uses the existing bounded Cosmos unit of work, including SDK
startup and metadata checks. Corrupt stored profile state, unavailable services
and uncertain writes return explicit problems rather than invented defaults.

Offline acceptance exercises real JWT verification and the pinned Cosmos SDK
over the existing stateful HTTP transport. Live Cosmos acceptance remains a
separate owner-approved check after review; follow the synthetic-data smoke and
cleanup procedure in the Cosmos documentation. These endpoints do not require
a live model call.
