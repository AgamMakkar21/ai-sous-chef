import asyncio
import json
from collections.abc import Callable, Iterator
from copy import deepcopy
from io import BytesIO
from typing import Any

import httpx2
import pytest
from azure.ai.projects.aio import AIProjectClient
from fastapi.testclient import TestClient
from httpx2 import Response
from openai import AsyncOpenAI
from PIL import Image
from sous_chef.adapters import receipt_reader
from sous_chef.adapters.receipt_images import receipt_images
from sous_chef.app import create_app
from sous_chef.domain.receipts import (
    MAX_RECEIPT_BYTES,
    RECEIPT_PATH,
    ReceiptError,
    ReceiptLine,
    ReceiptRead,
)
from sous_chef.settings import Settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_foundry_gateway import Harness, response_body
from test_foundry_gateway import settings as foundry_settings
from test_grocery_api import GROCERIES, add
from test_profile_api import ME, document
from test_profile_api import profile_settings as profile_settings


def receipt_image(format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (200, 300), "white").save(output, format=format)
    return output.getvalue()


def line(name: str, **values: object) -> ReceiptLine:
    return ReceiptLine.model_validate(
        {
            "receipt_text": name,
            "name": name,
            "category": "food",
            "quantity": 2,
            "unit": "item",
            "quantity_source": "printed",
            "estimate_reason": None,
            **values,
        }
    )


class FakeReceiptReader:
    def __init__(self) -> None:
        self.calls = 0
        self.result = ReceiptRead(
            is_receipt=True,
            lines=[
                line("Tomatoes"),
                line(
                    "Pasta",
                    quantity=500,
                    unit="g",
                    quantity_source="estimated",
                    estimate_reason="Assumed one 500 g packet.",
                ),
                line("Vanilla candle"),  # Deliberately misclassified by the model.
                line("Dish detergent", category="nonfood"),
                line("MYSTERY 42", category="uncertain"),
            ],
        )
        self.on_read: Callable[[], None] | None = None
        self.error: ReceiptError | None = None

    async def read(self, images: tuple[str, ...]) -> ReceiptRead:
        assert images and all(image.startswith("data:image/jpeg;base64,") for image in images)
        self.calls += 1
        if self.on_read:
            self.on_read()
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def reader() -> FakeReceiptReader:
    return FakeReceiptReader()


@pytest.fixture
def receipt_client(
    profile_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    reader: FakeReceiptReader,
) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            profile_settings.model_copy(update={"receipts_enabled": True}), receipt_reader=reader
        ),
        headers=auth_headers,
    ) as client:
        assert client.get(ME).status_code == 200
        yield client


def upload(client: TestClient, content: bytes | None = None, /, **headers: str) -> Response:
    etag = client.get(GROCERIES).headers["etag"]
    return client.post(
        RECEIPT_PATH,
        content=receipt_image() if content is None else content,
        headers={
            "Content-Type": "image/png",
            "If-Match": etag,
            "Idempotency-Key": "receipt-request",
            **headers,
        },
    )


