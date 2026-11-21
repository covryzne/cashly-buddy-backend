"""Photo input contract, bounded before parsing and decoded before provider access."""

from typing import Literal

from pydantic import Field

from cashly.schemas.extraction import StrictModel

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ENCODED_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_REQUEST_BYTES = MAX_ENCODED_BYTES + 65536


class ReceiptImage(StrictModel):
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    data: str = Field(min_length=4, max_length=MAX_ENCODED_BYTES)


class ReceiptExtractInput(StrictModel):
    image: ReceiptImage
    caption: str = Field(default="", max_length=1000)
