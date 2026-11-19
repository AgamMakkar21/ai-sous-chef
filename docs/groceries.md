# Grocery inventory APIs

ASC-17 adds synchronous, authenticated grocery inventory APIs. They patch only
`Users.groceries` and server `updatedAt`, preserving explicit preferences,
learned preferences and unrelated profile fields. They never invoke agents,
models, tools or the preference-learning pipeline. The optional receipt-import
route below is separate and does call AI.

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

The [Groceries tab](frontend.md#grocery-inventory) implements these controls with
confirmed server state, a single in-flight mutation and explicit reload/review
recovery. No mutation is automatically replayed after a failure.

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

## Optional receipt import

`ASC_RECEIPTS_ENABLED` defaults to `false`. When enabled, the inventory response
includes `receiptImportEnabled=true` and the pantry displays a compact,
expandable **Upload receipt** control. Import feedback stays visible when the
upload form is collapsed.
Manual add/edit/delete behavior is unchanged. Set `ASC_FOUNDRY_PROJECT_ENDPOINT`
and `ASC_RECEIPT_MODEL_DEPLOYMENT` (default `orchestrator`) to an existing
image-capable model deployment. This uses a separate, tool-free Responses call,
not the recipe/orchestrator agent or its instructions. Local credentials use the
developer's explicit Azure CLI sign-in; hosted credentials retain managed identity.

`POST /api/v1/groceries/receipts` accepts the **raw file body**, not JSON or
multipart. Send the file Content-Type, current `If-Match`, and an
`Idempotency-Key`; hosted Origin/CSRF requirements still apply. JPG/JPEG, PNG,
WebP and HEIC/HEIF are decoded and normalized to JPEG with metadata removed.
PDF receipts are rendered, with a three-page maximum. The upload limit is
10 MiB and the decoded image limit is 24 megapixels; ordinary APIs retain the
128-KiB limit. Image reading is limited to two concurrent calls per app instance.
Images are processed transiently and responses use `store=false`; provider-side
retention remains governed by Azure's configuration, not this application.

The extractor classifies human food/drinks, nonfood and uncertain descriptions.
Only identifiable foods are added. Nonfood descriptions, including candles,
cleaners and pet food, are skipped; uncertain identities are returned for review.
Printed quantities take precedence; absent amounts use a clearly labeled AI
estimate, with a one-package fallback if no useful size is available. These are
not measured inventory or proof of ingredient/allergen contents.

The response contains `importId`, `added`, `merged`, `estimated`, `skipped`,
`needsReview` and `alreadyImported`. Imported grocery rows include optional
`receipt` provenance with `quantitySource` and `estimateReason`. User edits mark
that amount as user-supplied. Legacy/manual rows need no provenance.
Imports merge only case-insensitive exact names with the same unit and known
amounts; other units remain separate. Unknown existing amounts require manual
review and are not overwritten. No unit conversions or synonym matching occur.

The full import is validated before one ETag-conditional profile patch commits
both `groceries` and `receiptImports` (hashed keys/file digests only). Concurrent
edits fail rather than overwrite stock. An exact-file replay returns
`alreadyImported=true`, even with a new key or stale ETag, before another model
call. A key with different bytes returns 409. Cropping/re-encoding a receipt
changes its hash and is not detected as an identical upload. Import markers
remain until user-data deletion and share the existing 256-KiB profile ceiling;
an oversized profile fails atomically rather than dropping deduplication history.
No automatic retry replays an unknown write.

### Isolated localhost preview

The development-only `scripts.preview_receipts` harness uses the existing signed
synthetic identity and Cosmos SDK transport fixtures. It never connects to
deployed Cosmos or disables production authentication. It persists its test
pantry to a supplied local directory and calls the real configured Foundry
model. It requires the development dependencies, editable backend installation,
frontend build, and an authorized Azure CLI sign-in:

```text
python -m scripts.preview_receipts --endpoint <existing-foundry-project-endpoint> --state-dir <local-preview-directory> --port 8000
```

Open `http://localhost:8000`. The preview binds only to loopback and rejects
foreign Host/Origin values. Recipe chat is not connected in this harness.
Do not deploy the preview harness; deployed routes keep normal sign-in and
Cosmos configuration. Uploads consume the existing model's quota and billing.
