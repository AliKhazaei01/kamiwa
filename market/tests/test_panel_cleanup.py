from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from market.models import Account

# Fresh checkouts have no collected static manifest.
STATIC = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
          "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}


@override_settings(STORAGES=STATIC)
class PanelCleanupTests(TestCase):
    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["session_version"] = user.session_version
        session["mfa_verified"] = True
        session.save()

    def make(self, username, **extra):
        return Account.objects.create_user(username, email=f"{username}@example.test", password="ValidComplexPass27",
                                           mobile="0912000" + str(Account.objects.count()).zfill(4), **extra)

    def test_manager_overview_is_the_management_dashboard(self):
        admin = self.make("chiefadmin", role="staff", is_staff=True, is_superuser=True)
        self.login(admin)
        self.assertRedirects(self.client.get(reverse("market:dashboard")), reverse("market:staff"))
        html = self.client.get(reverse("market:staff")).content.decode()
        self.assertNotIn("بازدید آگهی‌های شما", html)
        self.assertNotIn("نمای کلی", html)
        self.assertNotIn(reverse("market:subscriptions") + '"', html.split('<nav aria-label="پنل کاربری">')[0].split("<header")[1].split("</header>")[0])

    def test_buyer_overview_has_no_seller_statistics(self):
        buyer = self.make("plainbuyer", role="buyer", subscription_until=timezone.now() + timedelta(days=5))
        self.login(buyer)
        response = self.client.get(reverse("market:dashboard"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("روز باقی‌مانده اشتراک", html)
        for text in ("بازدید آگهی‌های شما", "بازدید صفحه شما", "ذخیره آگهی‌های شما"):
            self.assertNotIn(text, html)

    def test_seller_overview_keeps_listing_statistics(self):
        seller = self.make("plusseller", role="plus", subscription_until=timezone.now() + timedelta(days=5))
        self.login(seller)
        html = self.client.get(reverse("market:dashboard")).content.decode()
        self.assertIn("بازدید آگهی‌های شما", html)
        self.assertIn("ذخیره آگهی‌های شما", html)
