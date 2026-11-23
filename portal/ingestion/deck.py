"""Render an exported Slides PDF into local WebP slide images.

``pypdfium2`` is used instead of a headless browser: it is small, fast,
Apache/BSD licensed, and produces one raster page per slide. The result is
pixel-faithful on a phone and never needs a Drive, Slides or PDF viewer URL.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import pypdfium2 as pdfium

MAX_PAGE_WIDTH = 1600
MAX_PAGES = 150
MAX_PAGE_SCALE = 2.0
WEBP_QUALITY = 82


class DeckRenderError(RuntimeError):
    """The PDF could not be rendered into slide images."""


@dataclass(frozen=True)
class RenderedPage:
    webp: bytes
    width: int
    height: int
    text: str = ""


def render_pdf_pages(
    pdf_bytes: bytes,
    *,
    max_width: int = MAX_PAGE_WIDTH,
    max_pages: int = MAX_PAGES,
    quality: int = WEBP_QUALITY,
) -> list[RenderedPage]:
    """Return one WebP image per PDF page.

    Documents above ``max_pages`` are rejected rather than silently truncated:
    a truncated deck shown to students would be worse than a clear error the
    teacher can act on.
    """
    if not pdf_bytes:
        raise DeckRenderError("The exported PDF is empty.")

    try:
        document = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:  # noqa: BLE001 - library raises several types
        raise DeckRenderError(f"Could not open the exported PDF: {exc}") from exc

    try:
        page_count = len(document)
        if page_count == 0:
            raise DeckRenderError("The exported PDF has no pages.")
        if page_count > max_pages:
            raise DeckRenderError(
                f"The deck has {page_count} pages; the limit is {max_pages}."
            )

        pages: list[RenderedPage] = []
        for index in range(page_count):
            page = document[index]
            try:
                width_pt, _height_pt = page.get_size()
                scale = min(max_width / max(width_pt, 1.0), MAX_PAGE_SCALE)
                image = page.render(scale=scale).to_pil()
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGB")
                buffer = BytesIO()
                image.save(buffer, format="WEBP", quality=quality, method=4)
                text = ""
                try:
                    text = (page.get_textpage().get_text_range() or "").strip()
                except Exception:  # noqa: BLE001 - text is a bonus, not required
                    text = ""
                pages.append(
                    RenderedPage(
                        webp=buffer.getvalue(),
                        width=image.width,
                        height=image.height,
                        text=text[:20_000],
                    )
                )
            finally:
                page.close()
        return pages
    except DeckRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface one clear message
        raise DeckRenderError(f"Could not render the deck: {exc}") from exc
    finally:
        document.close()
