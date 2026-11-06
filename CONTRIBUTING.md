# Contributing

Create a feature/fix branch from current `dev` and open a PR into `dev` for the
other collaborator to review. New commits require fresh review. Integration into
`dev` does not, by itself, close an issue or approve production promotion.
See [CI requirements](docs/ci.md) and [production release policy](docs/production-release.md).

## Required deterministic safety gate

Run `python -m scripts.evaluate_safety` before the ordinary full tests.
The [versioned hard-safety suite](docs/hard-safety-evaluations.md) requires all
fixed synthetic cases to execute and pass; skipped/xfail/deselected cases fail
closed. Review fixture inputs, expected assertions and manifest version together
when changing this set. This gate supplements all existing allergy/auth tests
and does not replace the endpoint matrix below.

## Required user-data authorization matrix

Every new or changed user-data endpoint must join
[`tests/unit/test_authorization_matrix.py`](tests/unit/test_authorization_matrix.py).
This is required in addition to focused endpoint tests, not a replacement for them.
The route-inventory test compares the matrix with the application's actual product
operations and fails if an operation is omitted. Do not exempt a user-data route,
including one hidden from the public API schema.

The reusable `two_user_api` fixture creates two independent, synthetic, signed-JWT
users, profiles, groceries, conversations, completed turns and send operations.
It exercises the production application, services, repository and pinned Cosmos
SDK against the existing offline transport. It does not replace authentication
or add a deployed bypass.

For each new operation:

1. Add an `EndpointCase` to `CASES` and extend the fixture only with the synthetic
   owned resources that operation needs. Verify successful reads/mutations in
   both directions and audit all SDK calls for the authenticated partition.
2. Cover guessed foreign IDs versus absent IDs, forged identity headers, rejected
   client `userId` values, and wrong-owner records returned within the selected
   partition. Rejected requests must not mutate either user's state or invoke AI.
3. For lists, traverse every page and verify only the caller's items appear.
   Reject another user's cursor. For linked resources, test foreign and
   wrong-parent references.
4. Verify indistinguishable no-store 404 responses and safe validation/conflict
   payloads without another user's content, IDs or ETags. Retain the negative
   control proving that disabling `require_owned` makes the regression probe fail.

Only genuinely non-user-data product operations may enter `NON_USER_DATA`, with
an explicit reason and peer review. USDA search/lookup currently qualify because
they read public source data and never access stored user state; their existing
authentication tests remain required.

Run the matrix locally using the project's Python 3.12 environment:

```bash
python -m pytest tests/unit/test_authorization_matrix.py
python -m ruff format --check .
python -m ruff check .
python -m mypy
```

The normal full `python -m pytest` CI job includes this matrix. Before final
end-to-end release acceptance, recheck route inventory and run the complete suite;
future recipe save/history/cooked/feedback APIs must join the same cases when
implemented. Their unimplemented fallback 404 responses are not authorization
coverage. This offline regression matrix does not claim live Easy Auth/Cosmos
acceptance or production isolation certification.
