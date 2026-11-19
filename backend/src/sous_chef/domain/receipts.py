from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sous_chef.domain.groceries import GroceryUnit
from sous_chef.domain.profiles import ProfileModel

RECEIPT_PATH = "/api/v1/groceries/receipts"
MAX_RECEIPT_BYTES = 10 * 1024 * 1024


class ReceiptLine(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)

    receipt_text: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=100)
    category: Literal["food", "nonfood", "uncertain"]
    quantity: float | None = Field(gt=0, le=1_000_000)
    unit: GroceryUnit | None
    quantity_source: Literal["printed", "estimated"]
    estimate_reason: str | None = Field(max_length=300)


class ReceiptRead(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    is_receipt: bool
    lines: list[ReceiptLine] = Field(max_length=200)


class ReceiptReader(Protocol):
    async def read(self, images: tuple[str, ...]) -> ReceiptRead: ...


class ReceiptNotice(ProfileModel):
    name: str
    reason: str


class ReceiptResult(ProfileModel):
    import_id: UUID
    added: int = 0
    merged: int = 0
    estimated: int = 0
    skipped: tuple[ReceiptNotice, ...] = ()
    needs_review: tuple[ReceiptNotice, ...] = ()
    already_imported: bool = False


class ReceiptError(ValueError):
    def __init__(self, code: str, detail: str, status: int = 422) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        super().__init__(code)
