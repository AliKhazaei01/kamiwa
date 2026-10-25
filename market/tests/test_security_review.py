"""Independent pre-release regressions for revoked privileges and MFA boundaries."""
from datetime import timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pyotp
from captcha.models import CaptchaStore
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from market import jobs, security, services
from market.admin import AccountAdmin, SellerPageAdmin
from market.files import authorized_private_file
from market.models import Account, CredentialRotation, ListingImage, OutboxMessage, SellerPage
from market.providers import decrypt_payload, encrypt_payload
from market.management.commands.seed_demo import _bootstrap_manager
from market.tests.test_domain import DomainFixtures


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class SecurityReviewTests(DomainFixtures, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.files = TemporaryDirectory()
        self.addCleanup(self.files.cleanup)
        self.storage_settings = override_settings(PRIVATE_MEDIA_ROOT=self.files.name)
        self.storage_settings.enable()
        self.addCleanup(self.storage_settings.disable)

    def login_first_factor(self, account):
        key = CaptchaStore.generate_key()
        return self.client.post(reverse("market:login"), {
            "username": account.username,
            "password": "UnitTestOnlyAa123",
            "captcha_0": key,
            "captcha_1": CaptchaStore.objects.get(hashkey=key).response,
        })

    def test_stock_admin_login_get_redirects_to_captcha_login(self):
        response = self.client.get(reverse("admin:login"))
        self.assertRedirects(response, reverse("market:login"))
        main_login = self.client.get(reverse("market:login"))
        self.assertIn("captcha", main_login.context["form"].fields)

    def test_stock_admin_login_post_cannot_authenticate_without_captcha(self):
        response = self.client.post(reverse("admin:login"), {
            "username": self.manager.username,
            "password": "UnitTestOnlyAa123",
            "next": reverse("admin:index"),
        })
        self.assertRedirects(response, reverse("market:login"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 302)

    def test_admin_uses_shared_captcha_login_without_compulsory_mfa(self):
        response = self.client.post(reverse("market:login"), {
            "username": self.manager.username,
            "password": "UnitTestOnlyAa123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("captcha", response.context["form"].errors)
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.login_first_factor(self.manager)
        self.assertRedirects(response, reverse("market:home"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.manager.pk)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        self.assertNotIn("mfa_setup", self.client.session)

    def configured_mfa(self, account):
        secret = pyotp.random_base32()
        account.mfa_secret_encrypted = encrypt_payload({"secret": secret})
        account.save(update_fields=["mfa_secret_encrypted"])
        return secret

    def admin_login(self, account):
        self.configured_mfa(account)
        self.client.force_login(account)
        session = self.client.session
        session["mfa_verified"] = True
        session["session_version"] = account.session_version
        session.save()

    def pending_license(self):
        field = self.owner_page._meta.get_field("license_document")
        name = field.storage.save("licenses/pending.pdf", SimpleUploadedFile("pending.pdf", b"%PDF-1.4\n%%EOF"))
        self.owner_page.pending_data = {"license_document": name, "avatar": "avatars/pending.png"}
        self.owner_page.save(update_fields=["pending_data"])
        return name

    def test_pending_license_denies_other_sellers_buyers_and_content_only_moderators(self):
        self.pending_license()
        moderator = self.make_user("contentmoderator", "staff", is_staff=True)
        moderator.user_permissions.add(Permission.objects.get(codename="moderate_listing"))
        url = reverse("market:private_file", args=["license_pending", self.owner_page.pk])
        for actor in (self.buyer, self.other, moderator):
            with self.subTest(actor=actor.username):
                if actor.is_staff:
                    self.admin_login(actor)
                else:
                    self.client.force_login(actor)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)

    def test_pending_license_download_requires_ownership_or_identity_permission(self):
        name = self.pending_license()
        reviewer = self.make_user("documentreviewer", "staff", is_staff=True)
        reviewer.user_permissions.add(Permission.objects.get(codename="review_identity"))
        for actor in (self.owner, reviewer):
            with self.subTest(actor=actor.username):
                if actor.is_staff:
                    self.admin_login(actor)
                else:
                    self.client.force_login(actor)
                response = self.client.get(reverse("market:private_file", args=["license_pending", self.owner_page.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4\n%%EOF")
                self.assertIn("attachment", response["Content-Disposition"])
                self.assertEqual(response["Content-Security-Policy"], "sandbox; default-src 'none'")
                self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn(name, str(SellerPageAdmin(SellerPage, admin.site).license_review(self.owner_page)))

    def test_missing_pending_license_does_not_fall_back_to_current_document(self):
        self.owner_page.license_document.save("current.pdf", SimpleUploadedFile("current.pdf", b"%PDF-1.4\n%%EOF"))
        with self.assertRaises(PermissionDenied):
            authorized_private_file(self.owner, "license_pending", self.owner_page.pk)

    def test_revoking_staff_status_revokes_private_identity_review_permission(self):
        self.pending_license()
        reviewer = self.make_user("revokeddocumentreviewer", "staff", is_staff=True)
        reviewer.user_permissions.add(Permission.objects.get(codename="review_identity"))
        reviewer.is_staff = False
        reviewer.save(update_fields=["is_staff"])
        self.client.force_login(reviewer)
        response = self.client.get(reverse("market:private_file", args=["license_pending", self.owner_page.pk]))
        if response.streaming:
            b"".join(response.streaming_content)
        self.assertEqual(response.status_code, 403, "Revoked staff status still permits downloading another user's private document.")

    def test_private_document_service_rechecks_cached_reviewer_permission(self):
        self.pending_license()
        reviewer = self.make_user("stalefilereviewer", "staff", is_staff=True)
        permission = Permission.objects.get(codename="review_identity")
        reviewer.user_permissions.add(permission)
        self.assertTrue(reviewer.has_perm("market.review_identity"))
        reviewer.user_permissions.remove(permission)
        with self.assertRaises(PermissionDenied):
            authorized_private_file(reviewer, "license_pending", self.owner_page.pk)

    def test_revoked_expert_role_cannot_download_previously_assigned_draft_report(self):
        listing, inspection, expert = self.inspection()
        report = services.submit_report(expert, inspection, {
            "price": listing.price, "summary": "Draft before role revocation",
            "private_document": SimpleUploadedFile("report.pdf", b"%PDF-1.4\n%%EOF"),
        })
        Account.objects.filter(pk=expert.pk).update(role="buyer")
        expert.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            authorized_private_file(expert, "report", report.pk)

    def test_administration_displays_current_and_pending_images_and_private_links(self):
        self.pending_license()
        listing = self.listing()
        approved = ListingImage.objects.create(listing=listing, image="listings/approved.jpg", approved=True)
        pending = ListingImage.objects.create(listing=listing, image="listings/pending.jpg")
        self.admin_login(self.manager)
        listing_page = self.client.get(reverse("admin:market_listing_change", args=[listing.pk]))
        self.assertContains(listing_page, approved.image.url)
        self.assertContains(listing_page, pending.image.url)
        self.assertContains(listing_page, "در انتظار تأیید")
        page_response = self.client.get(reverse("admin:market_sellerpage_change", args=[self.owner_page.pk]))
        self.assertContains(page_response, self.owner_page.avatar.url)
        self.assertContains(page_response, "/media/avatars/pending.png")
        self.assertContains(page_response, reverse("market:private_file", args=["license_pending", self.owner_page.pk]))
        identity_page = self.client.get(reverse("admin:market_account_change", args=[self.buyer.pk]))
        self.assertContains(identity_page, reverse("market:private_file", args=["identity", self.buyer.pk]))

    def test_ordinary_email_edit_requires_renewed_email_and_identity_verification(self):
        reviewer = self.make_user("emailreviewer", "staff", is_staff=True)
        reviewer.user_permissions.add(Permission.objects.get(codename="review_identity"))
        request = RequestFactory().post("/administration/market/account/")
        request.user = reviewer
        model_admin = AccountAdmin(Account, admin.site)
        self.assertNotIn("email", model_admin.get_readonly_fields(request, self.buyer))
        self.buyer.email = "updated-address@example.test"
        model_admin.save_model(request, self.buyer, SimpleNamespace(changed_data=["email"]), change=True)
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.email, "updated-address@example.test")
        self.assertFalse(self.buyer.email_verified)
        self.assertEqual(self.buyer.identity_status, "pending")
        self.assertTrue(self.buyer.mobile_verified)

    def test_manager_email_cannot_be_changed_through_admin_even_by_superuser(self):
        request = RequestFactory().post("/administration/market/account/")
        request.user = self.manager
        model_admin = AccountAdmin(Account, admin.site)
        self.assertIn("email", model_admin.get_readonly_fields(request, self.manager))
        target = Account.objects.get(pk=self.manager.pk)
        target.email = "bypass@example.test"
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(request, target, SimpleNamespace(changed_data=["email"]), change=True)
        self.manager.refresh_from_db()
        self.assertNotEqual(self.manager.email, "bypass@example.test")

    def test_restricted_staff_cannot_enroll_mfa_and_reenter_administration(self):
        self.manager.restricted_until = timezone.now() + timedelta(hours=1)
        self.manager.save(update_fields=["restricted_until"])
        response = self.login_first_factor(self.manager)
        self.assertEqual(response.status_code, 302)
        setup_page = self.client.post(reverse("market:mfa_setup"), {"action": "start"})
        self.assertEqual(setup_page.status_code, 403)
        self.manager.refresh_from_db()
        self.assertFalse(self.manager.mfa_secret_encrypted, "A restricted administrator enrolled MFA and obtained a verified session.")
        self.assertFalse(self.client.session.get("mfa_verified"))

    def test_restricted_account_reviewer_cannot_open_or_edit_accounts(self):
        reviewer = self.make_user("identityreviewer", "staff", is_staff=True)
        reviewer.user_permissions.add(Permission.objects.get(codename="review_identity"))
        reviewer.restricted_until = timezone.now() + timedelta(hours=1)
        reviewer.save(update_fields=["restricted_until"])
        request = RequestFactory().get("/administration/market/account/")
        request.user = reviewer
        model_admin = AccountAdmin(Account, admin.site)
        self.assertFalse(model_admin.has_view_permission(request, self.buyer))
        self.assertFalse(model_admin.has_change_permission(request, self.buyer))

    def test_account_admin_rechecks_cached_permissions_after_revocation(self):
        reviewer = self.make_user("cachedreviewer", "staff", is_staff=True)
        permission = Permission.objects.get(codename="review_identity")
        reviewer.user_permissions.add(permission)
        self.assertTrue(reviewer.has_perm("market.review_identity"))
        reviewer.user_permissions.remove(permission)
        request = RequestFactory().get("/administration/market/account/")
        request.user = reviewer
        model_admin = AccountAdmin(Account, admin.site)
        self.assertFalse(model_admin.has_module_permission(request))
        self.assertFalse(model_admin.has_view_permission(request, self.buyer))
        self.assertFalse(model_admin.has_change_permission(request, self.buyer))

    def test_restricted_superuser_cannot_add_accounts_or_reset_passwords(self):
        self.manager.restricted_until = timezone.now() + timedelta(hours=1)
        self.manager.save(update_fields=["restricted_until"])
        request = RequestFactory().post("/administration/market/account/add/")
        request.user = self.manager
        model_admin = AccountAdmin(Account, admin.site)
        self.assertFalse(model_admin.has_add_permission(request))
        with self.assertRaises(PermissionDenied):
            model_admin.user_change_password(request, str(self.buyer.pk))

    def test_password_reset_revokes_an_incomplete_mfa_login(self):
        secret = self.configured_mfa(self.manager)
        response = self.login_first_factor(self.manager)
        self.assertRedirects(response, reverse("market:mfa_challenge"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        otp = security.issue_otp(self.manager, "email", purpose="password_reset")
        message = OutboxMessage.objects.get(idempotency_key=f"otp:{otp.pk}")
        reset_code = decrypt_payload(message.payload_ciphertext)["code"]
        self.assertTrue(security.reset_password(self.manager, reset_code, "ChangedAccountSecret74"))
        self.client.post(reverse("market:mfa_challenge"), {"code": pyotp.TOTP(secret).now()})
        self.assertNotIn("_auth_user_id", self.client.session, "A first factor verified before password reset remained valid.")

    def test_unassigned_expert_cannot_submit_or_read_another_expert_report(self):
        listing, inspection, expert = self.inspection()
        report = services.submit_report(expert, inspection, {
            "price": listing.price, "summary": "Private pending report",
            "private_document": SimpleUploadedFile("report.pdf", b"%PDF-1.4\n%%EOF"),
        })
        unrelated = self.make_user("unrelatedexpert", "expert")
        with self.assertRaises(PermissionDenied):
            services.submit_report(unrelated, inspection, {"price": 1, "summary": "tamper"})
        with self.assertRaises(PermissionDenied):
            authorized_private_file(unrelated, "report", report.pk)
        with self.assertRaises(PermissionDenied):
            authorized_private_file(self.buyer, "report", report.pk)

    def test_inactive_expert_cannot_read_draft_report_after_access_revocation(self):
        listing, inspection, expert = self.inspection()
        report = services.submit_report(expert, inspection, {"price": listing.price, "summary": "Private revoked draft"})
        profile = expert.expert_profile
        profile.active = False
        profile.save(update_fields=["active"])
        self.client.force_login(expert)
        response = self.client.get(reverse("market:inspection_detail", args=[inspection.pk]))
        self.assertIn(response.status_code, {403, 404}, "Inactive experts retain access to assigned draft report details.")

    def test_reassigned_account_role_revokes_expert_report_write_access(self):
        listing, inspection, expert = self.inspection()
        Account.objects.filter(pk=expert.pk).update(role="buyer")
        with self.assertRaises(PermissionDenied):
            services.submit_report(expert, inspection, {"price": listing.price, "summary": "Revoked expert role"})

    def test_restricted_expert_cannot_be_assigned_to_a_request(self):
        listing, inspection, expert = self.inspection()
        Account.objects.filter(pk=expert.pk).update(restricted_until=timezone.now() + timedelta(hours=1))
        with self.assertRaises(ValidationError):
            services.assign_expert(self.manager, inspection, expert.expert_profile)

    def test_revoking_staff_flag_revokes_service_permissions(self):
        reviewer = self.make_user("formerreviewer", "staff", is_staff=True)
        reviewer.user_permissions.add(Permission.objects.get(codename="moderate_listing"))
        Account.objects.filter(pk=reviewer.pk).update(is_staff=False)
        with self.assertRaises(PermissionDenied):
            services.approve_listing(reviewer, self.listing())

    def test_existing_seller_or_expert_role_conversion_is_rejected(self):
        self.owner.role = "pro"
        with self.assertRaises(ValidationError):
            self.owner.clean()
        listing, inspection, expert = self.inspection()
        expert.role = "buyer"
        with self.assertRaises(ValidationError):
            expert.clean()

    def test_non_seller_accounts_cannot_keep_public_listings(self):
        listing = self.listing()
        Account.objects.filter(pk=self.owner.pk).update(role="buyer")
        self.assertFalse(services.visible_listings().filter(pk=listing.pk).exists())

    def test_plus_page_moderation_uses_current_identity_name(self):
        submitted = services.submit_page(self.owner, {"display_name": "نام جعلی", "bio": "درباره من"})
        self.assertEqual(submitted.pending_data["display_name"], self.owner.get_full_name())
        Account.objects.filter(pk=self.owner.pk).update(first_name="نام تأییدشده")
        approved = services.moderate_page(self.manager, submitted)
        self.owner.refresh_from_db()
        self.assertEqual(approved.display_name, self.owner.get_full_name())

    def test_seed_manager_identity_survives_rotation_and_email_change(self):
        def forbidden_creation():
            self.fail("Seed attempted to create a second administrator.")
        original = _bootstrap_manager(self.province, forbidden_creation)
        Account.objects.filter(pk=original.pk).update(username="rotatedmanager", email="new-owner@example.test")
        selected = _bootstrap_manager(self.province, forbidden_creation)
        self.assertEqual(selected.pk, original.pk)
        self.assertEqual(selected.email, "new-owner@example.test")
        self.assertEqual(Account.objects.filter(is_superuser=True).count(), 1)

    def test_seed_cannot_recreate_or_elevate_revoked_bootstrap_manager(self):
        _bootstrap_manager(self.province, lambda: self.fail("Manager already exists."))
        Account.objects.filter(pk=self.manager.pk).update(is_superuser=False)
        with self.assertRaises(CommandError):
            _bootstrap_manager(self.province, lambda: self.fail("Revoked manager was recreated."))

    def test_final_appraisal_is_immutable_under_seller_and_expert_edits(self):
        listing, inspection, expert = self.inspection()
        report = services.submit_report(expert, inspection, {"price": listing.price, "summary": "Approved report"})
        services.finalize_report(self.manager, report)
        with self.assertRaises(PermissionDenied):
            services.submit_report(self.owner, inspection, {"price": 1, "summary": "tamper"})
        with self.assertRaises(ValidationError):
            services.submit_report(expert, inspection, {"price": 1, "summary": "tamper"})
        services.update_price(self.owner, listing, 5)
        report.refresh_from_db()
        self.assertEqual(listing.appraisal_price, report.price)
        self.assertNotEqual(listing.appraisal_price, 5)

    @override_settings(KAMIWA_ADMIN_ROTATION=True)
    def test_failed_rotation_delivery_retries_without_second_credential_change(self):
        now = timezone.now()
        Account.objects.filter(pk=self.manager.pk).update(date_joined=now - timedelta(days=31))
        self.assertEqual(jobs.rotate_admin_credentials(now), 1)
        self.manager.refresh_from_db()
        version = self.manager.session_version
        credential_hash = self.manager.password
        with patch("market.jobs.TestMessageProvider.send", side_effect=ConnectionError("SIMULATED")):
            self.assertEqual(jobs.deliver_outbox(now=now + timedelta(seconds=1)), 0)
        self.assertEqual(jobs.rotate_admin_credentials(now + timedelta(minutes=5)), 0)
        self.assertEqual(jobs.deliver_outbox(now=now + timedelta(minutes=5)), 1)
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.password, credential_hash)
        self.assertEqual(self.manager.session_version, version)
        self.assertEqual(CredentialRotation.objects.filter(user=self.manager).count(), 1)
