"""Login notice, activity, staff search, section admins, profile change approval and expert panel."""
from datetime import date, timedelta
from io import BytesIO
from tempfile import TemporaryDirectory

from captcha.models import CaptchaStore
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from market import services
from market.models import (Account, ActivityDay, AuditEvent, Category, ExpertProfile, InspectionRequest,
    Listing, OutboxMessage, Province, SellerPage)
from market.providers import decrypt_payload

STATIC = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
          "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
def upload(name="doc.png"):
    buffer = BytesIO()
    Image.new("RGB", (40, 30), "white").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@override_settings(STORAGES=STATIC)
class Phase2Base(TestCase):
    def setUp(self):
        cache.clear()
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.private = override_settings(PRIVATE_MEDIA_ROOT=self.media.name, MEDIA_ROOT=self.media.name)
        self.private.enable()
        self.addCleanup(self.private.disable)
        storage = Account._meta.get_field("identity_document").storage
        # The private storage re-encodes images, so keep the names it actually stored.
        self.old_card = storage.save("identity/old.png", upload("old.png"))
        self.old_birth = storage.save("identity/old-birth.png", upload("old-birth.png"))
        self.province = Province.objects.create(name="تهران")
        self.other_province = Province.objects.create(name="فارس")
        self.category = Category.objects.create(name="کامیون", slug="truck")
        self.buyer = self.make("buyerone", "buyer", "09120000001")
        self.seller = self.make("sellerone", "pro", "09120000002")
        SellerPage.objects.create(user=self.seller, slug="dealer", display_name="نمایشگاه", moderation_status="approved")
        self.listing = Listing.objects.create(owner=self.seller, title="آگهی نمونه", category=self.category, province=self.province,
                                              brand="Volvo", model="FH", year=1400, mileage=1, price=1, status="active", moderation_status="approved")
        self.boss = self.make("bossman", "staff", "09120000003", is_staff=True, is_superuser=True)

    def make(self, username, role, mobile, **extra):
        return Account.objects.create_user(username, email=username + "@example.test", password="ValidComplexPass27", role=role,
            first_name="کاربر", last_name="آزمایشی", province=self.province, city="تهران", mobile=mobile, national_id="0123456789",
            birth_date=date(1990, 1, 1), identity_status="approved", email_verified=True, mobile_verified=True,
            identity_document=self.old_card, birth_certificate=self.old_birth,
            subscription_until=timezone.now() + timedelta(days=10), **extra)

    def staff_login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["mfa_verified"] = True
        session.save()


class LoginNoticeAndActivityTests(Phase2Base):
    def test_successful_login_queues_private_email_notice(self):
        key = CaptchaStore.generate_key()
        response = self.client.post(reverse("market:login"), {"username": "BuyerOne", "password": "ValidComplexPass27",
            "captcha_0": key, "captcha_1": CaptchaStore.objects.get(hashkey=key).response}, HTTP_USER_AGENT="TestBrowser/1")
        self.assertEqual(response.status_code, 302)
        message = OutboxMessage.objects.get(target="buyerone@example.test")
        payload = decrypt_payload(message.payload_ciphertext)
        self.assertEqual(payload["subject"], "ورود موفق به حساب کامیوا")
        self.assertIn("TestBrowser/1", payload["body"])
        self.assertNotIn("ValidComplexPass27", payload["body"])

    def test_failed_login_sends_nothing(self):
        key = CaptchaStore.generate_key()
        self.client.post(reverse("market:login"), {"username": "buyerone", "password": "wrong",
            "captcha_0": key, "captcha_1": CaptchaStore.objects.get(hashkey=key).response})
        self.assertFalse(OutboxMessage.objects.exists())

    def test_activity_is_sampled_and_online_state_reported(self):
        self.client.force_login(self.buyer)
        for _ in range(3):
            self.client.get(reverse("market:dashboard"))
        self.buyer.refresh_from_db()
        self.assertTrue(self.buyer.is_online)
        self.assertEqual(ActivityDay.objects.get(user=self.buyer).minutes, 2)
        Account.objects.filter(pk=self.buyer.pk).update(last_seen_at=timezone.now() - timedelta(minutes=3))
        self.client.get(reverse("market:dashboard"))
        self.assertEqual(ActivityDay.objects.get(user=self.buyer).minutes, 4)
        Account.objects.filter(pk=self.buyer.pk).update(last_seen_at=timezone.now() - timedelta(minutes=10))
        self.buyer.refresh_from_db()
        self.assertFalse(self.buyer.is_online)


