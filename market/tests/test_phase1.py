"""Sign-up feedback, Jalali dates, numeric fields, AJAX favourites and staff navigation."""
from datetime import date, timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from market.forms import IdentityFields, JalaliDateField, digits_only
from market.jalali import format_jalali, to_gregorian, to_jalali
from market.models import Account, Category, Favorite, Listing, Province, SellerPage


class JalaliTests(TestCase):
    def test_known_dates_and_round_trip(self):
        self.assertEqual(to_jalali(date(2024, 3, 20)), (1403, 1, 1))
        self.assertEqual(to_jalali(date(1921, 3, 21)), (1300, 1, 1))
        self.assertEqual(to_gregorian(1368, 10, 11), date(1990, 1, 1))
        self.assertEqual(format_jalali(date(2024, 3, 20)), "۱ فروردین ۱۴۰۳")
        day = date(1921, 3, 21)
        while day < date(2031, 1, 1):
            self.assertEqual(to_gregorian(*to_jalali(day)), day)
            day += timedelta(days=97)

    def test_impossible_dates_are_rejected(self):
        for value in [(1403, 7, 31), (1402, 12, 30), (1403, 13, 1), (1403, 0, 1)]:
            with self.assertRaises(ValueError):
                to_gregorian(*value)

    def test_field_accepts_range_and_rejects_outside(self):
        field = JalaliDateField()
        self.assertEqual(field.clean(["11", "10", "1368"]), date(1990, 1, 1))
        self.assertEqual(field.clean(["1", "1", "1300"]), date(1921, 3, 21))
        for bad in (["30", "12", "1299"], ["31", "7", "1400"], ["", "1", "1400"]):
            with self.assertRaises(Exception):
                field.clean(bad)
        tomorrow = to_jalali(timezone.localdate() + timedelta(days=1))
        with self.assertRaises(Exception):
            field.clean([str(tomorrow[2]), str(tomorrow[1]), str(tomorrow[0])])

    def test_widget_shows_existing_date_in_jalali(self):
        html = IdentityFields(initial={"birth_date": date(1990, 1, 1)})["birth_date"].as_widget()
        self.assertIn('value="1368" selected', html)
        self.assertIn('value="10" selected', html)
        self.assertIn("دی", html)


class NumericFieldTests(TestCase):
    def test_persian_and_arabic_digits_are_normalised(self):
        self.assertEqual(digits_only("۰۹۱۲ ۳۴۵-٦٧٨٩"), "09123456789")
        form = IdentityFields(data={"national_id": "۱۲۳۴۵۶۷۸۹۰", "mobile": "۰۹۱۲۱۱۱۲۲۳۳"})
        form.is_valid()
        self.assertEqual(form.cleaned_data["national_id"], "1234567890")
        self.assertEqual(form.cleaned_data["mobile"], "09121112233")

    def test_letters_are_rejected_and_keyboard_is_numeric(self):
        form = IdentityFields(data={"national_id": "12345abcde", "mobile": "0912abc4567"})
        form.is_valid()
        self.assertIn("national_id", form.errors)
        self.assertIn("mobile", form.errors)
        for name in ("national_id", "mobile"):
            html = form[name].as_widget()
            self.assertIn('inputmode="numeric"', html)
            self.assertIn("data-digits-only", html)


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class SignupFeedbackTests(TestCase):
    def setUp(self):
        cache.clear()
        Account.objects.create_user("takenname", email="t@example.test", password="ValidComplexPass27")

    def check(self, value):
        return self.client.get(reverse("market:username_check"), {"username": value}).json()

    def test_username_check_reports_format_and_availability(self):
        self.assertFalse(self.check("TakenName")["ok"])
        self.assertIn("قبلاً", self.check("takenname")["message"])
        self.assertFalse(self.check("user1")["ok"])
        self.assertFalse(self.check("نام")["ok"])
        self.assertTrue(self.check("freshname")["ok"])

    def test_username_check_is_rate_limited(self):
        statuses = {self.client.get(reverse("market:username_check"), {"username": "abc"}).status_code for _ in range(61)}
        self.assertIn(429, statuses)

    def test_register_page_wires_live_checks(self):
        html = self.client.get(reverse("market:register")).content.decode()
        self.assertIn(reverse("market:username_check"), html)
        self.assertIn("data-password-rules", html)
        self.assertIn("data-password-match", html)
        self.assertIn('name="birth_date_2"', html)


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class FavoriteAndNavigationTests(TestCase):
    def setUp(self):
        province = Province.objects.create(name="تهران")
        category = Category.objects.create(name="کامیون", slug="truck")
        common = dict(province=province, city="تهران", identity_status="approved", email_verified=True, mobile_verified=True,
                      subscription_until=timezone.now() + timedelta(days=10), first_name="کاربر", last_name="آزمایشی",
                      national_id="0123456789", birth_date=date(1990, 1, 1))
        self.buyer = Account.objects.create_user("buyerx", email="b@example.test", password="ValidComplexPass27", role="buyer", mobile="09120000001", **common)
        seller = Account.objects.create_user("sellerx", email="s@example.test", password="ValidComplexPass27", role="pro", mobile="09120000002", **common)
        SellerPage.objects.create(user=seller, slug="dealer", display_name="نمایشگاه", moderation_status="approved")
        self.listing = Listing.objects.create(owner=seller, title="آگهی", category=category, province=province, brand="Volvo",
                                              model="FH", year=1400, mileage=1, price=1, status="active", moderation_status="approved")
        self.staff = Account.objects.create_user("staffer", email="st@example.test", password="ValidComplexPass27", role="staff", is_staff=True, mobile="09120000003")

    def test_fetch_toggle_returns_json_without_flash_message(self):
        self.client.force_login(self.buyer)
        url = reverse("market:favorite_toggle", args=[self.listing.pk])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.json(), {"favorited": True, "message": "آگهی به علاقه‌مندی‌ها اضافه شد."})
        self.assertTrue(Favorite.objects.filter(user=self.buyer, listing=self.listing).exists())
        self.assertFalse(self.client.post(url, HTTP_X_REQUESTED_WITH="fetch").json()["favorited"])
        self.assertFalse(Favorite.objects.exists())
        self.assertNotContains(self.client.get(reverse("market:favorites")), "علاقه‌مندی‌ها حذف شد")

    def test_plain_post_still_redirects(self):
        self.client.force_login(self.buyer)
        response = self.client.post(reverse("market:favorite_toggle", args=[self.listing.pk]), {"next": "/favorites/"})
        self.assertRedirects(response, "/favorites/", fetch_redirect_response=False)

    def test_staff_panel_has_no_subscription_link_but_buyer_does(self):
        self.client.force_login(self.staff)
        session = self.client.session
        session["mfa_verified"] = True
        session.save()
        html = self.client.get(reverse("market:dashboard")).content.decode()
        self.assertNotIn("اشتراک من", html)
        self.assertNotIn("روز باقی‌مانده اشتراک", html)
        self.client.force_login(self.buyer)
        self.assertContains(self.client.get(reverse("market:dashboard")), "اشتراک من")
