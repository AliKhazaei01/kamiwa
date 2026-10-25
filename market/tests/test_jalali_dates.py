from datetime import date, datetime, timezone as dt_timezone

from django.test import SimpleTestCase

from market.templatetags.market_numbers import jdate, jdatetime


class JalaliDateFilterTests(SimpleTestCase):
    def test_date_is_solar_hijri_with_persian_digits(self):
        self.assertEqual(jdate(date(2026, 10, 20)), "۱۴۰۵/۰۷/۲۸")

    def test_aware_datetime_uses_tehran_local_time(self):
        # 21:00 UTC is already the next day (00:30) in Tehran.
        value = datetime(2026, 10, 20, 21, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(jdate(value), "۱۴۰۵/۰۷/۲۹")
        self.assertEqual(jdatetime(value), "۱۴۰۵/۰۷/۲۹ ۰۰:۳۰")

    def test_plain_date_has_no_clock(self):
        self.assertEqual(jdatetime(date(2026, 3, 21)), "۱۴۰۵/۰۱/۰۱")

    def test_empty_values_render_blank(self):
        self.assertEqual(jdate(None), "")
        self.assertEqual(jdatetime(""), "")