class StaffSearchAndUserPageTests(Phase2Base):
    def test_code_search_opens_full_user_page_with_identity_and_all_listings(self):
        Listing.objects.create(owner=self.seller, title="آگهی حذف‌شده", category=self.category, province=self.province,
                               brand="Volvo", model="FH", year=1400, mileage=1, price=1, status="deleted", moderation_status="approved")
        self.staff_login(self.boss)
        response = self.client.get(reverse("market:staff"), {"q": f"#{self.seller.internal_code}"})
        self.assertRedirects(response, reverse("market:staff_user", args=[self.seller.pk]))
        page = self.client.get(reverse("market:staff_user", args=[self.seller.pk]))
        self.assertContains(page, "0123456789")
        self.assertContains(page, "آگهی نمونه")
        self.assertContains(page, "آگهی حذف‌شده")
        self.assertContains(page, reverse("market:private_file", args=["birth_certificate", self.seller.pk]))
        self.assertTrue(AuditEvent.objects.filter(action="staff.view_account", object_id=str(self.seller.pk)).exists())

    def test_partial_search_lists_matches_and_online_users_are_shown(self):
        Account.objects.filter(pk=self.buyer.pk).update(last_seen_at=timezone.now())
        self.staff_login(self.boss)
        response = self.client.get(reverse("market:staff"), {"q": "one"})
        self.assertEqual({a.username for a in response.context["search_results"]}, {"buyerone", "sellerone"})
        self.assertIn(self.buyer, list(response.context["online_users"]))

    def test_user_page_requires_identity_or_account_permission(self):
        support = self.make("supporter", "staff", "09120000004", is_staff=True)
        support.user_permissions.add(Permission.objects.get(codename="support_tickets"))
        self.staff_login(support)
        self.assertEqual(self.client.get(reverse("market:staff_user", args=[self.seller.pk])).status_code, 403)
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get(reverse("market:staff_user", args=[self.seller.pk])).status_code, 403)

    def test_account_manager_sees_activity_but_not_identity(self):
        manager = self.make("accountsman", "staff", "09120000005", is_staff=True)
        manager.user_permissions.add(Permission.objects.get(codename="manage_accounts"))
        self.staff_login(manager)
        page = self.client.get(reverse("market:staff_user", args=[self.seller.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "0123456789")
        self.assertNotContains(page, "آگهی نمونه")


class SectionAdminTests(Phase2Base):
    def grant(self, account, sections, **extra):
        return self.client.post(reverse("market:staff_admins"), {"action": "grant", "code": f"#{account.internal_code}", "sections": sections, **extra})

    def test_superuser_assigns_one_or_more_sections_and_revokes(self):
        self.staff_login(self.boss)
        self.grant(self.buyer, ["support_tickets", "moderate_listing"])
        self.buyer.refresh_from_db()
        self.assertTrue(self.buyer.is_staff)
        self.assertEqual(self.buyer.role, "staff")
        self.assertTrue(self.buyer.has_perm("market.support_tickets"))
        self.assertTrue(self.buyer.has_perm("market.moderate_listing"))
        self.assertFalse(self.buyer.has_perm("market.review_identity"))
        self.client.post(reverse("market:staff_admins"), {"action": "grant", "pk": self.buyer.pk, "sections": ["review_identity"]})
        self.buyer = Account.objects.get(pk=self.buyer.pk)
        self.assertTrue(self.buyer.has_perm("market.review_identity"))
        self.assertFalse(self.buyer.has_perm("market.support_tickets"))
        version = self.buyer.session_version
        self.client.post(reverse("market:staff_admins"), {"action": "revoke", "pk": self.buyer.pk})
        self.buyer = Account.objects.get(pk=self.buyer.pk)
        self.assertFalse(self.buyer.is_staff)
        self.assertFalse(self.buyer.has_perm("market.review_identity"))
        self.assertEqual(self.buyer.session_version, version + 1)
        self.assertTrue(AuditEvent.objects.filter(action="staff.sections").exists())

    def test_rules_seller_superuser_and_empty_selection(self):
        self.staff_login(self.boss)
        self.grant(self.seller, ["support_tickets"])
        self.assertFalse(Account.objects.get(pk=self.seller.pk).is_staff)
        self.grant(self.buyer, [])
        self.assertFalse(Account.objects.get(pk=self.buyer.pk).is_staff)
        other_boss = self.make("otherboss", "staff", "09120000006", is_staff=True, is_superuser=True)
        self.client.post(reverse("market:staff_admins"), {"action": "revoke", "pk": other_boss.pk})
        self.assertTrue(Account.objects.get(pk=other_boss.pk).is_staff)

    def test_only_superuser_can_manage_admins(self):
        helper = self.make("helperman", "staff", "09120000007", is_staff=True)
        for perm in Permission.objects.filter(codename__in=["review_identity", "manage_accounts"]):
            helper.user_permissions.add(perm)
        self.staff_login(helper)
        self.assertEqual(self.client.get(reverse("market:staff_admins")).status_code, 403)
        self.assertEqual(self.grant(self.buyer, ["support_tickets"]).status_code, 403)
        self.assertFalse(Account.objects.get(pk=self.buyer.pk).is_staff)


class ProfileChangeApprovalTests(Phase2Base):
    def submit(self, **changes):
        data = {"action": "identity", "username": "buyerone", "first_name": "کاربر", "last_name": "آزمایشی",
                "national_id": "0123456789", "birth_date_0": "11", "birth_date_1": "10", "birth_date_2": "1368",
                "mobile": "09120000001", "email": "buyerone@example.test", "province": self.province.pk, "city": "تهران"}
        data.update(changes)
        return self.client.post(reverse("market:profile"), data)

    def test_approved_account_keeps_old_data_until_staff_approve(self):
        self.client.force_login(self.buyer)
        self.submit(first_name="علی", city="شیراز", province=self.other_province.pk, email="new@example.test",
                    identity_document=upload("card.png"))
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.first_name, "کاربر")
        self.assertEqual(self.buyer.identity_status, "approved")
        self.assertTrue(self.buyer.is_entitled)
        self.assertEqual(set(self.buyer.pending_profile), {"first_name", "city", "province", "email", "identity_document"})
        self.assertContains(self.client.get(reverse("market:profile")), "در انتظار تأیید مدیر")
        download = self.client.get(reverse("market:private_file", args=["identity_pending", self.buyer.pk]))
        self.assertEqual(download.status_code, 200)
        # Consuming the stream closes the file without firing request_finished inside the test transaction.
        self.assertTrue(b"".join(download.streaming_content))
        self.staff_login(self.boss)
        staff_page = self.client.get(reverse("market:staff"))
        self.assertContains(staff_page, "درخواست‌های تغییر اطلاعات")
        self.assertContains(staff_page, "شیراز")
        self.client.post(reverse("market:staff"), {"action": "profile", "pk": self.buyer.pk, "approve": "1"})
        self.buyer.refresh_from_db()
        self.assertEqual((self.buyer.first_name, self.buyer.city, self.buyer.province_id), ("علی", "شیراز", self.other_province.pk))
        self.assertEqual(self.buyer.email, "new@example.test")
        self.assertFalse(self.buyer.email_verified)
        self.assertTrue(self.buyer.mobile_verified)
        self.assertEqual(self.buyer.pending_profile, {})
        self.assertNotEqual(self.buyer.identity_document.name, self.old_card)

    def test_rejection_needs_reason_discards_request_and_is_shown(self):
        self.client.force_login(self.buyer)
        self.submit(national_id="1111111111", birth_certificate=upload("birth.png"))
        pending_file = Account.objects.get(pk=self.buyer.pk).pending_profile["birth_certificate"]
        storage = Account._meta.get_field("birth_certificate").storage
        self.assertTrue(storage.exists(pending_file))
        with self.assertRaises(ValidationError):
            services.review_profile_change(self.boss, self.buyer, approve=False, reason="")
        services.review_profile_change(self.boss, self.buyer, approve=False, reason="تصویر خوانا نیست")
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.national_id, "0123456789")
        self.assertFalse(storage.exists(pending_file))
        self.client.force_login(self.buyer)
        self.assertContains(self.client.get(reverse("market:profile")), "تصویر خوانا نیست")

    def test_only_identity_reviewers_can_decide_and_duplicates_are_refused(self):
        self.client.force_login(self.buyer)
        self.submit(city="کرج")
        with self.assertRaises(PermissionDenied):
            services.review_profile_change(self.seller, self.buyer)
        response = self.submit(mobile="09120000002")
        self.assertContains(response, "برای حساب دیگری ثبت شده است")

    def test_unapproved_account_updates_directly(self):
        Account.objects.filter(pk=self.buyer.pk).update(identity_status="pending")
        self.client.force_login(self.buyer)
        self.submit(city="قم")
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.city, "قم")
        self.assertEqual(self.buyer.pending_profile, {})

    def test_identity_approval_requires_birth_certificate(self):
        Account.objects.filter(pk=self.buyer.pk).update(identity_status="pending", birth_certificate="")
        with self.assertRaises(ValidationError):
            services.approve_identity(self.boss, Account.objects.get(pk=self.buyer.pk))


