import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool

from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.adapters.receipt_images import receipt_images
from sous_chef.adapters.receipt_reader import FoundryReceiptReader
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.conversations import IdempotencyConflict
from sous_chef.domain.cosmos import CosmosError, CosmosFailure, RecordPatch, StoredRecord
from sous_chef.domain.groceries import (
    MAX_GROCERIES,
    CreateGrocery,
    GroceryLimitReached,
    GroceryReceipt,
    GroceryRow,
)
from sous_chef.domain.profiles import ProfileModel
from sous_chef.domain.receipts import (
    ReceiptError,
    ReceiptNotice,
    ReceiptRead,
    ReceiptReader,
    ReceiptResult,
)
from sous_chef.services.groceries import USERS, _read, _revision, _rows
from sous_chef.settings import Settings

_NONFOOD = re.compile(
    r"\b(candles?|detergent|soap|shampoo|conditioner|bleach|cleaner|"
    r"toilet paper|paper towels?|toothpaste|deodorant|kibble|"
    r"(?:dog|cat|pet) food|subtotal|sales tax|coupon|refund)\b",
    re.IGNORECASE,
)


class _ReceiptLedger(ProfileModel):
    model_config = ConfigDict(extra="ignore")

    receipt_imports: dict[str, str] = Field(default_factory=dict)


def _ledger(record: StoredRecord) -> dict[str, str]:
    try:
        ledger = _ReceiptLedger.model_validate_json(json.dumps(record.body)).receipt_imports
        if any(not re.fullmatch(r"[a-f0-9]{64}", part) for pair in ledger.items() for part in pair):
            raise ValueError
        return ledger
    except ValueError:
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None


def _replayed(ledger: dict[str, str], key: str, digest: str) -> bool:
    if key in ledger and ledger[key] != digest:
        raise IdempotencyConflict
    return digest in ledger.values()


def _prepare(
    read: ReceiptRead, existing: tuple[GroceryRow, ...], import_id: UUID
) -> tuple[tuple[GroceryRow, ...], ReceiptResult]:
    if not read.is_receipt:
        raise ReceiptError("receipt_unreadable", "Upload a legible shopping receipt.")
    rows = list(existing)
    added = merged = estimated = 0
    skipped: list[ReceiptNotice] = []
    review: list[ReceiptNotice] = []
    for line in read.lines:
        if line.category == "nonfood" or _NONFOOD.search(line.receipt_text + " " + line.name):
            skipped.append(ReceiptNotice(name=line.name, reason="Nonfood or non-purchase line."))
            continue
        if line.category != "food":
            review.append(ReceiptNotice(name=line.receipt_text, reason="Food identity is unclear."))
            continue
        guessed = line.quantity_source == "estimated" or line.quantity is None or line.unit is None
        reason = (
            (line.estimate_reason or "Assumed a typical retail amount; please check the quantity.")
            if guessed
            else None
        )
        try:
            amount = CreateGrocery(
                name=line.name,
                quantity=float(Decimal(str(line.quantity or 1)).quantize(Decimal("0.001"))),
                unit=line.unit or "package",
            )
        except ValidationError:
            raise ReceiptError(
                "receipt_invalid_amount",
                "The extracted amounts need correction. Nothing was added.",
            ) from None
        source = GroceryReceipt(
            import_id=import_id,
            quantity_source="estimated" if guessed else "printed",
            estimate_reason=reason,
        )
        match = next(
            (
                index
                for index, row in enumerate(rows)
                if row.name.casefold() == amount.name.casefold() and row.unit == amount.unit
            ),
            None,
        )
        if match is None:
            if len(rows) >= MAX_GROCERIES:
                raise GroceryLimitReached
            row = GroceryRow(
                grocery_id=uuid4(),
                name=amount.name,
                quantity=amount.quantity,
                unit=amount.unit,
                updated_at=datetime.now(UTC),
                receipt=source,
            )
            rows.append(row)
            added += 1
        else:
            previous = rows[match]
            if previous.quantity is None:
                review.append(
                    ReceiptNotice(
                        name=amount.name,
                        reason=(
                            f"Receipt amount: {amount.quantity} {amount.unit}. "
                            "Existing pantry amount is unknown; update it manually."
                        ),
                    )
                )
                continue
            assert amount.quantity is not None
            if previous.receipt is not None and previous.receipt.quantity_source == "estimated":
                source = source.model_copy(
                    update={
                        "quantity_source": "estimated",
                        "estimate_reason": "Includes a previously estimated pantry amount.",
                    }
                )
            try:
                rows[match] = GroceryRow.model_validate(
                    {
                        **previous.model_dump(),
                        "quantity": float(
                            Decimal(str(previous.quantity)) + Decimal(str(amount.quantity))
                        ),
                        "updated_at": datetime.now(UTC),
                        "receipt": source,
                    }
                )
            except ValidationError:
                raise ReceiptError(
                    "receipt_invalid_amount", "An imported amount exceeds the pantry limits."
                ) from None
            merged += 1
        estimated += guessed
    if not read.lines:
        raise ReceiptError("receipt_unreadable", "No readable purchases were found.")
    return tuple(rows), ReceiptResult(
        import_id=import_id,
        added=added,
        merged=merged,
        estimated=estimated,
        skipped=tuple(skipped),
        needs_review=tuple(review),
    )


class ReceiptService:
    def __init__(self, settings: Settings, reader: ReceiptReader | None = None) -> None:
        self.settings = settings
        self.reader = reader if reader is not None else FoundryReceiptReader(settings)
        self.slots = asyncio.Semaphore(2)

    async def import_receipt(
        self, user: AuthenticatedUser, content: bytes, key: str, etag: str
    ) -> tuple[ReceiptResult, str]:
        digest = hashlib.sha256(content).hexdigest()
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        import_id = UUID(
            bytes=hashlib.sha256((user.user_id + digest).encode()).digest()[:16], version=4
        )
        async with open_cosmos(self.settings) as repository:
            current, _ = await _read(repository, user)
            if _replayed(_ledger(current), key_hash, digest):
                return ReceiptResult(import_id=import_id, already_imported=True), current.etag
            _revision(current, etag)
        async with self.slots:
            images = await run_in_threadpool(receipt_images, content)
            extracted = await self.reader.read(images)
        async with open_cosmos(self.settings) as repository:
            current, rows = await _read(repository, user)
            ledger = _ledger(current)
            if _replayed(ledger, key_hash, digest):
                return ReceiptResult(import_id=import_id, already_imported=True), current.etag
            _revision(current, etag)
            updated_rows, result = _prepare(extracted, rows, import_id)
            # One conditional document patch commits stock and deduplication together.
            ledger[key_hash] = digest
            ledger_fields: dict[str, JsonValue] = dict(ledger)
            try:
                updated = await repository.patch(
                    user,
                    USERS,
                    user.user_id,
                    RecordPatch(
                        etag=etag,
                        fields={
                            "groceries": [
                                row.model_dump(mode="json", by_alias=True) for row in updated_rows
                            ],
                            "receiptImports": ledger_fields,
                        },
                    ),
                )
            except CosmosError as exc:
                if not exc.outcome_unknown:
                    raise
                reconciled, _ = await _read(repository, user)
                if not _replayed(_ledger(reconciled), key_hash, digest):
                    raise
                return result, reconciled.etag
            if _rows(updated, outcome_unknown=True) != updated_rows or not _replayed(
                _ledger(updated), key_hash, digest
            ):
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
            return result, updated.etag
