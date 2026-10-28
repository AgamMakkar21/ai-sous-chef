"""Conditional inventory-only profile patches; no agents or learning sources."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, ValidationError

from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    ProductContainer,
    RecordPatch,
    StoredRecord,
)
from sous_chef.domain.groceries import (
    MAX_GROCERIES,
    CreateGrocery,
    DuplicateGrocery,
    GroceryLimitReached,
    GroceryList,
    GroceryRow,
    UpdateGrocery,
)
from sous_chef.domain.profiles import ProfileModel
from sous_chef.settings import Settings

USERS = ProductContainer.USERS


class _StoredGroceries(ProfileModel):
    model_config = ConfigDict(extra="ignore")

    groceries: tuple[GroceryRow, ...] = Field(max_length=MAX_GROCERIES)


def _rows(record: StoredRecord, *, outcome_unknown: bool = False) -> tuple[GroceryRow, ...]:
    try:
        rows = _StoredGroceries.model_validate_json(json.dumps(record.body)).groceries
    except ValidationError:
        raise CosmosError(
            CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=outcome_unknown
        ) from None
    if len({row.grocery_id for row in rows}) != len(rows) or len(
        {(row.name.casefold(), row.unit) for row in rows}
    ) != len(rows):
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=outcome_unknown)
    return rows


def _revision(record: StoredRecord, etag: str) -> None:
    if record.etag != etag:
        raise CosmosError(CosmosFailure.CONFLICT, uuid4())


def _unique(rows: tuple[GroceryRow, ...], candidate: GroceryRow) -> None:
    for row in rows:
        if (
            row.grocery_id != candidate.grocery_id
            and row.name.casefold() == candidate.name.casefold()
            and row.unit == candidate.unit
        ):
            raise DuplicateGrocery(row.grocery_id)


async def _read(
    repository: CosmosRepository, user: AuthenticatedUser
) -> tuple[StoredRecord, tuple[GroceryRow, ...]]:
    record = await repository.read(user, USERS, user.user_id)
    if record is None:
        raise OwnedResourceNotFound
    return record, _rows(record)


async def _write(
    repository: CosmosRepository, user: AuthenticatedUser, etag: str, rows: tuple[GroceryRow, ...]
) -> str:
    updated = await repository.patch(
        user,
        USERS,
        user.user_id,
        RecordPatch(
            etag=etag,
            fields={"groceries": [row.model_dump(mode="json", by_alias=True) for row in rows]},
        ),
    )
    if _rows(updated, outcome_unknown=True) != rows:
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
    return updated.etag


def _find(rows: tuple[GroceryRow, ...], grocery_id: UUID) -> GroceryRow:
    for row in rows:
        if row.grocery_id == grocery_id:
            return row
    raise OwnedResourceNotFound


class GroceryService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def list(self, user: AuthenticatedUser) -> tuple[GroceryList, str]:
        async with open_cosmos(self.settings) as repository:
            record, rows = await _read(repository, user)
            return GroceryList(items=rows), record.etag

    async def add(
        self, user: AuthenticatedUser, request: CreateGrocery, etag: str
    ) -> tuple[GroceryRow, str]:
        async with open_cosmos(self.settings) as repository:
            record, rows = await _read(repository, user)
            _revision(record, etag)
            row = GroceryRow(
                grocery_id=uuid4(),
                name=request.name,
                quantity=request.quantity,
                unit=request.unit,
                updated_at=datetime.now(UTC),
            )
            _unique(rows, row)
            if len(rows) == MAX_GROCERIES:
                raise GroceryLimitReached
            revision = await _write(repository, user, etag, (*rows, row))
            return row, revision

    async def update(
        self, user: AuthenticatedUser, grocery_id: UUID, request: UpdateGrocery, etag: str
    ) -> tuple[GroceryRow, str]:
        async with open_cosmos(self.settings) as repository:
            record, rows = await _read(repository, user)
            current = _find(rows, grocery_id)
            _revision(record, etag)
            row = GroceryRow.model_validate(
                {
                    **current.model_dump(by_alias=True),
                    **request.model_dump(by_alias=True, exclude_unset=True),
                    "updatedAt": datetime.now(UTC),
                }
            )
            if row.name != current.name or row.unit != current.unit:
                row = GroceryRow.model_validate(
                    {**row.model_dump(by_alias=True), "ingredientId": None}
                )
            _unique(rows, row)
            updated = tuple(row if item.grocery_id == grocery_id else item for item in rows)
            revision = await _write(repository, user, etag, updated)
            return row, revision

    async def delete(self, user: AuthenticatedUser, grocery_id: UUID, etag: str) -> str:
        async with open_cosmos(self.settings) as repository:
            record, rows = await _read(repository, user)
            _find(rows, grocery_id)
            _revision(record, etag)
            return await _write(
                repository, user, etag, tuple(row for row in rows if row.grocery_id != grocery_id)
            )
