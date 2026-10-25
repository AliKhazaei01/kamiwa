"""Inspection administration, listing photos/videos and the expert wallet."""
import struct
from datetime import date, timedelta
from io import BytesIO
from tempfile import TemporaryDirectory

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from market import services
from market.files import inspect_video, validate_video
from market.models import (Account, AuditEvent, Category, ExpertProfile, InspectionReport, InspectionRequest,
    Listing, ListingImage, ListingVideo, Province, SellerPage, WalletTransaction, WithdrawalRequest)

STATIC = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
          "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
VALID_IBAN = "IR820540102680020817909002"


def box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def make_video(seconds=30, width=1920, height=1080, moov_last=False, name="clip.mp4"):
    """Smallest ISO-BMFF file with mvhd duration and a tkhd frame size."""
    timescale = 1000
    mvhd = box(b"mvhd", b"\x00\x00\x00\x00" + b"\x00" * 8 + struct.pack(">II", timescale, int(seconds * timescale)) + b"\x00" * 80)
    tkhd = box(b"tkhd", b"\x00\x00\x00\x00" + b"\x00" * 76 + struct.pack(">II", width << 16, height << 16))
    moov = box(b"moov", mvhd + box(b"trak", tkhd))
    ftyp = box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    mdat = box(b"mdat", b"\x00" * 64)
    data = ftyp + (mdat + moov if moov_last else moov + mdat)
    return SimpleUploadedFile(name, data, content_type="video/mp4")


def image(name="photo.png"):
    buffer = BytesIO()
    Image.new("RGB", (64, 48), "navy").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@override_settings(STORAGES=STATIC)
