"""OCR helper: line merging, missing Tesseract and (if installed) a real pass."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401

import ocr


class OcrTest(unittest.TestCase):
    def test_merge_keeps_single_digits_and_drops_duplicates(self) -> None:
        merged = ocr._merge_lines(["5\nNOV\n|\nRampenlichtstunde", "NOV\n18:00 - 19:00"])
        self.assertEqual(merged.splitlines(), ["5", "NOV", "Rampenlichtstunde", "18:00 - 19:00"])

    def test_missing_tesseract_raises(self) -> None:
        with mock.patch("ocr.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                ocr.run_ocr("/does/not/matter.png")

    @unittest.skipUnless(ocr.tesseract_available() and ocr.PIL_AVAILABLE, "Tesseract oder Pillow nicht installiert")
    def test_real_ocr_pass(self) -> None:
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (900, 300), (16, 20, 48))
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.load_default(size=56)
        except TypeError:  # very old Pillow
            font = ImageFont.load_default()
        draw.text((40, 40), "GIRATINA", fill=(255, 255, 255), font=font)
        draw.text((40, 160), "4. NOV - 8. NOV", fill=(255, 255, 255), font=font)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.png"
            image.save(path)
            text = ocr.run_ocr(path, "eng", 60)
        self.assertIn("GIRATINA", text.upper())
        self.assertIn("NOV", text.upper())


if __name__ == "__main__":
    unittest.main()
