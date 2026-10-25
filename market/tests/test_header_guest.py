"""Guests see a plain header; search and the category bar appear after sign-in."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from market.models import Account

STATIC = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
          "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}


@override_settings(STORAGES=STATIC)
class HeaderVisibilityTests(TestCase):
    def test_guest_header_has_no_search_or_category_bar(self):
        html = self.client.get(reverse("market:landing")).content.decode()
        header = html.split("<header")[1].split("</header>")[0]
        self.assertNotIn("header-search", header)
        self.assertNotIn("header-cats", header)
        self.assertIn('role="search"', html)
        self.assertIn('id="hero-q"', html)

    def test_signed_in_header_has_search_and_categories(self):
        user = Account.objects.create_user("buyerz", email="z@example.test", password="ValidComplexPass27", role="buyer",
                                           mobile="09120000009", subscription_until=timezone.now() + timedelta(days=5))
        self.client.force_login(user)
        header = self.client.get(reverse("market:dashboard")).content.decode().split("<header")[1].split("</header>")[0]
        self.assertIn("header-search", header)
        self.assertIn("header-cats", header)
