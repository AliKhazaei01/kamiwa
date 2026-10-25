"""Admin access, card layout with view counts, and paused credential rotation in test mode."""
from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from market import jobs
from market.models import Account, Category, Listing, Province, SellerPage, ViewEvent

STATIC = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
          "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}


@override_settings(STORAGES=STATIC)
class Phase4Tests(TestCase):
    def setUp(self):
        province = Province.objects.create(name="تهران")
        category = Category.objects.create(name="کامیون", slug="truck")
        common = dict(province=province, city="تهران", identity_status="approved", email_verified=True, mobile_verified=True,
                      subscription_until=timezone.now() + timedelta(days=10), first_name="کاربر", last_name="آزمایشی",
                      national_id="0123456789", birth_date=date(1990, 1, 1))
        self.buyer = Account.objects.create_user("buyerx", email="b@example.test", password="ValidComplexPass27", role="buyer", mobile="09120000001", **common)
        self.other = Account.objects.create_user("buyery", email="y@example.test", password="ValidComplexPass27", role="buyer", mobile="09120000002", **common)
        seller = Account.objects.create_user("sellerx", email="s@example.test", password="ValidComplexPass27", role="pro", mobile="09120000003", **common)
        SellerPage.objects.create(user=seller, slug="dealer", display_name="نمایشگاه", moderation_status="approved")
        self.listing = Listing.objects.create(owner=seller, title="آگهی", category=category, province=province, brand="Volvo",
                                              model="FH", year=1400, mileage=1, price=1, status="active", moderation_status="approved")
        self.boss = Account.objects.create_user("bossman", email="boss@example.test", password="ValidComplexPass27", role="staff",
                                                is_staff=True, is_superuser=True, mobile="09120000004")

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["mfa_verified"] = True
        session.save()

    def test_superuser_sidebar_links_to_admin_management(self):
        self.login(self.boss)
        html = self.client.get(reverse("market:staff")).content.decode()
        self.assertIn(reverse("market:staff_admins"), html.split('<nav aria-label="پنل کاربری">')[1].split("</nav>")[0])
        helper = Account.objects.create_user("helper", email="h@example.test", password="ValidComplexPass27", role="staff", is_staff=True, mobile="09120000005")
        helper.user_permissions.add(Permission.objects.get(codename="support_tickets"))
        self.login(helper)
        self.assertNotIn(reverse("market:staff_admins"), self.client.get(reverse("market:staff")).content.decode())

    def test_card_shows_views_and_favorite_outside_image(self):
        ViewEvent.objects.create(user=self.other, listing=self.listing)
        ViewEvent.objects.create(user=self.buyer, listing=self.listing, day=timezone.localdate() - timedelta(days=1))
        self.login(self.buyer)
        html = self.client.get(reverse("market:home"), {"province": "all"}).content.decode()
        card = html.split('<article class="listing-card">')[1].split("</article>")[0]
        media, body = card.split('<div class="listing-body">')
        self.assertNotIn("favorite-form", media)
        self.assertIn("card-favorite", body)
        self.assertIn("card-views", body)
        self.assertRegex(body, r"(?s)card-views[^>]*>.*?</svg>\s*۲</span>")
        self.assertContains(self.client.get(reverse("market:listing_detail", args=[self.listing.pk])), "بازدید")

    def test_footer_has_navigation_columns(self):
        html = self.client.get(reverse("market:landing")).content.decode()
        self.assertIn('footer-grid', html)
        self.assertIn(reverse("market:register"), html.split('class="site-footer"')[1])

    def test_rotation_is_paused_with_test_providers(self):
        self.assertTrue(settings.KAMIWA_TEST_PROVIDERS)
        self.assertFalse(settings.KAMIWA_ADMIN_ROTATION)
        Account.objects.filter(pk=self.boss.pk).update(date_joined=timezone.now() - timedelta(days=90))
        username = self.boss.username
        self.assertEqual(jobs.rotate_admin_credentials(), 0)
        self.assertEqual(Account.objects.get(pk=self.boss.pk).username, username)
