import base64
import warnings
from contextlib import closing
from io import BytesIO

import pillow_heif
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image, ImageOps, UnidentifiedImageError

from sous_chef.domain.receipts import MAX_RECEIPT_BYTES, ReceiptError

pillow_heif.register_heif_opener()
MAX_PIXELS = 24_000_000
MAX_PAGES = 3


def _image_url(image: Image.Image) -> str:
    if image.width * image.height > MAX_PIXELS or min(image.size) < 16:
        raise ReceiptError("receipt_image_size", "Use a legible image of at most 24 megapixels.")
    image = ImageOps.exif_transpose(image)
    image.thumbnail((2200, 6000))
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def receipt_images(content: bytes) -> tuple[str, ...]:
    if not content or len(content) > MAX_RECEIPT_BYTES:
        raise ReceiptError("receipt_file_size", "Upload a nonempty receipt of at most 10 MiB.")
    try:
        if content.startswith(b"%PDF-"):
            with pdfium.PdfDocument(content) as document:
                if not 1 <= len(document) <= MAX_PAGES:
                    raise ReceiptError(
                        "receipt_page_limit", "Upload a PDF with one to three pages."
                    )
                images = []
                for page in document:
                    with closing(page):
                        width, height = page.get_size()
                        if min(width, height) <= 0:
                            raise ValueError
                        scale = min(2, 2200 / width, 6000 / height)
                        with closing(page.render(scale=scale)) as bitmap:
                            images.append(_image_url(bitmap.to_pil()))
                return tuple(images)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP", "HEIF", "HEIC"}:
                    raise ValueError
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError
                return (_image_url(image),)
    except ReceiptError:
        raise
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        pdfium.PdfiumError,
    ):
        raise ReceiptError(
            "receipt_file_invalid", "Use a valid JPG, PNG, WebP, HEIC photo or PDF receipt."
        ) from None