class ExpertPanelTests(Phase2Base):
    def setUp(self):
        super().setUp()
        self.expert_user = self.make("expertone", "expert", "09120000008")
        self.expert = ExpertProfile.objects.create(user=self.expert_user, province=self.province, credential="آزمایشی")
        self.open = InspectionRequest.objects.create(listing=self.listing, requester=self.buyer, expert=self.expert, status="assigned")
        self.done = InspectionRequest.objects.create(listing=self.listing, requester=self.buyer, expert=self.expert, status="final")

    def test_expert_menu_and_lists_only_requests_and_history(self):
        self.client.force_login(self.expert_user)
        self.assertRedirects(self.client.get(reverse("market:dashboard")), reverse("market:inspections"))
        response = self.client.get(reverse("market:inspections"))
        self.assertEqual(list(response.context["inspections"]), [self.open])
        html = response.content.decode()
        self.assertIn("سابقه کارشناسی", html)
        for hidden in ("علاقه‌مندی‌ها", "اشتراک من", "نمای کلی", "پشتیبانی"):
            self.assertNotIn(hidden, html.split('<nav aria-label="پنل کاربری">')[1].split("</nav>")[0])
        history = self.client.get(reverse("market:inspections"), {"view": "history"})
        self.assertEqual(list(history.context["inspections"]), [self.done])

    def test_buyer_view_is_unchanged(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("market:inspections"))
        self.assertEqual(set(response.context["inspections"]), {self.open, self.done})
        self.assertContains(response, "علاقه‌مندی‌ها")