class Phase3Base(TestCase):
    def setUp(self):
        cache.clear()
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(PRIVATE_MEDIA_ROOT=media.name, MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.province = Province.objects.create(name="تهران")
        self.other_province = Province.objects.create(name="فارس")
        self.category = Category.objects.create(name="کامیون", slug="truck")
        self.buyer = self.make("buyerone", "buyer", "09120000001")
        self.pro = self.make("prodealer", "pro", "09120000002")
        self.plus = self.make("plusseller", "plus", "09120000003")
        SellerPage.objects.create(user=self.pro, slug="dealer", display_name="نمایشگاه", moderation_status="approved")
        SellerPage.objects.create(user=self.plus, slug="pl_personal", display_name="فروشنده", moderation_status="approved")
        self.listing = self.ad(self.pro)
        self.boss = self.make("bossman", "staff", "09120000004", is_staff=True, is_superuser=True)
        self.expert_user = self.make("expertone", "expert", "09120000005")
        self.expert = ExpertProfile.objects.create(user=self.expert_user, province=self.province, credential="آزمایشی")

    def make(self, username, role, mobile, **extra):
        return Account.objects.create_user(username, email=username + "@example.test", password="ValidComplexPass27", role=role,
            first_name="کاربر", last_name="آزمایشی", province=self.province, city="تهران", mobile=mobile, national_id="0123456789",
            birth_date=date(1990, 1, 1), identity_status="approved", email_verified=True, mobile_verified=True,
            subscription_until=timezone.now() + timedelta(days=10), **extra)

    def ad(self, owner, province=None, **extra):
        return Listing.objects.create(owner=owner, title="آگهی " + owner.username, category=self.category, province=province or self.province,
                                      brand="Volvo", model="FH", year=1400, mileage=1, price=1000, status="active", moderation_status="approved", **extra)

    def staff_login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["mfa_verified"] = True
        session.save()

    def inspection(self, status="assigned", listing=None, price=None):
        request = InspectionRequest.objects.create(listing=listing or self.listing, requester=self.buyer,
                                                   expert=self.expert if status != "paid" else None, status=status)
        if price:
            InspectionReport.objects.create(inspection=request, expert=self.expert, submitted_by=self.expert_user, price=price,
                                            summary="گزارش نمونه", status="final" if status == "final" else "pending",
                                            finalized_at=timezone.now() if status == "final" else None)
        return request


class InspectionAdminTests(Phase3Base):
    def test_list_filters_by_province_and_status(self):
        here = self.inspection()
        there = self.inspection(listing=self.ad(self.pro, province=self.other_province))
        self.staff_login(self.boss)
        response = self.client.get(reverse("market:staff_inspections"), {"province": self.other_province.pk})
        self.assertEqual(list(response.context["inspections"]), [there])
        response = self.client.get(reverse("market:staff_inspections"), {"status": "assigned", "province": self.province.pk})
        self.assertEqual(list(response.context["inspections"]), [here])
        counts = {row["province"].pk: row["open"] for row in response.context["provinces"]}
        self.assertEqual(counts, {self.province.pk: 1, self.other_province.pk: 1})

    def test_cancel_requires_reason_and_not_final(self):
        request = self.inspection()
        with self.assertRaises(ValidationError):
            services.cancel_inspection(self.boss, request, "")
        services.cancel_inspection(self.boss, request, "درخواست تکراری")
        self.assertEqual(InspectionRequest.objects.get(pk=request.pk).status, "cancelled")
        final = self.inspection("final", price=5000)
        with self.assertRaises(ValidationError):
            services.cancel_inspection(self.boss, final, "دلیل")

    def test_reinspection_removes_report_and_reopens_assignment(self):
        final = self.inspection("final", price=5000)
        self.staff_login(self.boss)
        self.client.post(reverse("market:staff_inspections"), {"action": "reinspect", "pk": final.pk, "reason": "قیمت غیرواقعی"})
        final.refresh_from_db()
        self.assertEqual((final.status, final.expert_id), ("paid", None))
        self.assertFalse(InspectionReport.objects.filter(inspection=final).exists())
        event = AuditEvent.objects.get(action="inspection.reinspect")
        self.assertEqual(event.before["report"]["price"], "5000")
        services.assign_expert(self.boss, final, self.expert)
        self.assertEqual(InspectionRequest.objects.get(pk=final.pk).status, "assigned")

    def test_staff_edit_and_delete_report(self):
        final = self.inspection("final", price=5000)
        report = final.report
        self.staff_login(self.boss)
        self.client.post(reverse("market:staff_report_edit", args=[report.pk]), {"price": "7000", "summary": "گزارش اصلاح‌شده"})
        report.refresh_from_db()
        self.assertEqual((report.price, report.summary, report.status), (7000, "گزارش اصلاح‌شده", "final"))
        self.client.post(reverse("market:staff_inspections"), {"action": "delete_report", "pk": report.pk, "reason": "اشتباه"})
        self.assertFalse(InspectionReport.objects.filter(pk=report.pk).exists())
        self.assertEqual(InspectionRequest.objects.get(pk=final.pk).status, "assigned")

    def test_only_report_admins_can_manage(self):
        request = self.inspection()
        support = self.make("supporter", "staff", "09120000006", is_staff=True)
        support.user_permissions.add(Permission.objects.get(codename="support_tickets"))
        self.staff_login(support)
        self.assertEqual(self.client.get(reverse("market:staff_inspections")).status_code, 403)
        with self.assertRaises(PermissionDenied):
            services.reinspect(support, request, "x")
        with self.assertRaises(PermissionDenied):
            services.cancel_inspection(self.pro, request, "x")


class VideoInspectionTests(TestCase):
    def test_reads_duration_and_size_with_moov_anywhere(self):
        for moov_last in (False, True):
            info = inspect_video(make_video(42.5, 1280, 720, moov_last=moov_last))
            self.assertEqual((round(info["seconds"], 1), info["width"], info["height"]), (42.5, 1280, 720))

    def test_limits(self):
        self.assertTrue(validate_video(make_video(60, 1080, 1920, name="portrait.mov")))
        for upload, message in [(make_video(75), "یک دقیقه"), (make_video(20, 3840, 2160), "۱۰۸۰p"),
                                (make_video(name="clip.avi"), "MP4")]:
            with self.assertRaisesMessage(ValidationError, message):
                validate_video(upload)
        with self.assertRaises(ValidationError):
            validate_video(SimpleUploadedFile("x.mp4", b"not a video at all"))


class ListingMediaTests(Phase3Base):
    def form_data(self, **extra):
        return {"title": "کشنده نمونه", "category": self.category.pk, "brand": "Volvo", "model": "FH", "year": "1400",
                "mileage": "1000", "province": self.province.pk, "city": "تهران", "price": "1000", "negotiability": "negotiable",
                "description": "توضیح", **extra}

    def test_pro_can_upload_five_photos_and_two_videos(self):
        self.client.force_login(self.pro)
        response = self.client.post(reverse("market:listing_create"), self.form_data(
            images=[image(f"p{i}.png") for i in range(5)], videos=[make_video(name="a.mp4"), make_video(name="b.mov")]))
        self.assertEqual(response.status_code, 302)
        listing = Listing.objects.filter(owner=self.pro).latest("pk")
        self.assertEqual(listing.images.count(), 5)
        self.assertEqual(listing.videos.count(), 2)
        self.assertFalse(listing.videos.filter(approved=True).exists())
        self.assertEqual({v.content_type for v in listing.videos.all()}, {"video/mp4", "video/quicktime"})
        services.approve_listing(self.boss, listing)
        self.assertEqual(listing.videos.filter(approved=True).count(), 2)

    def test_limits_and_plus_cannot_add_videos(self):
        self.client.force_login(self.pro)
        response = self.client.post(reverse("market:listing_create"), self.form_data(images=[image(f"p{i}.png") for i in range(6)]))
        self.assertContains(response, "حداکثر ۵ تصویر")
        response = self.client.post(reverse("market:listing_create"), self.form_data(videos=[make_video(name=f"{i}.mp4") for i in range(3)]))
        self.assertContains(response, "حداکثر ۲ ویدیو")
        self.client.force_login(self.plus)
        self.assertNotContains(self.client.get(reverse("market:listing_create")), 'name="videos"')
        with self.assertRaises(PermissionDenied):
            services.submit_listing(self.plus, {"title": "x", "category": self.category, "brand": "a", "model": "b", "year": 1400,
                                                "mileage": 1, "province": self.province, "price": 1, "videos": [make_video()]})

    def test_edit_removes_media_and_respects_total(self):
        for i in range(4):
            ListingImage.objects.create(listing=self.listing, image=f"listings/x{i}.jpg", approved=True)
        self.client.force_login(self.pro)
        url = reverse("market:listing_edit", args=[self.listing.pk])
        response = self.client.post(url, self.form_data(images=[image("a.png"), image("b.png")]))
        self.assertContains(response, "حداکثر ۵ تصویر")
        first = self.listing.images.first()
        self.client.post(url, self.form_data(images=[image("a.png"), image("b.png")], remove_image=[first.pk]))
        self.assertEqual(self.listing.images.count(), 5)
        self.assertFalse(ListingImage.objects.filter(pk=first.pk).exists())

    def test_rejected_listing_drops_unapproved_videos(self):
        clip = ListingVideo.objects.create(listing=self.listing, video="listings/videos/a.mp4", approved=False)
        services.approve_listing(self.boss, self.listing, approve=False, reason="ویدیو نامناسب")
        self.assertFalse(ListingVideo.objects.filter(pk=clip.pk).exists())


class WalletTests(Phase3Base):
    def setUp(self):
        super().setUp()
        self.billing = self.make("billingman", "staff", "09120000007", is_staff=True)
        self.billing.user_permissions.add(Permission.objects.get(codename="manage_billing"))

    def test_iban_validation(self):
        self.assertEqual(services.normalize_iban("ir82 0540 1026 8002 0817 9090 02"), VALID_IBAN)
        for bad in ("IR820540102680020817909003", "IR12", "DE89370400440532013000"):
            with self.assertRaises(ValidationError):
                services.normalize_iban(bad)

    def test_credit_withdraw_and_pay(self):
        final = self.inspection("final", price=5000)
        services.credit_wallet(self.billing, self.expert, 300000, inspection=final, note="کارشناسی")
        with self.assertRaises(ValidationError):
            services.credit_wallet(self.billing, self.expert, 300000, inspection=final)
        self.client.force_login(self.expert_user)
        self.client.post(reverse("market:wallet"), {"amount": "۱۰۰۰۰۰", "iban": VALID_IBAN})
        self.expert.refresh_from_db()
        self.assertEqual(self.expert.wallet_balance, 200000)
        withdrawal = WithdrawalRequest.objects.get()
        self.assertEqual((withdrawal.status, withdrawal.amount, withdrawal.iban), ("pending", 100000, VALID_IBAN))
        page = self.client.get(reverse("market:wallet"))
        self.assertContains(page, "در حال انجام")
        self.assertContains(page, "۲۰۰٬۰۰۰")
        services.decide_withdrawal(self.billing, withdrawal, paid=True, reference="TRX-1")
        self.assertEqual(WithdrawalRequest.objects.get().status, "paid")
        self.assertContains(self.client.get(reverse("market:wallet")), "واریز شد")
        with self.assertRaises(ValidationError):
            services.decide_withdrawal(self.billing, withdrawal, paid=False, reference="دوباره")
        self.assertEqual([t.kind for t in WalletTransaction.objects.order_by("pk")], ["credit", "withdrawal"])

    def test_overdraw_is_refused_and_rejection_refunds(self):
        services.credit_wallet(self.billing, self.expert, 50000)
        with self.assertRaises(ValidationError):
            services.request_withdrawal(self.expert_user, 60000, VALID_IBAN)
        withdrawal = services.request_withdrawal(self.expert_user, 50000, VALID_IBAN)
        self.assertEqual(ExpertProfile.objects.get(pk=self.expert.pk).wallet_balance, 0)
        with self.assertRaises(ValidationError):
            services.decide_withdrawal(self.billing, withdrawal, paid=False, reference="")
        services.decide_withdrawal(self.billing, withdrawal, paid=False, reference="شبا به نام کارشناس نیست")
        self.assertEqual(ExpertProfile.objects.get(pk=self.expert.pk).wallet_balance, 50000)

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            services.credit_wallet(self.expert_user, self.expert, 1000)
        with self.assertRaises(PermissionDenied):
            services.request_withdrawal(self.buyer, 1000, VALID_IBAN)
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get(reverse("market:wallet")).status_code, 403)
        self.staff_login(self.billing)
        self.assertEqual(self.client.get(reverse("market:staff_wallets")).status_code, 200)
        self.client.post(reverse("market:staff_wallets"), {"action": "credit", "expert": self.expert.pk, "amount": "۲۵۰۰۰"})
        self.assertEqual(ExpertProfile.objects.get(pk=self.expert.pk).wallet_balance, 25000)
        self.client.force_login(self.expert_user)
        self.assertContains(self.client.get(reverse("market:inspections")), "کیف پول")
