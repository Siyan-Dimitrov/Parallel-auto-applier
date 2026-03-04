from __future__ import annotations

import base64
from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber

from src.utils.logging import get_logger

_cache: dict[str, str] = {}


def parse_resume(path: str | Path, ollama_model: str | None = None,
                 ollama_url: str = "http://localhost:11434") -> str:
    """Extract text from a PDF resume.

    First tries direct text extraction (pdfplumber).  If the PDF is
    image-based (scanned / designed), it renders each page to an image
    and sends it to the configured Ollama model for OCR.

    Results are cached in memory.
    """
    path = Path(path)
    log = get_logger()
    cache_key = str(path.resolve())

    if cache_key in _cache:
        log.debug("Using cached resume text for %s", path)
        return _cache[cache_key]

    if not path.exists():
        raise FileNotFoundError(f"Resume not found at {path}")

    log.info("Parsing resume: %s", path)

    # ── Try 1: direct text extraction with pdfplumber ──────────────
    text_parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

    if text_parts:
        full_text = "\n\n".join(text_parts)
        if len(full_text) >= 500:
            _cache[cache_key] = full_text
            log.info("Extracted %d characters from %d pages (text-based PDF)",
                     len(full_text), len(text_parts))
            return full_text
        log.info("Only %d characters extracted via text — too short, falling back to vision OCR",
                 len(full_text))
        text_parts.clear()

    # ── Try 2: render to image and send to Ollama for OCR ──────────
    log.info("No selectable text found — using Ollama vision to extract text from images")

    if not ollama_model:
        ollama_model = "kimi-k2.5:cloud"

    import ollama as ollama_client
    client = ollama_client.Client(host=ollama_url)

    doc = fitz.open(str(path))
    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            b64_img = base64.b64encode(img_bytes).decode()

            log.info("Sending page %d to %s for text extraction...", i + 1, ollama_model)
            resp = client.chat(
                model=ollama_model,
                messages=[{
                    "role": "user",
                    "content": (
                        "Extract ALL text from this resume page image. "
                        "Return the complete text content preserving the structure "
                        "(headings, bullet points, sections). Do not summarize or "
                        "add commentary — only return the extracted text."
                    ),
                    "images": [b64_img],
                }],
            )
            page_text = resp.message.content.strip()
            if page_text:
                text_parts.append(page_text)
    finally:
        doc.close()

    if not text_parts:
        raise ValueError(
            f"No text could be extracted from {path}. "
            "Neither text extraction nor Ollama vision succeeded."
        )

    full_text = "\n\n".join(text_parts)
    _cache[cache_key] = full_text
    log.info("Extracted %d characters from %d pages (via Ollama vision)",
             len(full_text), len(text_parts))
    return full_text
