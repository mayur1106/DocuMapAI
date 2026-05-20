import unittest

from app.services.bookmark_namer import derive_bookmark_title_from_text


class TestBookmarkNamer(unittest.TestCase):
    def test_derive_bookmark_title_strips_leading_roman_noise(self) -> None:
        title = "787 31 I I Indicating Record..."

        normalized = derive_bookmark_title_from_text(title)

        self.assertEqual(normalized, "787_31_indicating_record")
