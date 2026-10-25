"""HTTP regression tests exercise authorization and tamper-resistant workflows."""
from datetime import timedelta
from io import BytesIO
from tempfile import TemporaryDirectory

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from PIL import Image
import pyotp
from captcha.models import CaptchaStore

from market import services
from market.models import (Account, Category, Favorite, InspectionReport, InspectionRequest,
    Listing, Payment, Plan, Province, SellerPage, Ticket, ExpertProfile)
from market.providers import queue_message


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class WebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.province = Province.objects.create(name="تهران")
        cls.other_province = Province.objects.create(name="فارس")
        cls.category = Category.objects.create(name="کامیون", slug="truck")
        cls.buyer = cls.account("buyeralpha")
        cls.other = cls.account("buyerbeta")
        cls.seller = cls.account("selleralpha", "pro")
        cls.page = SellerPage.objects.create(user=cls.seller, slug="dealer", display_name="نمایشگاه آزمایشی", moderation_status="approved")
        cls.listing = cls.ad("آگهی قابل نمایش")
        cls.plan = Plan.objects.create(role="buyer", name="خریدار", price=100000, duration_days=30)
        Plan.objects.create(role="pro", name="پرو", price=300000, duration_days=30)
        cls.staffuser = cls.account("supportstaff", "staff", is_staff=True)
        cls.staffuser.user_permissions.add(Permission.objects.get(codename="support_tickets"))

    @classmethod
    def account(cls, username, role="buyer", **extra):
        return Account.objects.create_user(username, email=f"{username}@example.test", password="ValidComplexPass27", role=role,
            first_name="کاربر", last_name="آزمایشی", province=cls.province, city="تهران", mobile="09" + str(100000000 + Account.objects.count()),
            national_id="0123456789", birth_date="1990-01-01", identity_status="approved", email_verified=True,
            mobile_verified=True, subscription_until=timezone.now() + timedelta(days=10), **extra)

    @classmethod
    def ad(cls, title, **extra):
        data = dict(owner=cls.seller, title=title, category=cls.category, province=cls.province, city="تهران", brand="Volvo", model="FH500", year=1402, mileage=100000, price=1000000, status="active", moderation_status="approved")
        data.update(extra)
        return Listing.objects.create(**data)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.buyer)

    def login_staff(self):
        self.client.force_login(self.staffuser)
        session = self.client.session
        session["mfa_verified"] = True
        session.save()

    def captcha(self):
        key = CaptchaStore.generate_key()
        return {"captcha_0": key, "captcha_1": CaptchaStore.objects.get(hashkey=key).response}

    def test_guest_cannot_browse_but_landing_is_public(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("market:landing")).status_code, 200)
        self.assertRedirects(self.client.get(reverse("market:home")), reverse("market:login") + "?next=/market/", fetch_redirect_response=False)

    def test_stored_listing_and_seller_markup_is_escaped(self):
        payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
        Listing.objects.filter(pk=self.listing.pk).update(description=payload)
        SellerPage.objects.filter(pk=self.page.pk).update(bio=payload, display_name=payload)
        for url in (reverse("market:listing_detail", args=[self.listing.pk]),
                    reverse("market:seller_page", args=[self.page.slug])):
            response = self.client.get(url)
            self.assertNotContains(response, payload)
            self.assertContains(response, '&lt;script&gt;alert(2)&lt;/script&gt;')
            self.assertIn("script-src 'self'", response['Content-Security-Policy'])
            self.assertNotIn("'unsafe-inline'", response['Content-Security-Policy'])

    def test_ticket_message_markup_stays_text(self):
        payload = '<svg onload="alert(1)"></svg>'
        result = self.client.post(reverse('market:ticket_create'), {'subject': 'XSS regression', 'body': payload})
        self.assertEqual(result.status_code, 302)
        response = self.client.get(result['Location'])
        self.assertContains(response, '&lt;svg onload=&quot;alert(1)&quot;&gt;&lt;/svg&gt;')
        self.assertNotContains(response, payload)

    def test_search_injection_payload_does_not_bypass_visibility(self):
        self.ad('Hidden marker', moderation_status='rejected')
        count = Listing.objects.count()
        for payload in ("' OR 1=1 --", "'; DROP TABLE market_listing; --", '<script>alert(3)</script>'):
            response = self.client.get(reverse('market:home'), {'q': payload, 'province': 'all'})
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'Hidden marker')
            self.assertNotContains(response, 'آگهی قابل نمایش')
            self.assertNotContains(response, '<script>alert(3)</script>')
            self.assertEqual(Listing.objects.count(), count)

    def test_entitlement_redirects_verification_then_subscription(self):
        self.buyer.identity_status = "pending"
        self.buyer.save(update_fields=["identity_status"])
        self.assertRedirects(self.client.get(reverse("market:home")), reverse("market:verify"), fetch_redirect_response=False)
        self.buyer.identity_status = "approved"
        self.buyer.subscription_until = timezone.now() - timedelta(days=1)
        self.buyer.save(update_fields=["identity_status", "subscription_until"])
        self.assertRedirects(self.client.get(reverse("market:home")), reverse("market:subscriptions"), fetch_redirect_response=False)

    def test_market_location_and_filters_only_return_eligible_listings(self):
        self.ad("دور از تهران", province=self.other_province)
        self.ad("پیش‌نویس محرمانه", moderation_status="pending")
        response = self.client.get(reverse("market:home"))
        self.assertContains(response, self.listing.title)
        self.assertNotContains(response, "پیش‌نویس محرمانه")
        self.assertNotContains(response, "دور از تهران")
        response = self.client.get(reverse("market:home"), {"province": "all", "q": "دور از"})
        self.assertContains(response, "دور از تهران")
        self.assertEqual(self.client.session["province_id"], "all")

    def test_hidden_listing_is_not_addressable_by_buyer(self):
        hidden = self.ad("آگهی ردشده", moderation_status="rejected")
        self.assertEqual(self.client.get(reverse("market:listing_detail", args=[hidden.pk])).status_code, 404)
        self.seller.subscription_until = timezone.now() - timedelta(days=1)
        self.seller.save(update_fields=["subscription_until"])
        self.assertEqual(self.client.get(reverse("market:listing_detail", args=[self.listing.pk])).status_code, 404)

    def test_expired_owner_can_still_open_own_ad_and_dashboard(self):
        self.seller.subscription_until = timezone.now() - timedelta(days=1)
        self.seller.save(update_fields=["subscription_until"])
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("market:listing_detail", args=[self.listing.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("market:dashboard")).status_code, 200)

    def test_compares_maximum_three_and_removes_without_get_mutation(self):
        adverts = [self.listing] + [self.ad(f"آگهی {number}") for number in range(3)]
        for ad in adverts:
            self.client.post(reverse("market:compare_toggle", args=[ad.pk]))
        self.assertEqual(len(self.client.session["compare_ids"]), 3)
        self.assertNotIn(adverts[-1].pk, self.client.session["compare_ids"])
        self.assertEqual(self.client.get(reverse("market:compare_toggle", args=[self.listing.pk])).status_code, 405)
        self.client.post(reverse("market:compare_toggle", args=[self.listing.pk]))
        self.assertEqual(len(self.client.session["compare_ids"]), 2)

    def test_unpublished_comparison_slot_is_freed_when_adding_another_listing(self):
        selected = [self.listing, self.ad("آگهی دوم"), self.ad("آگهی سوم")]
        replacement = self.ad("آگهی چهارم")
        for listing in selected:
            self.client.post(reverse("market:compare_toggle", args=[listing.pk]))
        self.listing.status = "archived"
        self.listing.save(update_fields=["status"])
        self.client.post(reverse("market:compare_toggle", args=[replacement.pk]))
        self.assertEqual(self.client.session["compare_ids"], [selected[1].pk, selected[2].pk, replacement.pk])

    def test_favorite_and_redirect_cannot_be_tampered(self):
        response = self.client.post(reverse("market:favorite_toggle", args=[self.listing.pk]), {"next": "https://attacker.example/path", "user": self.other.pk})
        self.assertEqual(response.url, reverse("market:home"))
        self.assertTrue(Favorite.objects.filter(user=self.buyer, listing=self.listing).exists())
        self.assertFalse(Favorite.objects.filter(user=self.other).exists())
        self.client.post(reverse("market:favorite_toggle", args=[self.listing.pk]))
        self.assertFalse(Favorite.objects.filter(user=self.buyer).exists())

    def test_csrf_is_required_for_favorite(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.buyer)
        response = csrf_client.post(reverse("market:favorite_toggle", args=[self.listing.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Favorite.objects.exists())

    def test_buyer_and_other_seller_cannot_edit_or_publish(self):
        self.assertEqual(self.client.get(reverse("market:listing_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("market:listing_status", args=[self.listing.pk]), {"status": "deleted"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("market:listing_edit", args=[self.listing.pk])).status_code, 404)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, "active")

    def test_subscription_checkout_owner_amount_and_replay(self):
        response = self.client.post(reverse("market:subscriptions"), {"plan": self.plan.pk, "amount": 1, "user": self.other.pk})
        payment = Payment.objects.get(user=self.buyer)
        self.assertEqual(payment.amount, self.plan.price)
        self.assertEqual(response.url, reverse("market:checkout", args=[payment.pk]))
        before = Account.objects.get(pk=self.buyer.pk).subscription_until
        self.client.post(response.url, {"outcome": "delay"})
        payment.refresh_from_db()
        self.assertEqual(payment.status, "delayed")
        self.client.post(response.url, {"outcome": "success"})
        self.client.post(response.url, {"outcome": "success"})
        self.assertEqual(Account.objects.get(pk=self.buyer.pk).subscription_until, before + timedelta(days=30))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(response.url).status_code, 404)
        self.assertEqual(self.client.post(response.url, {"outcome": "success"}).status_code, 404)

    def test_inspection_request_creates_separate_payment_and_is_private(self):
        response = self.client.post(reverse("market:inspection_create", args=[self.listing.pk]), {"coordination_note": "زمان هماهنگی", "requester": self.other.pk})
        inspection = InspectionRequest.objects.get()
        payment = Payment.objects.get(inspection=inspection)
        self.assertEqual(inspection.requester_id, self.buyer.pk)
        self.assertEqual(payment.kind, "inspection")
        self.assertEqual(response.url, reverse("market:checkout", args=[payment.pk]))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("market:inspection_detail", args=[inspection.pk])).status_code, 404)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("market:inspection_detail", args=[inspection.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("market:report_create", args=[inspection.pk])).status_code, 403)

    def test_tickets_are_private_and_support_can_reply_without_identity_access(self):
        response = self.client.post(reverse("market:ticket_create"), {"subject": "پرسش آزمایشی", "body": "متن خصوصی", "owner": self.other.pk})
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.owner_id, self.buyer.pk)
        self.assertEqual(response.url, reverse("market:ticket_detail", args=[ticket.pk]))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(response.url).status_code, 404)
        self.login_staff()
        self.assertEqual(self.client.post(response.url, {"body": "پاسخ پشتیبانی"}).status_code, 302)
        self.assertEqual(ticket.messages.count(), 2)
        self.assertEqual(self.client.get(reverse("market:private_file", args=["identity", self.buyer.pk])).status_code, 403)

    def test_test_outbox_never_exposes_another_users_payload(self):
        message = queue_message("email", self.other.email, {"code": "654321", "body": "خصوصی برای دیگری"})
        self.assertEqual(self.client.get(reverse("market:outbox_detail", args=[message.pk])).status_code, 404)
        response = self.client.get(reverse("market:outbox"))
        self.assertNotContains(response, "654321")
        self.login_staff()
        self.assertEqual(self.client.get(reverse("market:outbox_detail", args=[message.pk])).status_code, 404)

    def test_staff_sections_respect_granular_permissions(self):
        self.login_staff()
        response = self.client.get(reverse("market:staff"))
        self.assertNotIn("pending_users", response.context)
        self.assertEqual(self.client.post(reverse("market:staff"), {"action": "identity", "pk": self.buyer.pk, "approve": "1"}).status_code, 403)

    def test_login_case_insensitive_and_requires_captcha(self):
        self.client.logout()
        response = self.client.post(reverse("market:login"), {"username": self.buyer.username.upper(), "password": "ValidComplexPass27"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(reverse("market:login"), {"username": self.buyer.username.upper(), "password": "ValidComplexPass27", **self.captcha()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.buyer.pk)

    def test_document_form_retains_valid_upload(self):
        from market.forms import ReportForm
        image = Image.new("RGB", (10, 10), "white")
        buffer = BytesIO()
        image.save(buffer, "PNG")
        upload = SimpleUploadedFile("report.png", buffer.getvalue(), content_type="image/png")
        form = ReportForm({"price": 10000, "summary": "گزارش"}, {"private_document": upload})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIs(form.cleaned_data["private_document"], upload)

    def test_dashboard_does_not_show_other_accounts_statistics(self):
        # Buyers have no listings, so their overview carries no listing statistics at all.
        response = self.client.get(reverse("market:dashboard"))
        self.assertNotIn("listings", response.context)
        self.assertEqual(set(response.context["stats"]), {"days_remaining"})

    def test_final_appraisal_price_is_mandatory_but_draft_is_private(self):
        expert_user = self.account("expertalpha", role="expert")
        expert = ExpertProfile.objects.create(user=expert_user, province=self.province, credential="کارشناس آزمایشی")
        inspection = InspectionRequest.objects.create(listing=self.listing, requester=self.buyer, expert=expert, status="reported")
        report = InspectionReport.objects.create(inspection=inspection, expert=expert, submitted_by=expert_user, price=987654321, summary="گزارش خصوصی", status="pending")
        response = self.client.get(reverse("market:listing_detail", args=[self.listing.pk]))
        self.assertIsNone(response.context["report"])
        report.status = "final"
        report.finalized_at = timezone.now()
        report.save()
        response = self.client.get(reverse("market:listing_detail", args=[self.listing.pk]))
        self.assertEqual(response.context["report"].price, report.price)

    def test_unenrolled_staff_login_has_no_compulsory_mfa_setup(self):
        self.client.logout()
        response = self.client.post(reverse("market:login"), {"username": self.staffuser.username, "password": "ValidComplexPass27", **self.captcha()})
        self.assertRedirects(response, reverse("market:home"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("market:staff")).status_code, 200)
        self.assertRedirects(self.client.get(reverse("market:dashboard")), reverse("market:staff"))
        self.assertNotIn("mfa_setup", self.client.session)
        self.assertRedirects(self.client.get(reverse("market:mfa_setup")), reverse("market:profile"), fetch_redirect_response=False)
        self.assertNotIn("mfa_setup", self.client.session)
        response = self.client.get(reverse("market:profile"))
        self.assertContains(response, "فعال‌سازی اختیاری تأیید دومرحله‌ای")
        response = self.client.post(reverse("market:mfa_setup"), {"action": "start"})
        code = pyotp.TOTP(response.context["mfa_setup"]["secret"]).now()
        response = self.client.post(reverse("market:mfa_setup"), {"code": code})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.client.session.get("mfa_verified"))
        self.assertEqual(self.client.get(reverse("market:staff")).status_code, 200)

    def test_mfa_setup_requires_explicit_fresh_opt_in(self):
        setup_url = reverse("market:mfa_setup")
        self.assertRedirects(self.client.post(setup_url, {"code": "123456"}), reverse("market:profile"), fetch_redirect_response=False)
        self.assertNotIn("mfa_setup", self.client.session)
        self.client.post(setup_url, {"action": "start"})
        old = self.client.session["mfa_setup"]["secret"]
        self.client.post(setup_url, {"action": "start"})
        self.assertNotEqual(self.client.session["mfa_setup"]["secret"], old)
        session = self.client.session
        setup = session["mfa_setup"]
        setup["started_at"] = timezone.now().timestamp() - 601
        session["mfa_setup"] = setup
        session.save()
        self.assertRedirects(self.client.post(setup_url, {"code": pyotp.TOTP(setup["secret"]).now()}), reverse("market:profile"), fetch_redirect_response=False)
        self.buyer.refresh_from_db()
        self.assertFalse(self.buyer.mfa_secret_encrypted)
        self.assertNotIn("mfa_setup", self.client.session)
        for key, value in (("user_id", self.other.pk), ("session_version", self.buyer.session_version + 1)):
            with self.subTest(invalid_binding=key):
                self.client.post(setup_url, {"action": "start"})
                session = self.client.session
                setup = session["mfa_setup"]
                setup[key] = value
                session["mfa_setup"] = setup
                session.save()
                self.assertRedirects(self.client.post(setup_url, {"code": pyotp.TOTP(setup["secret"]).now()}), reverse("market:profile"), fetch_redirect_response=False)
                self.assertNotIn("mfa_setup", self.client.session)

    def test_enrolled_buyer_session_cannot_bypass_challenge_or_reopen_setup(self):
        from market.providers import encrypt_payload
        secret = pyotp.random_base32()
        self.buyer.mfa_secret_encrypted = encrypt_payload({"secret": secret})
        self.buyer.save(update_fields=["mfa_secret_encrypted"])
        for name in ("dashboard", "profile", "mfa_setup"):
            self.assertRedirects(self.client.get(reverse("market:" + name)), reverse("market:mfa_challenge"), fetch_redirect_response=False)
        response = self.client.post(reverse("market:mfa_challenge"), {"code": pyotp.TOTP(secret).now()})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.client.session.get("mfa_verified"))
        self.assertEqual(self.client.get(reverse("market:dashboard")).status_code, 200)

    def test_staff_authenticated_session_can_complete_mfa_challenge(self):
        from market.providers import encrypt_payload
        secret = pyotp.random_base32()
        self.staffuser.mfa_secret_encrypted = encrypt_payload({"secret": secret})
        self.staffuser.save(update_fields=["mfa_secret_encrypted"])
        self.client.force_login(self.staffuser)
        self.assertRedirects(self.client.get(reverse("market:staff")), reverse("market:mfa_challenge"), fetch_redirect_response=False)
        response = self.client.post(reverse("market:mfa_challenge"), {"code": pyotp.TOTP(secret).now()})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.client.session.get("mfa_verified"))

    def test_restricted_staff_cannot_read_other_accounts_tickets_or_staff_dashboard(self):
        ticket = services.create_ticket(self.buyer, "تیکت محرمانه", "پیام خصوصی")
        self.staffuser.restricted_until = timezone.now() + timedelta(days=1)
        self.staffuser.save(update_fields=["restricted_until"])
        self.login_staff()
        self.assertEqual(self.client.get(reverse("market:staff")).status_code, 403)
        self.assertEqual(self.client.get(reverse("market:ticket_detail", args=[ticket.pk])).status_code, 404)

    def test_seller_id_search_finds_approved_page_without_active_ads(self):
        self.listing.status = "archived"
        self.listing.save(update_fields=["status"])
        response = self.client.get(reverse("market:home"), {"view": "sellers", "q": "dealer"})
        self.assertEqual(list(response.context["sellers"]), [self.page])
        self.assertContains(response, self.page.display_name)

    def test_all_member_pages_render_with_current_template_contracts(self):
        for name in ("home", "verify", "profile", "password_change", "dashboard", "favorites", "compare", "subscriptions", "inspections", "tickets", "ticket_create", "outbox"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse("market:" + name)).status_code, 200)
        self.client.force_login(self.seller)
        for name, args in (("profile", []), ("dashboard", []), ("listing_create", []), ("listing_edit", [self.listing.pk]), ("listing_price", [self.listing.pk]), ("seller_page", [self.page.slug])):
            with self.subTest(seller_page=name):
                self.assertEqual(self.client.get(reverse("market:" + name, args=args)).status_code, 200)

    def test_configured_optional_mfa_is_enforced_for_buyer_login(self):
        from market.providers import encrypt_payload
        secret = pyotp.random_base32()
        self.buyer.mfa_secret_encrypted = encrypt_payload({"secret": secret})
        self.buyer.save(update_fields=["mfa_secret_encrypted"])
        self.client.logout()
        response = self.client.post(reverse("market:login"), {"username": self.buyer.username, "password": "ValidComplexPass27", **self.captcha()})
        self.assertRedirects(response, reverse("market:mfa_challenge"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(reverse("market:mfa_challenge"), {"code": pyotp.TOTP(secret).now()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.buyer.pk)

    def test_non_decimal_and_out_of_range_filter_ids_do_not_crash(self):
        for value in ("²", "9" * 100, "not-a-number"):
            with self.subTest(value=value):
                self.assertEqual(self.client.get(reverse("market:home"), {"province": value, "category": value}).status_code, 200)
                self.assertEqual(self.client.post(reverse("market:subscriptions"), {"plan": value}).status_code, 404)
        self.staffuser.user_permissions.add(Permission.objects.get(codename="review_identity"))
        self.login_staff()
        self.assertEqual(self.client.get(reverse("market:staff"), {"q": "²"}).status_code, 200)
        self.assertEqual(self.client.post(reverse("market:staff"), {"action": "identity", "pk": "²"}).status_code, 404)
        persian_code = str(self.buyer.internal_code).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
        # A full five-digit code (Persian digits allowed) opens that user's page directly.
        response = self.client.get(reverse("market:staff"), {"q": "#" + persian_code})
        self.assertRedirects(response, reverse("market:staff_user", args=[self.buyer.pk]))

    def test_database_ids_in_paths_are_bounded_before_querying(self):
        for value in ("0", "9223372036854775808", "9" * 100):
            for path in (f"/listings/{value}/", f"/checkout/{value}/", f"/private/identity/{value}/", f"/tickets/{value}/"):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)

    def test_dashboard_daily_statistics_are_scoped_to_listing_owner(self):
        from market.models import ViewEvent
        today = timezone.localdate()
        ViewEvent.objects.create(user=self.buyer, listing=self.listing, day=today)
        ViewEvent.objects.create(user=self.other, page=self.page, day=today)
        Favorite.objects.create(user=self.buyer, listing=self.listing)
        response = self.client.get(reverse("market:dashboard"))
        self.assertNotIn("activity_series", response.context)
        self.client.force_login(self.seller)
        response = self.client.get(reverse("market:dashboard"))
        self.assertEqual(len(response.context["activity_series"]), 7)
        self.assertEqual(response.context["activity_series"][-1]["views"], 1)
        self.assertEqual(response.context["activity_series"][-1]["page_views"], 1)
        self.assertEqual(response.context["activity_series"][-1]["favorites"], 1)

    def test_seller_search_paginates_sellers_instead_of_listings(self):
        for suffix in "bcdefghijklm":
            user = self.account("dealer" + suffix, role="pro")
            SellerPage.objects.create(user=user, slug="dealer" + suffix, display_name="نمایشگاه " + suffix, moderation_status="approved")
        response = self.client.get(reverse("market:home"), {"view": "sellers"})
        self.assertEqual(response.context["result_count"], 13)
        self.assertEqual(len(response.context["sellers"]), 12)
        response = self.client.get(reverse("market:home"), {"view": "sellers", "page": "2"})
        self.assertEqual(len(response.context["sellers"]), 1)
        self.assertEqual(response.context["page_obj"].number, 2)

    def test_expired_and_pending_listing_actions_match_allowed_workflows(self):
        self.client.force_login(self.seller)
        self.listing.moderation_status = "pending"
        self.listing.save(update_fields=["moderation_status"])
        response = self.client.get(reverse("market:listing_detail", args=[self.listing.pk]))
        self.assertFalse(response.context["can_interact"])
        self.assertTrue(response.context["can_request_inspection"])
        self.seller.subscription_until = timezone.now() - timedelta(days=1)
        self.seller.save(update_fields=["subscription_until"])
        response = self.client.get(reverse("market:listing_detail", args=[self.listing.pk]))
        self.assertFalse(response.context["can_request_inspection"])

    def test_listing_content_edit_preserves_published_version_until_staff_decides(self):
        self.client.force_login(self.seller)
        data = {"title": "مشخصات اصلاح‌شده", "category": self.category.pk, "brand": "Volvo", "model": "FH500", "year": 1402, "mileage": 100000, "province": self.province.pk, "city": "تهران", "price": 2000000, "negotiability": "fixed", "description": "شرح جدید"}
        response = self.client.post(reverse("market:listing_edit", args=[self.listing.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.title, "آگهی قابل نمایش")
        self.assertEqual(self.listing.pending_data["title"], data["title"])
        self.assertEqual(self.listing.price, 2000000)
        self.staffuser.user_permissions.add(Permission.objects.get(codename="moderate_listing"))
        self.login_staff()
        self.client.post(reverse("market:staff"), {"action": "listing", "pk": self.listing.pk, "approve": "0", "reason": "مشخصات بازبینی شود"})
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.title, "آگهی قابل نمایش")
        self.assertEqual(self.listing.moderation_status, "approved")
        self.assertEqual(self.listing.pending_data, {})

    def test_registration_verification_manual_review_and_paid_access_end_to_end(self):
        from market.models import OutboxMessage
        from market.providers import decrypt_payload
        image = BytesIO()
        Image.new("RGB", (10, 10), "white").save(image, "PNG")
        with TemporaryDirectory() as private_dir, self.settings(PRIVATE_MEDIA_ROOT=private_dir):
            self.client.logout()
            response = self.client.post(reverse("market:register"), {"role": "buyer", "username": "newbuyer", "password1": "NewComplexPassword29", "password2": "NewComplexPassword29", "first_name": "خریدار", "last_name": "جدید", "national_id": "1234567890", "birth_date_0": "11", "birth_date_1": "10", "birth_date_2": "1368", "mobile": "09121112233", "email": "newbuyer@example.test", "province": self.province.pk, "city": "تهران", "identity_document": SimpleUploadedFile("identity.png", image.getvalue(), content_type="image/png"), "birth_certificate": SimpleUploadedFile("birth.png", image.getvalue(), content_type="image/png"), "identity_status": "approved", "is_staff": "1"})
            self.assertRedirects(response, reverse("market:verify"), fetch_redirect_response=False)
            user = Account.objects.get(username="newbuyer")
            self.assertEqual(user.identity_status, "pending")
            self.assertFalse(user.is_staff)
            self.assertTrue(user.identity_document.storage.exists(user.identity_document.name))
            for channel, target in (("email", user.email), ("sms", user.mobile)):
                self.assertEqual(self.client.post(reverse("market:verify"), {"channel": channel, "action": "send"}).status_code, 302)
                message = OutboxMessage.objects.filter(channel=channel, target=target).latest("created_at")
                code = decrypt_payload(message.payload_ciphertext)["code"]
                self.assertEqual(self.client.post(reverse("market:verify"), {"channel": channel, "code": code, "action": "verify"}).status_code, 302)
            self.staffuser.user_permissions.add(Permission.objects.get(codename="review_identity"))
            self.login_staff()
            self.client.post(reverse("market:staff"), {"action": "identity", "pk": user.pk, "approve": "1"})
            user.refresh_from_db()
            self.assertTrue(user.is_verified)
            self.client.force_login(user)
            self.assertRedirects(self.client.get(reverse("market:home")), reverse("market:subscriptions"), fetch_redirect_response=False)
            response = self.client.post(reverse("market:subscriptions"), {"plan": self.plan.pk})
            self.client.post(response.url, {"outcome": "success"})
            self.assertEqual(self.client.get(reverse("market:home")).status_code, 200)

    def test_paid_inspection_assignment_expert_report_and_staff_finalization(self):
        response = self.client.post(reverse("market:inspection_create", args=[self.listing.pk]), {"coordination_note": "هماهنگی حضوری"})
        self.client.post(response.url, {"outcome": "success"})
        inspection = InspectionRequest.objects.get()
        expert_user = self.account("expertworkflow", role="expert")
        expert = ExpertProfile.objects.create(user=expert_user, province=self.province, credential="کارشناس آزمایشی")
        self.staffuser.user_permissions.add(Permission.objects.get(codename="finalize_report"))
        self.login_staff()
        self.assertEqual(self.client.get(reverse("market:staff")).status_code, 200)
        self.client.post(reverse("market:staff"), {"action": "assign", "pk": inspection.pk, "expert": expert.pk})
        self.client.force_login(expert_user)
        response = self.client.post(reverse("market:report_create", args=[inspection.pk]), {"price": 1234000, "summary": "قیمت و وضعیت فنی کارشناسی شد", "submitted_by": self.buyer.pk})
        self.assertEqual(response.status_code, 302)
        report = InspectionReport.objects.get()
        self.assertEqual(report.submitted_by_id, expert_user.pk)
        self.client.force_login(self.buyer)
        self.assertIsNone(self.client.get(reverse("market:inspection_detail", args=[inspection.pk])).context["report"])
        self.login_staff()
        self.client.post(reverse("market:staff"), {"action": "report", "pk": report.pk})
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("market:listing_detail", args=[self.listing.pk]))
        self.assertEqual(response.context["report"].price, 1234000)
        self.assertContains(response, "قیمت کارشناسی تأییدشده")

    def test_seller_registrations_require_role_specific_documents_and_safe_public_images(self):
        image = BytesIO()
        Image.new("RGB", (10, 10), "white").save(image, "PNG")
        image_bytes = image.getvalue()
        with TemporaryDirectory() as private_dir, TemporaryDirectory() as public_dir, self.settings(PRIVATE_MEDIA_ROOT=private_dir, MEDIA_ROOT=public_dir):
            for role, suffix in (("plus", "a"), ("pro", "b")):
                with self.subTest(role=role):
                    self.client.logout()
                    data = {"role": role, "username": "newdealer" + suffix, "password1": "NewComplexPassword29", "password2": "NewComplexPassword29", "first_name": "فروشنده", "last_name": "جدید", "national_id": "1234567890", "birth_date_0": "11", "birth_date_1": "10", "birth_date_2": "1368", "mobile": "0912111223" + ("4" if role == "plus" else "5"), "email": "dealer" + suffix + "@example.test", "province": self.province.pk, "city": "تهران", "identity_document": SimpleUploadedFile("identity.png", image_bytes, content_type="image/png"), "birth_certificate": SimpleUploadedFile("birth.png", image_bytes, content_type="image/png"), "slug": "new" + suffix, "display_name": "نمایشگاه جدید", "address": "تهران، نشانی آزمایشی", "activity": "کامیون و کشنده"}
                    response = self.client.post(reverse("market:register"), data)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(Account.objects.filter(username=data["username"]).exists())
                    data["identity_document"] = SimpleUploadedFile("identity.png", image_bytes, content_type="image/png")
                    data["birth_certificate"] = SimpleUploadedFile("birth.png", image_bytes, content_type="image/png")
                    if role == "plus":
                        data["avatar"] = SimpleUploadedFile("portrait.png", image_bytes, content_type="image/png")
                    else:
                        data["license_document"] = SimpleUploadedFile("license.png", image_bytes, content_type="image/png")
                    response = self.client.post(reverse("market:register"), data)
                    self.assertEqual(response.status_code, 302)
                    user = Account.objects.get(username=data["username"])
                    self.assertEqual(user.page.slug, data["slug"] + ("_personal" if role == "plus" else ""))
                    if role == "plus":
                        self.assertTrue(user.page.avatar.name.endswith(".jpg"))
                        self.assertEqual(user.page.display_name, "فروشنده جدید")
                    else:
                        self.assertTrue(user.page.license_document.storage.exists(user.page.license_document.name))

    def test_profile_page_edits_are_moderated_and_old_slug_redirects(self):
        private_dir = TemporaryDirectory()
        self.addCleanup(private_dir.cleanup)
        storage_settings = self.settings(PRIVATE_MEDIA_ROOT=private_dir.name)
        storage_settings.enable()
        self.addCleanup(storage_settings.disable)
        image = BytesIO()
        Image.new("RGB", (10, 10), "white").save(image, "PNG")
        self.page.address = "آدرس قبلی"
        self.page.activity = "فروش کامیون"
        self.page.license_document.save("test-license.png", SimpleUploadedFile("test-license.png", image.getvalue(), content_type="image/png"), save=False)
        self.page.save()
        self.client.force_login(self.seller)
        response = self.client.post(reverse("market:profile"), {"action": "page", "slug": "updated", "display_name": "نام نمایشگاه جدید", "bio": "درباره نمایشگاه", "address": "آدرس جدید", "activity": "فروش کشنده", "contact_telegram": "@newdealer"})
        self.assertEqual(response.status_code, 302)
        self.page.refresh_from_db()
        self.assertEqual(self.page.slug, "dealer")
        self.assertEqual(self.page.pending_data["slug"], "updated")
        self.assertEqual(self.page.contact_telegram, "")
        self.staffuser.user_permissions.add(Permission.objects.get(codename="moderate_listing"))
        self.login_staff()
        self.client.post(reverse("market:staff"), {"action": "page", "pk": self.page.pk, "approve": "1"})
        self.page.refresh_from_db()
        self.assertEqual(self.page.slug, "updated")
        self.assertEqual(self.page.contact_telegram, "newdealer")
        self.client.force_login(self.buyer)
        self.assertRedirects(self.client.get(reverse("market:seller_page", args=["dealer"])), reverse("market:seller_page", args=["updated"]), status_code=301)

    def test_dashboard_query_count_does_not_grow_with_listings(self):
        self.client.force_login(self.seller)
        self.client.get(reverse("market:dashboard"))
        with CaptureQueriesContext(connection) as few:
            self.assertEqual(self.client.get(reverse("market:dashboard")).status_code, 200)
        for index in range(8):
            self.ad(f"آگهی اضافه {index}", price=1000000 + index)
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(reverse("market:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "آگهی اضافه 7")
        self.assertEqual(len(many), len(few))

    def test_repeated_market_view_does_not_rewrite_session(self):
        url = reverse("market:home") + f"?province={self.province.pk}"
        self.assertEqual(self.client.get(url).status_code, 200)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse([q for q in queries if q["sql"].startswith("UPDATE") and "django_session" in q["sql"]])
        self.assertEqual(self.client.session["province_id"], self.province.pk)
