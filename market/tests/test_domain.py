"""Behavior and regression tests, including real PostgreSQL contention scenarios."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from market import services as s
from market.models import (Account, AuditEvent, Category, ExpertProfile, Favorite,
                           InspectionReport, InspectionRequest, Listing, Payment,
                           Plan, PriceHistory, Province, SellerPage, SlugHistory, ViewEvent)


class DomainFixtures:
    def setUp(self):
        super().setUp()
        self.province = Province.objects.create(name="تهران")
        self.other_province = Province.objects.create(name="اصفهان")
        self.category = Category.objects.create(slug="truck", name="کامیون")
        self.subcategory = Category.objects.create(slug="tipper", name="کمپرسی", parent=self.category)
        self.other_category = Category.objects.create(slug="trailer", name="تریلر")
        self.owner = self.make_user("seller", "plus")
        self.buyer = self.make_user("buyer", "buyer")
        self.other = self.make_user("another", "plus")
        self.manager = self.make_user("manager", "staff", is_staff=True, is_superuser=True)
        self.owner_page = SellerPage.objects.create(user=self.owner, slug="seller_personal", display_name="فروشنده آزمایشی", avatar="avatars/test.png", moderation_status="approved")
        SellerPage.objects.create(user=self.other, slug="another_personal", display_name="دیگر", avatar="avatars/test.png", moderation_status="approved")
        self.plan = Plan.objects.create(role="buyer", name="خریدار", price=1000, duration_days=30, inspection_fee=2000)
        Plan.objects.create(role="plus", name="پلاس", price=2000, duration_days=30, inspection_fee=2000)
        Plan.objects.create(role="pro", name="پرو", price=3000, duration_days=30, inspection_fee=2000)

    def make_user(self, name, role, **kwargs):
        count = Account.objects.count()
        return Account.objects.create_user(username=name, password="UnitTestOnlyAa123", role=role,
            first_name="کاربر", last_name="آزمایشی", email=name + "@example.test", mobile="09" + str(count).zfill(9),
            national_id="1234567890", birth_date=date(1990, 1, 1), province=self.province, city="تهران",
            identity_document="identity/test.png", birth_certificate="identity/birth.png", identity_status="approved", email_verified=True,
            mobile_verified=True, subscription_until=timezone.now() + timedelta(days=30), **kwargs)

    def data(self, **kwargs):
        return {"title": "کشنده نمونه", "category": self.category, "province": self.province,
                "brand": "Volvo", "model": "FH500", "year": 1401, "mileage": 100000,
                "price": Decimal(9000000000), "description": "توضیحات نمونه", **kwargs}

    def listing(self, **kwargs):
        values = self.data(**kwargs)
        values.setdefault("owner", self.owner)
        values.setdefault("moderation_status", "approved")
        return Listing.objects.create(**values)

    def inspection(self):
        listing = self.listing()
        request = s.request_inspection(self.buyer, listing)
        s.settle_payment(s.create_payment(self.buyer, "inspection", inspection=request))
        expert_user = self.make_user("expert", "expert")
        expert = ExpertProfile.objects.create(user=expert_user, province=self.province, credential="آزمایشی")
        request = s.assign_expert(self.manager, request, expert)
        return listing, request, expert_user


class DomainTests(DomainFixtures, TestCase):
    def test_guests_and_unverified_or_expired_accounts_cannot_browse(self):
        self.listing()
        for user in [AnonymousUser(), self.buyer]:
            if user == self.buyer:
                Account.objects.filter(pk=user.pk).update(email_verified=False)
            with self.assertRaises(PermissionDenied):
                s.visible_listings(user)
        Account.objects.filter(pk=self.buyer.pk).update(email_verified=True, subscription_until=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(PermissionDenied):
            s.require_entitled(self.buyer)

    def test_expired_restricted_and_unapproved_seller_hidden(self):
        ad = self.listing()
        self.assertTrue(s.visible_listings(self.buyer).filter(pk=ad.pk).exists())
        Account.objects.filter(pk=self.owner.pk).update(restricted_until=timezone.now() + timedelta(hours=1))
        self.assertFalse(s.visible_listings(self.buyer).exists())
        Account.objects.filter(pk=self.owner.pk).update(restricted_until=None, subscription_until=timezone.now() - timedelta(hours=1))
        self.assertFalse(s.visible_listings().exists())
        Account.objects.filter(pk=self.owner.pk).update(subscription_until=timezone.now() + timedelta(hours=1))
        SellerPage.objects.filter(user=self.owner).update(moderation_status="pending")
        self.assertFalse(s.visible_listings().exists())

    def test_buyer_cannot_publish_or_override_role_owner_approval(self):
        with self.assertRaises(PermissionDenied):
            s.submit_listing(self.buyer, self.data(owner=self.owner, moderation_status="approved"))
        ad = s.submit_listing(self.owner, self.data(owner=self.other, moderation_status="approved", status="sold"))
        self.assertEqual(ad.owner, self.owner)
        self.assertEqual(ad.moderation_status, "pending")
        self.assertEqual(ad.status, "active")
        self.assertFalse(s.visible_listings().filter(pk=ad.pk).exists())

    def test_moderation_preserves_old_content_until_approved(self):
        ad = self.listing()
        old_title = ad.title
        edited = s.submit_listing_edit(self.owner, ad, self.data(title="عنوان جدید"))
        self.assertEqual(edited.title, old_title)
        self.assertEqual(s.visible_listings().get(pk=ad.pk).title, old_title)
        self.assertEqual(edited.pending_data["title"], "عنوان جدید")
        approved = s.approve_listing(self.manager, edited)
        self.assertEqual(approved.title, "عنوان جدید")
        self.assertEqual(approved.pending_data, {})

    def test_rejected_edit_keeps_previous_approved_version(self):
        ad = self.listing()
        edited = s.submit_listing_edit(self.owner, ad, self.data(title="عنوان ردشده"))
        rejected = s.approve_listing(self.manager, edited, approve=False, reason="مشخصات اشتباه است")
        self.assertEqual(rejected.moderation_status, "approved")
        self.assertEqual(rejected.title, ad.title)
        self.assertTrue(rejected.rejection_reason)
        with self.assertRaises(PermissionDenied):
            s.approve_listing(self.buyer, rejected)

    def test_staff_edits_are_versioned_and_audited(self):
        ad = self.listing()
        edited = s.submit_listing_edit(self.manager, ad, self.data(title="اصلاح مدیر"))
        self.assertEqual(edited.title, ad.title)
        self.assertEqual(edited.pending_data["title"], "اصلاح مدیر")
        self.assertTrue(AuditEvent.objects.filter(actor=self.manager, action="listing.submit").exists())

    def test_price_history_and_owner_permissions(self):
        ad = self.listing()
        with self.assertRaises(PermissionDenied):
            s.update_price(self.other, ad, 1)
        s.update_price(self.owner, ad, 8000000000)
        history = PriceHistory.objects.get(listing=ad)
        self.assertEqual(history.old_price, 9000000000)
        self.assertEqual(history.new_price, 8000000000)
        ad.refresh_from_db()
        self.assertEqual(ad.price, 8000000000)
        for invalid in ["NaN", "Infinity", "-1", "1.5", "1000000000000000000"]:
            with self.assertRaises(ValidationError):
                s.update_price(self.owner, ad, invalid)

    def test_quota_includes_pending_and_subcategories(self):
        for i in range(24):
            self.listing(title=str(i), category=self.subcategory)
        final = s.submit_listing(self.owner, self.data())
        with self.assertRaises(ValidationError):
            s.submit_listing(self.owner, self.data())
        self.assertEqual(s.quota_usage(self.owner)[0]["remaining"], 0)
        s.change_listing_status(self.owner, final, "archived")
        self.assertEqual(s.quota_usage(self.owner)[0]["remaining"], 1)
        self.assertEqual(s.submit_listing(self.owner, self.data()).moderation_status, "pending")

    def test_pending_category_change_reserves_destination(self):
        for i in range(25):
            self.listing(title=str(i), category=self.other_category)
        ad = self.listing()
        with self.assertRaises(ValidationError):
            s.submit_listing_edit(self.owner, ad, self.data(category=self.other_category))

    def test_approval_rechecks_quota_and_subscription(self):
        for i in range(25):
            self.listing(title=str(i))
        overflow = self.listing(moderation_status="pending")
        with self.assertRaises(ValidationError):
            s.approve_listing(self.manager, overflow)
        Account.objects.filter(pk=self.owner.pk).update(subscription_until=timezone.now() - timedelta(days=1))
        with self.assertRaises(ValidationError):
            s.approve_listing(self.manager, overflow)

    def test_plus_sold_retention_uses_sold_time(self):
        old = None
        for i in range(50):
            ad = self.listing(title=str(i), status="sold", sold_at=timezone.now() - timedelta(days=60-i))
            old = old or ad
        new = self.listing()
        s.change_listing_status(self.owner, new, "sold")
        old.refresh_from_db()
        self.assertEqual(old.status, "deleted")
        self.assertEqual(Listing.objects.filter(owner=self.owner, status="sold").count(), 50)

    def test_terminal_statuses_cannot_be_reactivated(self):
        for status in ["sold", "disabled", "deleted"]:
            ad = self.listing(status=status)
            with self.assertRaises(ValidationError):
                s.change_listing_status(self.owner, ad, "active")
        ad = self.listing(status="paused")
        self.assertEqual(s.change_listing_status(self.owner, ad, "active").status, "active")
        sold = self.listing(status="sold", sold_at=timezone.now())
        archived = s.change_listing_status(self.owner, sold, "archived")
        with self.assertRaises(ValidationError):
            s.change_listing_status(self.owner, archived, "active")
        disabled = self.listing(status="disabled")
        with self.assertRaises(ValidationError):
            s.change_listing_status(self.owner, disabled, "archived")

    def test_expired_seller_keeps_ticket_and_archive_access(self):
        ad = self.listing()
        Account.objects.filter(pk=self.owner.pk).update(subscription_until=timezone.now() - timedelta(days=1))
        self.assertEqual(s.change_listing_status(self.owner, ad, "archived").status, "archived")
        self.assertEqual(s.create_ticket(self.owner, "تمدید", "راهنمایی لطفاً").owner, self.owner)

    def test_subscription_price_duration_snapshot_and_replay(self):
        payment = s.create_payment(self.buyer, "subscription", plan=self.plan)
        previous_until = self.buyer.subscription_until
        self.plan.price, self.plan.duration_days = 9000, 90
        self.plan.save()
        s.settle_payment(payment)
        s.settle_payment(payment)
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.subscription_until, previous_until + timedelta(days=30))
        payment.refresh_from_db()
        self.assertEqual(payment.amount, 1000)

    def test_failure_is_terminal_but_delayed_can_succeed(self):
        payment = s.create_payment(self.buyer, "subscription", plan=self.plan)
        s.settle_payment(payment, "failure")
        self.assertEqual(s.settle_payment(payment, "success").status, "failure")
        self.buyer.refresh_from_db()
        original = self.buyer.subscription_until
        second = s.create_payment(self.buyer, "subscription", plan=self.plan)
        s.settle_payment(second, "delayed")
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.subscription_until, original)
        s.settle_payment(second, "success")
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.subscription_until, original + timedelta(days=30))

    def test_inspection_requires_payment_and_matching_expert(self):
        ad = self.listing()
        inspection = s.request_inspection(self.buyer, ad)
        expert = ExpertProfile.objects.create(user=self.make_user("expert", "expert"), province=self.province, credential="آزمایشی")
        with self.assertRaises(ValidationError):
            s.assign_expert(self.manager, inspection, expert)
        with self.assertRaises(PermissionDenied):
            s.create_payment(self.other, "inspection", inspection=inspection)
        first = s.create_payment(self.buyer, "inspection", inspection=inspection)
        second = s.create_payment(self.buyer, "inspection", inspection=inspection)
        self.assertEqual(s.settle_payment(first).status, "success")
        self.assertEqual(s.settle_payment(second).status, "failure")
        expert.province = self.other_province
        expert.save()
        with self.assertRaises(ValidationError):
            s.assign_expert(self.manager, inspection, expert)

    def test_final_appraisal_is_immutable_and_visible(self):
        listing, inspection, expert = self.inspection()
        with self.assertRaises(PermissionDenied):
            s.submit_report(self.owner, inspection, {"price": 8500000000, "summary": "تلاش نامعتبر"})
        report = s.submit_report(expert, inspection, {"price": 8500000000, "summary": "گزارش"})
        self.assertIsNone(listing.expert_price)
        report = s.finalize_report(self.manager, report)
        self.assertEqual(listing.expert_price, 8500000000)
        self.assertIsNotNone(listing.expert_date)
        for actor in [expert, self.manager]:
            with self.assertRaises(ValidationError):
                s.submit_report(actor, inspection, {"price": 1, "summary": "تغییر"})
        s.update_price(self.owner, listing, 7000000000)
        self.assertEqual(listing.expert_price, 8500000000)

    def test_expert_limit_is_three_per_province(self):
        for name in ["experta", "expertb", "expertc"]:
            ExpertProfile.objects.create(user=self.make_user(name, "expert"), province=self.province, credential="آزمایشی")
        with self.assertRaises(ValidationError):
            ExpertProfile.objects.create(user=self.make_user("expertd", "expert"), province=self.province, credential="آزمایشی")

    def test_favorites_comparison_limit_and_daily_view_deduplication(self):
        listings = [self.listing(title=str(i)) for i in range(4)]
        s.add_favorite(self.buyer, listings[0])
        s.add_favorite(self.buyer, listings[0])
        self.assertEqual(Favorite.objects.count(), 1)
        self.assertEqual(len(s.compare_listings(self.buyer, [ad.pk for ad in listings[:3]])), 3)
        with self.assertRaises(ValidationError):
            s.compare_listings(self.buyer, listings)
        s.record_view(self.buyer, listing=listings[0])
        s.record_view(self.buyer, listing=listings[0])
        s.record_view(self.owner, listing=listings[0])
        self.assertEqual(ViewEvent.objects.count(), 1)

    def test_support_permission_does_not_grant_other_access(self):
        support = self.make_user("support", "staff", is_staff=True)
        support.user_permissions.add(Permission.objects.get(codename="support_tickets", content_type__app_label="market"))
        ticket = s.create_ticket(self.owner, "کمک", "متن درخواست")
        with self.assertRaises(PermissionDenied):
            s.reply_ticket(self.other, ticket, "حمله")
        s.reply_ticket(support, ticket, "پاسخ پشتیبانی")
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "answered")
        with self.assertRaises(PermissionDenied):
            s.approve_identity(support, self.buyer)
        with self.assertRaises(PermissionDenied):
            s.approve_listing(support, self.listing())

    def test_identity_requires_complete_information_and_verification(self):
        Account.objects.filter(pk=self.buyer.pk).update(identity_document="")
        with self.assertRaises(ValidationError):
            s.approve_identity(self.manager, self.buyer)
        Account.objects.filter(pk=self.buyer.pk).update(identity_document="identity/test.png", email_verified=False)
        with self.assertRaises(ValidationError):
            s.approve_identity(self.manager, self.buyer)
        Account.objects.filter(pk=self.buyer.pk).update(email_verified=True)
        self.assertEqual(s.approve_identity(self.manager, self.buyer).identity_status, "approved")

    def test_slug_normalization_and_history_reservation(self):
        self.assertEqual(s.normalize_slug("pro", "AbCd"), "abcd")
        self.assertEqual(s.normalize_slug("plus", "AbCd"), "abcd_personal")
        for value in ["", "123", "abcdefghij", "نام"]:
            with self.assertRaises(ValidationError):
                s.normalize_slug("pro", value)
        page = self.owner_page
        page.pending_data = {"slug": "newname_personal"}
        page.save()
        s.moderate_page(self.manager, page)
        self.assertTrue(SlugHistory.objects.filter(slug="seller_personal", page=page).exists())
        with self.assertRaises(ValidationError):
            s._slug_available("seller_personal", self.other.page)

    def test_username_email_mobile_uniqueness_is_database_enforced(self):
        for values in [{"username": "SELLER", "email": "unique@example.test"},
                       {"username": "unique", "email": self.owner.email.upper()},
                       {"username": "unique", "email": "unique@example.test", "mobile": self.owner.mobile}]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                Account.objects.create(**values)

    def test_market_position_nationwide_samples_exclude_self_and_bad_matches(self):
        ad = self.listing(price=500)
        for index, price in enumerate([100, 200, 300, 400, 500]):
            self.listing(title=str(index), price=price, province=self.other_province)
        self.listing(price=99999, brand="Scania")
        self.listing(price=99999, mileage=130000)
        self.listing(price=99999, moderation_status="pending")
        result = s.market_position(ad)
        self.assertEqual(result["count"], 5)
        self.assertEqual((result["q25"], result["q75"]), (200, 400))
        self.assertEqual(result["status"], "above")
        self.assertEqual(s.market_position(self.listing(model="Other"))["status"], "insufficient")

    def test_batched_market_positions_match_single_listing_results(self):
        for index, price in enumerate([100, 200, 300, 400, 500, 600]):
            self.listing(title=str(index), price=price, province=self.other_province)
        for index, price in enumerate([10, 20, 30, 40, 50]):
            self.listing(title="s" + str(index), price=price, brand="SCANIA", model="r450", year=1399)
        ads = [self.listing(price=50), self.listing(price=35, brand="scania", model="R450", year=1400),
               self.listing(price=1, year=1395), self.listing(price=5, mileage=500000),
               self.listing(price=7, category=self.other_category)]
        def reference(ad):
            low, high = s._mileage_bounds(ad.mileage)
            return s._position(ad, list(s.visible_listings().filter(
                category=ad.category, brand__iexact=ad.brand, model__iexact=ad.model,
                year__gte=ad.year - 1, year__lte=ad.year + 1, mileage__gte=low, mileage__lte=high,
            ).exclude(pk=ad.pk).order_by("price").values_list("price", flat=True)))
        expected = {ad.pk: reference(ad) for ad in ads}
        with self.assertNumQueries(1):
            batched = s.market_positions(ads)
        self.assertEqual(batched, expected)
        self.assertEqual(batched[ads[0].pk]["status"], "below")
        self.assertEqual(batched[ads[1].pk]["status"], "within")
        self.assertEqual({batched[ad.pk]["status"] for ad in ads[2:]}, {"insufficient"})


@skipUnless(connection.vendor == "postgresql", "Row-lock contention tests require PostgreSQL")
class PostgreSQLContentionTests(DomainFixtures, TransactionTestCase):
    reset_sequences = True

    def race(self, operation):
        barrier = Barrier(2)
        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                operation()
                return "ok"
            except ValidationError:
                return "quota"
            finally:
                # Persistent connections (CONN_MAX_AGE) would otherwise block test DB teardown.
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(lambda _: run(), range(2)))

    def test_simultaneous_last_slot_submissions_cannot_overbook(self):
        for i in range(24):
            self.listing(title=str(i))
        outcomes = self.race(lambda: s.submit_listing(Account.objects.get(pk=self.owner.pk), self.data()))
        self.assertCountEqual(outcomes, ["ok", "quota"])
        self.assertEqual(Listing.objects.filter(owner=self.owner, status="active").count(), 25)

    def test_simultaneous_payment_callbacks_extend_once(self):
        payment = s.create_payment(self.buyer, "subscription", plan=self.plan)
        original = self.buyer.subscription_until
        outcomes = self.race(lambda: s.settle_payment(Payment.objects.get(pk=payment.pk)))
        self.assertCountEqual(outcomes, ["ok", "ok"])
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.subscription_until, original + timedelta(days=30))

    def test_simultaneous_activation_checks_last_slot(self):
        for i in range(24):
            self.listing(title=str(i))
        paused = [self.listing(title="paused" + str(i), status="paused") for i in range(2)]
        barrier = Barrier(2)
        def run(pk):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                s.change_listing_status(Account.objects.get(pk=self.owner.pk), Listing.objects.get(pk=pk), "active")
                return "ok"
            except ValidationError:
                return "quota"
            finally:
                # Persistent connections (CONN_MAX_AGE) would otherwise block test DB teardown.
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(run, [ad.pk for ad in paused])), ["ok", "quota"])
