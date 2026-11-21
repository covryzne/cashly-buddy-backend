"""OCR over authenticated API + actual loopback HTTP; no hosted writes."""

import base64
import json
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from test_chat_extraction import CATEGORIES, NOW, draft, extraction, provider
from test_openrouter_extraction import provider as router_provider

from cashly.core.auth import Identity, require_identity
from cashly.core.errors import ApiError
from cashly.core.settings import Settings
from cashly.main import create_app
from cashly.schemas.extraction import ChatExtractInput, ModelExtraction
from cashly.schemas.receipt import ReceiptImage
from cashly.services.chat_extraction import apply_extraction
from cashly.services.receipt_extraction import validate_image


def photo(format="PNG"):
    buffer = BytesIO()
    Image.new("RGB", (24, 32), "white").save(buffer, format=format)
    return {
        "mime_type": Image.MIME[format],
        "data": base64.b64encode(buffer.getvalue()).decode(),
    }


def client_for(monkeypatch, **settings):
    monkeypatch.setattr("cashly.api.chat.database", lambda _: object())
    monkeypatch.setattr(
        "cashly.api.chat.extraction_context", lambda *_: (CATEGORIES, "Asia/Jakarta")
    )
    app = create_app(Settings(environment="dev", **settings))
    app.dependency_overrides[require_identity] = lambda: Identity(
        uuid4(), "ocr@example.invalid", "OCR Tester", datetime.now(UTC)
    )
    return TestClient(app)


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
def test_gemini_image_caption_and_shared_draft(monkeypatch, format):
    image = photo(format)
    with provider() as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        # Database is deliberately a plain object: this endpoint cannot persist expenses.
        with client_for(monkeypatch, gemini_api_key=SecretStr("fake")) as client:
            response = client.post(
                "/api/v1/receipts/extract", json={"image": image, "caption": "kopi"}
            )
    assert response.status_code == 200, response.text
    result = response.json()["data"]["entries"][0]
    assert result["draft"]["items"][0]["priceIdr"] == "18000"
    assert result["draft"]["merchant"] == "Kopi"
    request = calls[0]["body"]
    assert calls[0]["path"] == "/interactions"
    assert request["input"][1] == {"type": "image", **image}
    assert json.loads(request["input"][0]["text"])["message"] == "kopi"
    assert request["store"] is False
    assert "receipt, handwritten note or payment proof" in request["system_instruction"]
    assert response.headers["cache-control"] == "private, no-store"


def test_openrouter_uses_multimodal_input(monkeypatch):
    image = photo()
    with router_provider() as (url, calls):
        monkeypatch.setattr("cashly.services.openrouter_extraction.OPENROUTER_BASE_URL", url)
        with client_for(
            monkeypatch, ai_provider="openrouter", openrouter_api_key=SecretStr("fake")
        ) as client:
            response = client.post("/api/v1/receipts/extract", json={"image": image})
    assert response.status_code == 200, response.text
    request = calls[0]["body"]
    assert calls[0]["path"] == "/chat/completions"
    assert request["messages"][1]["content"][1]["image_url"]["url"] == (
        f"data:image/png;base64,{image['data']}"
    )


@pytest.mark.parametrize(
    "mutation", ["corrupt", "mismatch", "base64", "mime", "caption", "animation", "huge"]
)
def test_invalid_input_never_calls_provider(monkeypatch, mutation):
    image = photo()
    body = {"image": image}
    if mutation == "corrupt":
        image["data"] = base64.b64encode(b"not a photo").decode()
    elif mutation == "mismatch":
        image["mime_type"] = "image/jpeg"
    elif mutation == "base64":
        image["data"] = "%%%%"
    elif mutation == "mime":
        image["mime_type"] = "image/svg+xml"
    elif mutation == "caption":
        body["caption"] = "x" * 1001
    elif mutation == "animation":
        buffer = BytesIO()
        Image.new("RGB", (5, 5), "red").save(
            buffer, format="WEBP", save_all=True, append_images=[Image.new("RGB", (5, 5), "blue")]
        )
        image.update(mime_type="image/webp", data=base64.b64encode(buffer.getvalue()).decode())
    else:
        monkeypatch.setattr("cashly.api.chat.MAX_REQUEST_BYTES", 100)
    with provider() as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(monkeypatch, gemini_api_key=SecretStr("fake")) as client:
            response = client.post("/api/v1/receipts/extract", json=body)
    assert response.status_code == (413 if mutation == "huge" else 422)
    assert not calls
    assert image["data"] not in response.text


def test_pixel_limit_and_size_limit():
    buffer = BytesIO()
    Image.new("RGB", (6000, 6000)).save(buffer, format="PNG")
    with pytest.raises(ApiError) as error:
        validate_image(
            ReceiptImage(mime_type="image/png", data=base64.b64encode(buffer.getvalue()).decode())
        )
    assert error.value.status_code == 413


def test_total_evidence_mismatch_requires_clarification():
    result = extraction()
    result["changes"][0]["source_total_idr"] = "20000"
    data = apply_extraction(
        ChatExtractInput(message="nota"), ModelExtraction.model_validate(result), CATEGORIES, NOW
    )
    assert data.entries[0].draft is None
    assert data.entries[0].reason == "amount"
    assert "Total sumber berbeda" in data.reply


def test_total_only_payment_preview_has_one_summary_item():
    value = draft(
        merchant="Pembayaran toko",
        paymentMethod="QRIS",
        items=[{"id": "1", "name": "Pembayaran toko", "quantity": "1", "priceIdr": "24800"}],
    )
    result = extraction(value=value)
    result["changes"][0]["source_total_idr"] = "24800"
    data = apply_extraction(
        ChatExtractInput(message="bukti qris"),
        ModelExtraction.model_validate(result),
        CATEGORIES,
        NOW,
    )
    assert data.entries[0].issue is None
    assert len(data.entries[0].draft.items) == 1


def test_provider_failure_and_missing_auth_are_safe(monkeypatch):
    with provider(503, {"private": "never return raw output"}) as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(monkeypatch, gemini_api_key=SecretStr("fake")) as client:
            response = client.post("/api/v1/receipts/extract", json={"image": photo()})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "AI_UNAVAILABLE"
            assert response.json()["error"]["message"].startswith(
                "Mochi belum bisa membaca strukmu."
            )
            assert "never return" not in response.text
            client.app.dependency_overrides.clear()
            response = client.post("/api/v1/receipts/extract", json={"image": photo()})
            assert response.status_code == 401
    assert len(calls) == 1
