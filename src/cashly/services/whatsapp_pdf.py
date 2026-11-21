import base64
import binascii
import re
import shutil
from io import BytesIO
from pathlib import Path
from subprocess import DEVNULL, PIPE, SubprocessError, run
from tempfile import TemporaryDirectory

from PIL import Image

from cashly.core.errors import ApiError
from cashly.schemas.receipt import MAX_IMAGE_BYTES, ReceiptImage


def pdf_receipt(encoded):
    error = ApiError(
        422, "INVALID_PDF", "PDF-nya belum bisa dibaca. Kirim satu struk yang jelas, ya."
    )
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_IMAGE_BYTES or not raw.startswith(b"%PDF-"):
            raise error
        with TemporaryDirectory(prefix="cashly-receipt-") as directory:
            path = Path(directory) / "receipt.pdf"
            path.write_bytes(raw)
            info = run(["pdfinfo", str(path)], stdout=PIPE, stderr=DEVNULL, timeout=8, check=True)
            pages = re.search(rb"^Pages:\s+([0-9]+)", info.stdout, re.MULTILINE)
            if not pages or not 1 <= int(pages[1]) <= 4:
                raise ApiError(
                    422, "PDF_TOO_MANY_PAGES", "Kirim PDF satu transaksi, maksimal 4 halaman, ya."
                )
            run(
                [
                    "pdftoppm",
                    "-f",
                    "1",
                    "-l",
                    "4",
                    "-scale-to",
                    "1500",
                    "-jpeg",
                    str(path),
                    str(Path(directory) / "page"),
                ],
                stdout=DEVNULL,
                stderr=DEVNULL,
                timeout=20,
                check=True,
            )
            images = []
            for page in sorted(Path(directory).glob("page-*.jpg")):
                with Image.open(page) as image:
                    images.append(image.convert("RGB"))
            if len(images) != int(pages[1]):
                raise error
            canvas = Image.new(
                "RGB",
                (max(page.width for page in images), sum(page.height for page in images)),
                "white",
            )
            offset = 0
            for page in images:
                canvas.paste(page, (0, offset))
                offset += page.height
            buffer = BytesIO()
            canvas.save(buffer, format="JPEG", quality=88)
    except FileNotFoundError:
        raise ApiError(
            503, "PDF_READER_UNAVAILABLE", "Mochi belum bisa membaca PDF. Coba lagi nanti, ya."
        ) from None
    except (binascii.Error, OSError, SubprocessError):
        raise error from None
    return ReceiptImage(mime_type="image/jpeg", data=base64.b64encode(buffer.getvalue()).decode())


def render_report(url):
    chromium = shutil.which("chromium") or shutil.which("google-chrome")
    unavailable = ApiError(
        503, "WHATSAPP_PDF_RENDER", "PDF belum bisa dibuat. Coba lagi nanti, ya."
    )
    if not chromium:
        raise unavailable
    try:
        with TemporaryDirectory(prefix="cashly-report-") as directory:
            path = Path(directory) / "laporan.pdf"
            run(
                [
                    chromium,
                    "--headless",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    f"--user-data-dir={directory}/profile",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={path}",
                    url,
                ],
                stdout=DEVNULL,
                stderr=DEVNULL,
                timeout=55,
                check=True,
            )
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ApiError(
                    422,
                    "WHATSAPP_PDF_TOO_LARGE",
                    "PDF terlalu besar. Coba pilih periode lebih pendek.",
                )
            text = run(
                ["pdftotext", str(path), "-"], stdout=PIPE, stderr=DEVNULL, timeout=10, check=True
            )
            if b"Rincian transaksi per kategori" not in text.stdout:
                raise unavailable
            return base64.b64encode(path.read_bytes()).decode("ascii")
    except (OSError, SubprocessError):
        raise unavailable from None
