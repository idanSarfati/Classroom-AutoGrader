"""Turn a teacher-uploaded picture into the same WebP blob imported decks use.

Uploads are re-encoded instead of stored as they arrived, for three reasons:
a phone photo can carry an EXIF GPS position, a class set of phones should not
download 12-megapixel originals, and every slide image then has one mime type
and one serving path. The original bytes never reach the content store.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_EDGE = 1600
MAX_PIXELS = 40_000_000
QUALITY = 85

# Formats Pillow can decode safely. SVG is deliberately absent: it is a
# document that can carry script, and it is not a raster picture.
_ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"}
_ALPHA_MODES = {"RGBA", "LA", "PA", "P"}


class ImageUploadError(Exception):
    """The upload is not a picture the portal can safely store."""


@dataclass(frozen=True)
class PreparedImage:
    webp: bytes
    width: int
    height: int
    mime_type: str = "image/webp"


def prepare_slide_image(data: bytes) -> PreparedImage:
    """Validate, downscale and re-encode one uploaded picture.

    Raises :class:`ImageUploadError` with a Hebrew, teacher-facing message for
    anything that is empty, too large, not a raster image, or corrupt.
    """
    if not data:
        raise ImageUploadError("לא נבחרה תמונה.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageUploadError("התמונה גדולה מדי (מקסימום 8MB).")

    try:
        with Image.open(BytesIO(data)) as image:
            if (image.format or "").upper() not in _ALLOWED_FORMATS:
                raise ImageUploadError(
                    "אפשר להעלות רק תמונות: PNG, JPEG, WebP או GIF."
                )
            # The header is enough to reject a decompression bomb before any
            # pixel is decoded.
            if image.width * image.height > MAX_PIXELS:
                raise ImageUploadError("רזולוציית התמונה גבוהה מדי.")
            # An animated GIF contributes its first frame only.
            image.seek(0)
            mode = "RGBA" if image.mode in _ALPHA_MODES or "A" in image.getbands() else "RGB"
            frame = image.convert(mode)
    except ImageUploadError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ) as exc:
        raise ImageUploadError("הקובץ אינו תמונה תקינה.") from exc

    frame.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    buffer = BytesIO()
    frame.save(buffer, format="WEBP", quality=QUALITY, method=4)
    return PreparedImage(buffer.getvalue(), frame.width, frame.height)


__all__ = [
    "MAX_EDGE",
    "MAX_UPLOAD_BYTES",
    "ImageUploadError",
    "PreparedImage",
    "prepare_slide_image",
]