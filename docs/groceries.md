# Grocery inventory APIs

ASC-17 adds synchronous, authenticated grocery inventory APIs. They patch only
`Users.groceries` and server `updatedAt`, preserving explicit preferences,
learned preferences and unrelated profile fields. They never invoke agents,
models, tools or the preference-learning pipeline.

| Route | Behavior |
| --- | --- |
| `GET /api/v1/groceries` | Read the entire bounded inventory and its ETag |
| `POST /api/v1/groceries` | Add a row with a server-issued stable UUID |
| `PATCH /api/v1/groceries/{groceryId}` | Update supplied name, quantity and/or unit |
| `DELETE /api/v1/groceries/{groceryId}` | Confirm removal of the row |

## Setup and requests

Reuse [authentication](authentication.md) and [Cosmos access](cosmos-access.md).
No new environment variables, secrets, resources or role assignments are
required. These APIs require the live Cosmos adapter even when local readiness
doubles work. Missing configuration returns 503, not temporary inventory.

Bootstrap the [profile](profiles.md) with `GET /api/v1/me` first. Grocery
operations do not mark onboarding complete or invent allergy selections.
A missing profile returns 404. The authenticated identity selects the profile
and its partition; the client cannot supply an owner. Hosted browser mutations
retain the existing exact-Origin and CSRF checks.

Read `GET /api/v1/groceries`, then send its exact `ETag` as `If-Match` on every
mutation. The same profile revision is used by profile and learning writes.
For example, the body for adding a grocery is:

```json
{"name": "Milk", "quantity": 1, "unit": "l"}
```

Name is required. Quantity and unit are independently optional and default to
explicit `null` (unknown). PATCH accepts a nonempty subset of those three fields.
Use `null` to clear a quantity or unit; a name cannot be null. Client-written
IDs, ingredient mappings, timestamps, inferred fields, ownership and unknown
properties are rejected. No query parameters are supported.

Creation returns 201 and `{"data": ...}`; PATCH returns 200 with the same envelope.
Each row contains `groceryId`, `name`, `quantity`, `unit`, `ingredientId` and
server UTC `updatedAt`. New rows have no inferred ingredient mapping.
Renaming or changing a row's unit clears any existing mapping; quantity-only
edits preserve it. GET returns `{"items":[...],"nextCursor":null}`.
DELETE accepts no body and returns 204 with no body. Successful responses include
`Cache-Control: no-store` and the current collection `ETag`.

## Quantities, duplicates and removal

Names are trimmed, nonblank and at most 100 Unicode code points; control
characters are invalid. Quantities must be JSON numbers, strictly positive,
finite, at most 1,000,000 and have at most three fractional digits. Numeric
strings and booleans are invalid. Decimal JSON numbers are checked without
binary-float rounding. The units are `g`, `kg`, `ml`, `l`, `tsp`,
`tbsp`, `cup`, `oz`, `lb`, `item`, `piece`, `bunch`, `can`, `package`, or null.
There is no unit conversion or synonym/alias normalization.

The UI's +/- step is 1 in the selected unit. For a known quantity, send the new
absolute amount through PATCH using the revision that was read. An unknown
quantity first requires explicit input; never assume it is one.
If decrement would reach zero or below, ask the user to confirm removal.
Confirmation sends DELETE with the current ETag; cancellation sends nothing.
The backend rejects zero/negative quantities and never silently deletes a row.

The same trimmed, Unicode-casefolded name and exact selected unit (including
null) is a duplicate, even if its quantity differs. Creation and edits reject
duplicates with 409 `duplicate_grocery`. The error's safe `errors` entry includes
the existing owned row's `groceryId`; quantities are never merged. Different
units remain separate rows. At 200 rows, adding another returns 422
`grocery_limit_reached`. Removing or editing existing rows is still possible.

## Errors and concurrency

Missing `If-Match` returns 428; stale revisions return 412. Weak, wildcard or
duplicate revision headers are invalid. Two edits based on the same revision
cannot silently overwrite one another. Keep the draft, reload the inventory,
review the current amount and explicitly retry with its new ETag.

Only the bounded grocery array is patched, never the entire profile. The shared
Cosmos repository enforces conditional writes and the 256-KiB projected-record
ceiling; mutation requests have the shared 128-KiB body limit. Ambiguous writes
return an explicit unknown-outcome error and are not automatically replayed.
Reload to reconcile before deciding whether another mutation is needed.

Invalid input returns a safe field/code error without echoing private values.
Malformed JSON returns 400, invalid fields 422, and absent or foreign row IDs
the same 404. Database errors and corrupt stored rows fail explicitly instead
of reporting a successful empty inventory.

Offline acceptance uses signed synthetic identities and the real pinned Cosmos
SDK over the existing HTTP transport double. No Azure resource or user inventory
was accessed. Live acceptance remains owner-approved work after peer review,
using the [existing Cosmos smoke and cleanup procedure](cosmos-access.md).
