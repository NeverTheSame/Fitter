import unittest

from fitter.schemas import Category, ValidationError, parse_tryon_request
from tests.helpers import PNG_1PX

MAX = 1024 * 1024


class ParseTryonRequestTest(unittest.TestCase):
    def test_accepts_bare_garment_string(self):
        selfie, garment = parse_tryon_request({"selfie": PNG_1PX, "garment": PNG_1PX}, MAX)
        self.assertEqual(selfie, PNG_1PX)
        self.assertEqual(garment.category, Category.AUTO)

    def test_accepts_garment_object_with_http_image(self):
        _, garment = parse_tryon_request({
            "selfie": PNG_1PX,
            "garment": {"image": "https://shop.example/a.jpg", "category": "tops"},
        }, MAX)
        self.assertEqual(garment.category, Category.TOPS)

    def test_rejects_missing_fields(self):
        with self.assertRaises(ValidationError):
            parse_tryon_request({"garment": PNG_1PX}, MAX)
        with self.assertRaises(ValidationError):
            parse_tryon_request({"selfie": PNG_1PX}, MAX)

    def test_rejects_non_image_data_url(self):
        with self.assertRaises(ValidationError):
            parse_tryon_request({"selfie": "data:text/plain;base64,aGk=", "garment": PNG_1PX}, MAX)

    def test_rejects_oversized_image(self):
        with self.assertRaises(ValidationError):
            parse_tryon_request({"selfie": PNG_1PX, "garment": PNG_1PX}, max_image_bytes=10)

    def test_rejects_unknown_category(self):
        with self.assertRaises(ValidationError):
            parse_tryon_request({"selfie": PNG_1PX, "garment": {"image": PNG_1PX, "category": "hats"}}, MAX)


if __name__ == "__main__":
    unittest.main()
