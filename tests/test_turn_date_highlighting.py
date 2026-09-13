from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TurnDateHighlightingTests(unittest.TestCase):
    def test_date_kind_styles_preserve_vibrant_turn_highlights(self):
        css = (ROOT / "static" / "date_exceptions.css").read_text(encoding="utf-8")

        self.assertIn(
            ".calendar-day.is-weekday.is-self-selectable:not(:disabled)",
            css,
        )
        self.assertIn(
            ".calendar-day.is-weekend.is-self-selectable:not(:disabled)",
            css,
        )
        self.assertIn(
            ".calendar-day.is-weekday.is-self-selectable:not(:disabled):focus-visible",
            css,
        )
        self.assertIn(
            ".calendar-day.is-weekend.is-self-selectable:not(:disabled):focus-visible",
            css,
        )


if __name__ == "__main__":
    unittest.main()
