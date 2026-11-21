"""Bounded, decoded image input. No file persistence or transaction writes."""

import base64
import binascii
import warnings
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from cashly.core.errors import ApiError
from cashly.schemas.extraction import ChatExtractInput
from cashly.schemas.receipt import MAX_IMAGE_BYTES, ReceiptExtractInput, ReceiptImage
from cashly.services.chat_extraction import extract_chat, invalid_result


def validate_image(image: ReceiptImage):
    try:
        raw = base64.b64decode(image.data, validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ApiError(413, "IMAGE_TOO_LARGE", "Ukuran foto maksimal 10 MB.")
        expected = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as decoded:
                if decoded.format != expected[image.mime_type]:
                    raise ValueError("Mismatched image format")
                if decoded.width * decoded.height > 32000000:
                    raise ApiError(413, "IMAGE_TOO_LARGE", "Foto maksimal 32 megapiksel.")
                if getattr(decoded, "n_frames", 1) != 1:
                    raise ValueError("Animated image")
                decoded.verify()
            # verify checks structure; load also checks the compressed pixel stream.
            with Image.open(BytesIO(raw)) as decoded:
                decoded.load()
        return image
    except ApiError:
        raise
    except (
        ValueError,
        OSError,
        binascii.Error,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        raise ApiError(
            422, "INVALID_IMAGE", "Foto tidak bisa dibaca. Pilih JPEG, PNG, atau WebP yang valid."
        ) from None


async def extract_receipt(
    settings,
    client,
    body: ReceiptExtractInput,
    categories,
    timezone,
    request_id,
    *,
    message_at=None,
):
    text = ChatExtractInput(message=body.caption or "Baca pengeluaran dari foto ini.")
    text._whatsapp_message_at = message_at
    data = await extract_chat(
        settings,
        client,
        text,
        categories,
        timezone,
        request_id,
        media=body.image,
    )
    # The current photo preview represents one transaction; never silently discard extras.
    if len(data.entries) > 1:
        raise invalid_result()
    return data
