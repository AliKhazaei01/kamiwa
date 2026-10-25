"""Row-level security in PostgreSQL: the database itself hides other people's rows.

These tests query the ORM with no owner filter, the way a buggy view would, and
check that PostgreSQL still returns only the rows the person may see.
"""
from datetime import date, timedelta
from unittest import skipUnless

from django.contrib.auth.models import Permission
from django.db import connection
from django.db.utils import ProgrammingError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from market import rls
from market.models import (OTP, Account, ActivityDay, AuditEvent, Category, ExpertProfile, Favorite, InspectionReport,
                           InspectionRequest, Listing, OutboxMessage, Payment, Plan, Province, SellerPage, Ticket,
                           TicketMessage, ViewEvent, WalletTransaction, WithdrawalRequest)

STATIC = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
          "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}


@skipUnless(connection.vendor == "postgresql", "row-level security exists only on PostgreSQL")
@override_settings(STORAGES=STATIC)
class RowLevelSecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        province = Province.objects.create(name="تهران")
        category = Category.objects.create(name="کامیون", slug="truck")
        common = dict(province=province, city="تهران", identity_status="approved", email_verified=True, mobile_verified=True,
                      subscription_until=timezone.now() + timedelta(days=10), first_name="کاربر", last_name="آزمایشی",
                      national_id="0123456789", birth_date=date(1990, 1, 1))

        def account(name, role, mobile, **extra):
            return Account.objects.create_user(name, email=f"{name}@example.test", password="ValidComplexPass27", role=role,
                                               mobile=mobile, **{**common, **extra})

        cls.alice = account("alice", "buyer", "09120000011")
        cls.bob = account("bob", "buyer", "09120000012")
        cls.seller = account("seller", "pro", "09120000013")
        cls.expert_user = account("expert", "expert", "09120000014")
        cls.boss = account("boss", "staff", "09120000015", is_staff=True, is_superuser=True)
        cls.support = account("support", "staff", "09120000016", is_staff=True)
        cls.support.user_permissions.add(Permission.objects.get(codename="support_tickets"))
        SellerPage.objects.create(user=cls.seller, slug="dealer", display_name="نمایشگاه", moderation_status="approved")
        cls.listing = Listing.objects.create(owner=cls.seller, title="آگهی", category=category, province=province, brand="Volvo",
                                             model="FH", year=1400, mileage=1, price=1, status="active", moderation_status="approved")
        plan = Plan.objects.create(role="buyer", name="خریدار", price=100000, duration_days=30)
        cls.expert = ExpertProfile.objects.create(user=cls.expert_user, province=province, credential="مدرک", iban="IR" + "0" * 24)
        for person in (cls.alice, cls.bob):
            Payment.objects.create(user=person, kind="subscription", plan=plan, amount=100000, duration_days=30,
                                   reference=f"ref-{person.username}")
            ticket = Ticket.objects.create(owner=person, subject=f"تیکت {person.username}")
            TicketMessage.objects.create(ticket=ticket, author=person, body="متن خصوصی")
            Favorite.objects.create(user=person, listing=cls.listing)
            ViewEvent.objects.create(user=person, listing=cls.listing)
            ActivityDay.objects.create(user=person, day=timezone.localdate(), minutes=2)
            OTP.objects.create(user=person, target=person.email, channel="email", code_hash="x",
                               expires_at=timezone.now() + timedelta(minutes=5))
            OutboxMessage.objects.create(channel="email", target=person.email, payload_ciphertext="x")
            AuditEvent.objects.create(actor=person, action="test", object_type="account", object_id=str(person.pk))
        cls.alice_inspection = InspectionRequest.objects.create(listing=cls.listing, requester=cls.alice, expert=cls.expert,
                                                                coordination_note="یادداشت خصوصی")
        cls.report = InspectionReport.objects.create(inspection=cls.alice_inspection, expert=cls.expert,
                                                     submitted_by=cls.expert_user, price=5, summary="خلاصه")
        WalletTransaction.objects.create(expert=cls.expert, kind="credit", amount=5, balance_after=5, actor=cls.boss)
        WithdrawalRequest.objects.create(expert=cls.expert, amount=1, iban="IR" + "0" * 24)

    def tearDown(self):
        rls.set_context(rls.SYSTEM)

    def as_user(self, user):
        rls.set_context(rls.SYSTEM)
        rls.apply_user(Account.objects.get(pk=user.pk))

    def test_policies_are_enabled_and_forced_on_private_tables(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT relname FROM pg_class WHERE relname LIKE 'market_%%' AND relrowsecurity AND relforcerowsecurity")
            protected = {row[0] for row in cursor.fetchall()}
        for table in ("market_account", "market_payment", "market_ticket", "market_ticketmessage", "market_otp",
                      "market_favorite", "market_viewevent", "market_activityday", "market_outboxmessage",
                      "market_auditevent", "market_expertprofile", "market_wallettransaction", "market_withdrawalrequest",
                      "market_inspectionrequest", "market_inspectionreport", "market_credentialrotation"):
            self.assertIn(table, protected)

    def test_application_role_does_not_bypass_policies(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            self.assertFalse(cursor.fetchone()[0], "tests must run as a role that RLS applies to")

    def test_buyer_sees_only_own_private_rows(self):
        self.as_user(self.alice)
        for model, field in ((Payment, "user"), (Ticket, "owner"), (Favorite, "user"), (OTP, "user"),
                             (ActivityDay, "user"), (AuditEvent, "actor")):
            self.assertEqual(set(model.objects.values_list(f"{field}_id", flat=True)), {self.alice.pk}, model.__name__)
        self.assertEqual(set(TicketMessage.objects.values_list("ticket__owner_id", flat=True)), {self.alice.pk})
        self.assertEqual(list(OutboxMessage.objects.values_list("target", flat=True)), [self.alice.email])
        self.assertEqual(set(ViewEvent.objects.values_list("user_id", flat=True)), {self.alice.pk})

    def test_buyer_cannot_see_another_buyer_account(self):
        self.as_user(self.alice)
        visible = set(Account.objects.values_list("username", flat=True))
        self.assertIn("alice", visible)
        self.assertNotIn("bob", visible)
        # Sellers, experts and staff are shown by name on listings and replies.
        self.assertTrue({"seller", "expert", "support"} <= visible)
        self.assertFalse(Account.objects.filter(pk=self.bob.pk).exists())

    def test_buyer_cannot_change_or_delete_another_buyers_rows(self):
        self.as_user(self.alice)
        self.assertEqual(Account.objects.filter(pk=self.bob.pk).update(first_name="هک"), 0)
        self.assertEqual(Payment.objects.filter(user=self.bob).update(status="success"), 0)
        self.assertEqual(Ticket.objects.filter(owner=self.bob).delete()[0], 0)
        rls.set_context(rls.SYSTEM)
        self.assertEqual(Account.objects.get(pk=self.bob.pk).first_name, "کاربر")
        self.assertEqual(Payment.objects.get(user=self.bob).status, "pending")
        self.assertTrue(Ticket.objects.filter(owner=self.bob).exists())

    def test_buyer_cannot_insert_rows_for_someone_else(self):
        self.as_user(self.alice)
        with self.assertRaises(ProgrammingError):
            from django.db import transaction
            with transaction.atomic():
                Ticket.objects.create(owner=self.bob, subject="جعلی")

    def test_seller_sees_inspection_buyer_and_views_of_own_listing_only(self):
        self.as_user(self.seller)
        self.assertEqual(list(InspectionRequest.objects.values_list("pk", flat=True)), [self.alice_inspection.pk])
        accounts = set(Account.objects.values_list("username", flat=True))
        self.assertIn("alice", accounts)  # requested an inspection of the seller's listing
        self.assertNotIn("bob", accounts)
        self.assertEqual(ViewEvent.objects.count(), 2)
        self.assertEqual(Favorite.objects.count(), 2)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Ticket.objects.exists())

    def test_expert_sees_own_wallet_and_assigned_work(self):
        self.as_user(self.expert_user)
        self.assertEqual(WalletTransaction.objects.count(), 1)
        self.assertEqual(WithdrawalRequest.objects.count(), 1)
        self.assertEqual(InspectionReport.objects.count(), 1)
        self.as_user(self.bob)
        self.assertFalse(WalletTransaction.objects.exists())
        self.assertFalse(WithdrawalRequest.objects.exists())
        self.assertFalse(ExpertProfile.objects.exists())  # holds the expert's bank account number
        self.assertFalse(InspectionRequest.objects.exists())
        self.assertFalse(InspectionReport.objects.exists())  # not final yet

    def test_final_report_is_visible_but_its_private_request_is_not(self):
        rls.set_context(rls.SYSTEM)
        InspectionReport.objects.filter(pk=self.report.pk).update(status="final", finalized_at=timezone.now())
        self.as_user(self.bob)
        self.assertEqual(InspectionReport.objects.count(), 1)
        self.assertFalse(InspectionRequest.objects.exists())
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).appraisal_price, 5)

    def test_section_admin_sees_only_their_section(self):
        self.as_user(self.support)
        self.assertEqual(Ticket.objects.count(), 2)
        self.assertEqual(TicketMessage.objects.count(), 2)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(OTP.objects.exists())
        self.assertFalse(WalletTransaction.objects.exists())

    def test_main_admin_sees_every_row(self):
        self.as_user(self.boss)
        rls_counts = {model: model.objects.count() for model in (Account, Payment, Ticket, TicketMessage, OTP, OutboxMessage,
                                                                 InspectionRequest, WalletTransaction, ExpertProfile)}
        rls.set_context(rls.SYSTEM)
        for model, count in rls_counts.items():
            self.assertEqual(count, model.objects.count(), model.__name__)

    def test_anonymous_and_unset_contexts_see_nothing(self):
        rls.set_context(rls.ANONYMOUS)
        self.assertFalse(Account.objects.exists())
        self.assertFalse(Payment.objects.exists())
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('kamiwa.mode', '', false)")
            cursor.execute("SELECT count(*) FROM market_payment")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_middleware_filters_an_unfiltered_query_in_a_request(self):
        """A view that forgets the owner filter still gets only the signed-in user's rows."""
        from django.http import HttpResponse
        from django.test import RequestFactory

        def careless_view(request):
            return HttpResponse(",".join(Ticket.objects.order_by("pk").values_list("subject", flat=True)))

        request = RequestFactory().get("/")
        request.user = self.alice
        response = rls.RowSecurityMiddleware(careless_view)(request)
        self.assertEqual(response.content.decode(), "تیکت alice")
        self.assertEqual(rls.current_context(), rls.SYSTEM)  # reset after the request
        self.assertEqual(Ticket.objects.count(), 2)

    def test_signed_in_ticket_page_lists_only_own_tickets(self):
        self.client.force_login(self.alice)
        session = self.client.session
        session["mfa_verified"] = True
        session.save()
        response = self.client.get(reverse("market:tickets"))
        self.assertContains(response, "تیکت alice")
        self.assertNotContains(response, "تیکت bob")
