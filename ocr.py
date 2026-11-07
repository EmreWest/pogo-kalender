"""Local OCR for infographics using the Tesseract command line tool.

Typical event infographics combine light text on dark backgrounds with dark text on
light boxes. A single Tesseract pass misses parts of either, so two passes
are combined: the original image, and an enlarged, inverted black-and-white
version. Pillow is only needed for the second pass; without it the first pass
still runs. OCR is an aid for matching and never a requirement.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

LOGGER = logging.getLogger("scrapedduck-ical.ocr")

try:  # Pillow is optional
    from PIL import Image, ImageOps  # type: ignore

    PIL_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    PIL_AVAILABLE = False


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _run_tesseract(image_path: Path, languages: str, timeout: int) -> str:
    env = dict(os.environ)
    # Keep OCR on one core so it never competes with the web server.
    env.setdefault("OMP_THREAD_LIMIT", "1")
    result = subprocess.run(
        ["tesseract", str(image_path), "stdout", "-l", languages, "--psm", "11"],
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"Tesseract-Fehler: {message[-1] if message else result.returncode}")
    return result.stdout.decode("utf-8", "replace")


def _prepare_inverted(source: Path, target: Path) -> None:
    with Image.open(source) as original:
        image = original.convert("L")
        longest = max(image.width, image.height)
        factor = 2.0 if longest <= 2200 else max(1.0, 4000 / longest)
        if factor > 1.0:
            image = image.resize((int(image.width * factor), int(image.height * factor)), Image.LANCZOS)
        image = ImageOps.invert(image)
        image = image.point(lambda value: 0 if value < 110 else 255)
        image.save(target, format="PNG")


def _merge_lines(texts: list[str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for text in texts:
        for line in text.splitlines():
            clean = " ".join(line.split())
            # Single digits are kept: date boxes often split "5" and "NOV".
            if not clean or (len(clean) < 2 and not clean.isdigit()):
                continue
            key = clean.casefold()
            if key in seen:
                continue
            seen.add(key)
            lines.append(clean)
    return "\n".join(lines)


def run_ocr(image_path: str | Path, languages: str = "deu+eng", timeout: int = 90, max_chars: int = 8000) -> str:
    """Return the combined OCR text of both passes (may be empty)."""
    path = Path(image_path)
    if not tesseract_available():
        raise RuntimeError("Tesseract ist nicht installiert.")
    texts: list[str] = []
    errors: list[str] = []
    try:
        texts.append(_run_tesseract(path, languages, timeout))
    except Exception as exc:
        errors.append(str(exc))
    if PIL_AVAILABLE:
        with tempfile.TemporaryDirectory(prefix="ocr-") as directory:
            prepared = Path(directory) / "inverted.png"
            try:
                _prepare_inverted(path, prepared)
                texts.append(_run_tesseract(prepared, languages, timeout))
            except Exception as exc:
                errors.append(str(exc))
    if not texts and errors:
        raise RuntimeError("; ".join(errors))
    return _merge_lines(texts)[:max_chars]