def test_food_import_filters_nonfood_marks_estimates_and_preserves_profile(
    receipt_client: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, _ = harness
    before = deepcopy(document(transport))
    result = upload(receipt_client)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["added"] == 2
    assert result.json()["estimated"] == 1
    assert len(result.json()["skipped"]) == 2
    assert len(result.json()["needsReview"]) == 1
    rows = receipt_client.get(GROCERIES).json()["items"]
    assert [row["name"] for row in rows] == ["Tomatoes", "Pasta"]
    assert rows[0]["receipt"]["quantitySource"] == "printed"
    assert rows[1]["receipt"]["quantitySource"] == "estimated"
    assert rows[1]["receipt"]["estimateReason"] == "Assumed one 500 g packet."
    for field in ("explicitPreferences", "learnedPreferences", "createdAt"):
        assert document(transport)[field] == before[field]
    assert "Pasta" not in caplog.text
    assert len(transport.item_calls("PATCH")) == 1
    assert {
        operation["path"] for operation in transport.item_calls("PATCH")[0].body["operations"]
    } == {"/groceries", "/receiptImports", "/updatedAt"}


def test_repeated_uploads_and_new_keys_never_double_add_or_invoke_ai(
    receipt_client: TestClient, reader: FakeReceiptReader
) -> None:
    first = upload(receipt_client)
    stale = first.headers["etag"]
    add(receipt_client, name="Milk")
    for key in ("receipt-request", "another-key"):
        replay = upload(receipt_client, **{"If-Match": stale, "Idempotency-Key": key})
        assert replay.status_code == 200
        assert replay.json()["alreadyImported"] is True
        assert replay.json()["importId"] == first.json()["importId"]
    assert reader.calls == 1
    assert len(receipt_client.get(GROCERIES).json()["items"]) == 3
    conflict = upload(receipt_client, receipt_image("JPEG"))
    assert conflict.status_code == 409
    assert reader.calls == 1


def test_known_amounts_merge_and_correcting_an_estimate_clears_its_badge(
    receipt_client: TestClient,
) -> None:
    add(receipt_client, name="tomatoes", quantity=3, unit="item")
    imported = upload(receipt_client)
    assert imported.json()["merged"] == 1
    inventory = receipt_client.get(GROCERIES)
    rows = inventory.json()["items"]
    assert rows[0]["quantity"] == 5
    pasta = rows[1]
    changed = receipt_client.patch(
        GROCERIES + "/" + pasta["groceryId"],
        json={"quantity": 450},
        headers={"If-Match": inventory.headers["etag"]},
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["receipt"]["quantitySource"] == "user"
    assert changed.json()["data"]["receipt"]["estimateReason"] is None


def test_unknown_existing_amount_is_not_overwritten_with_receipt_amount(
    receipt_client: TestClient,
) -> None:
    add(receipt_client, name="Tomatoes", unit="item")
    response = upload(receipt_client)
    assert response.status_code == 200
    assert response.json()["merged"] == 0
    assert len(response.json()["needsReview"]) == 2
    assert receipt_client.get(GROCERIES).json()["items"][0]["quantity"] is None


def test_fractional_receipt_amounts_merge_without_float_precision_errors(
    receipt_client: TestClient, reader: FakeReceiptReader
) -> None:
    add(receipt_client, name="Tomatoes", quantity=0.1, unit="kg")
    reader.result = ReceiptRead(is_receipt=True, lines=[line("Tomatoes", quantity=0.2, unit="kg")])
    response = upload(receipt_client)
    assert response.status_code == 200, response.text
    assert response.json()["merged"] == 1
    assert receipt_client.get(GROCERIES).json()["items"][0]["quantity"] == 0.3


def test_stale_revision_and_concurrent_edit_never_overwrite_stock(
    receipt_client: TestClient,
    reader: FakeReceiptReader,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    stale = receipt_client.get(GROCERIES).headers["etag"]
    add(receipt_client)
    response = upload(receipt_client, **{"If-Match": stale})
    assert response.status_code == 412 and reader.calls == 0
    before = deepcopy(document(transport)["groceries"])

    def concurrent_change() -> None:
        document(transport)["_etag"] = '"concurrent-profile-edit"'

    reader.on_read = concurrent_change
    response = upload(receipt_client)
    assert response.status_code == 412 and reader.calls == 1
    assert document(transport)["groceries"] == before
    assert "receiptImports" not in document(transport)


def test_lost_write_response_reconciles_using_atomic_import_marker(
    receipt_client: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    transport.write_then_disconnect = True
    response = upload(receipt_client)
    assert response.status_code == 200, response.text
    assert len(document(transport)["groceries"]) == 2
    assert len(transport.item_calls("PATCH")) == 1
    assert upload(receipt_client).json()["alreadyImported"] is True


@pytest.mark.parametrize("failure", ["not_receipt", "model", "invalid_name", "full"])
def test_failures_do_not_partially_import(
    receipt_client: TestClient,
    reader: FakeReceiptReader,
    harness: tuple[Transport, SyntheticCredential],
    failure: str,
) -> None:
    transport, _ = harness
    if failure == "not_receipt":
        reader.result = ReceiptRead(is_receipt=False, lines=[])
    elif failure == "model":
        reader.error = ReceiptError("receipt_ai_unavailable", "Try later.", 503)
    elif failure == "invalid_name":
        reader.result.lines.append(line("bad\nname"))
    else:
        row = add(receipt_client).json()["data"]
        from uuid import uuid4

        document(transport)["groceries"] = [
            {**row, "groceryId": str(uuid4()), "name": f"Existing {index}"} for index in range(200)
        ]
    before = deepcopy(document(transport))
    response = upload(receipt_client)
    assert response.status_code == (503 if failure == "model" else 422), response.text
    assert document(transport) == before


def test_missing_amount_gets_an_explicitly_labeled_fallback(
    receipt_client: TestClient, reader: FakeReceiptReader
) -> None:
    reader.result = ReceiptRead(is_receipt=True, lines=[line("Rice", quantity=None, unit=None)])
    assert upload(receipt_client).status_code == 200
    row = receipt_client.get(GROCERIES).json()["items"][0]
    assert (row["quantity"], row["unit"]) == (1, "package")
    assert row["receipt"]["quantitySource"] == "estimated"


def test_invalid_oversized_and_forged_uploads_do_not_invoke_ai(
    receipt_client: TestClient, reader: FakeReceiptReader
) -> None:
    for content, expected in ((b"not an image", 422), (b"x" * (MAX_RECEIPT_BYTES + 1), 413)):
        assert upload(receipt_client, content).status_code == expected
    assert upload(receipt_client, **{"Content-Type": "application/json"}).status_code == 422
    assert upload(receipt_client, **{"If-Match": "*"}).status_code == 422
    assert reader.calls == 0


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP", "HEIF"])
def test_receipt_formats_normalize_to_metadata_free_jpeg(format: str) -> None:
    import base64

    normalized = receipt_images(receipt_image(format))
    image = Image.open(BytesIO(base64.b64decode(normalized[0].split(",", 1)[1])))
    assert image.format == "JPEG"
    assert image.size == (200, 300)
    assert not image.getexif()


def test_pdf_page_limit_and_invalid_images() -> None:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    with pdfium.PdfDocument.new() as document:
        document.new_page(200, 300)
        output = BytesIO()
        document.save(output)
        assert len(receipt_images(output.getvalue())) == 1
        for _ in range(3):
            document.new_page(200, 300)
        output = BytesIO()
        document.save(output)
        with pytest.raises(ReceiptError, match="receipt_page_limit"):
            receipt_images(output.getvalue())
    with pytest.raises(ReceiptError, match="receipt_file_invalid"):
        receipt_images(receipt_image("GIF"))


def test_image_reader_uses_explicit_message_types_with_real_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ReceiptRead(is_receipt=True, lines=[line("Tomatoes")])
    harness = Harness(
        body=response_body(
            output=[
                {
                    "id": "msg_receipt",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": expected.model_dump_json(),
                            "annotations": [],
                        }
                    ],
                }
            ]
        )
    )
    monkeypatch.setattr(
        receipt_reader, "build_async_credential", lambda _settings: SyntheticCredential()
    )
    original = AIProjectClient.get_openai_client

    def get_client(project: AIProjectClient, **kwargs: Any) -> AsyncOpenAI:
        kwargs["http_client"] = httpx2.AsyncClient(transport=httpx2.MockTransport(harness.handle))
        return original(project, **kwargs)

    monkeypatch.setattr(AIProjectClient, "get_openai_client", get_client)
    images = receipt_images(receipt_image())
    result = asyncio.run(receipt_reader.FoundryReceiptReader(foundry_settings()).read(images))
    assert result == expected
    payload = json.loads(harness.requests[0].content)
    assert all(message["type"] == "message" for message in payload["input"])
    assert payload["input"][1]["content"][0]["image_url"] == images[0]
    assert payload["store"] is False
    assert "agent_reference" not in payload and "tools" not in payload
