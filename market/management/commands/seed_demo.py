"""Create explicitly fictional sample data; credentials never enter source or stdout."""
import io
import json
import os
import secrets
import string
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw
from django.conf import settings
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from market.models import (Account, AuditEvent, Category, ExpertProfile, InspectionReport, InspectionRequest,
                           Listing, Plan, PriceHistory, Province, SellerPage, Ticket, TicketMessage)


PROVINCES = ["آذربایجان شرقی", "آذربایجان غربی", "اردبیل", "اصفهان", "البرز", "ایلام", "بوشهر", "تهران", "چهارمحال و بختیاری", "خراسان جنوبی", "خراسان رضوی", "خراسان شمالی", "خوزستان", "زنجان", "سمنان", "سیستان و بلوچستان", "فارس", "قزوین", "قم", "کردستان", "کرمان", "کرمانشاه", "کهگیلویه و بویراحمد", "گلستان", "گیلان", "لرستان", "مازندران", "مرکزی", "هرمزگان", "همدان", "یزد"]


def _password():
    return "Aa7" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(21))


def _letters(number):
    return string.ascii_lowercase[number // 26] + string.ascii_lowercase[number % 26]


def _test_image():
    image = Image.new("RGB", (160, 160), "#e3eaf1")
    draw = ImageDraw.Draw(image)
    draw.ellipse((55, 25, 105, 75), fill="#102c40")
    draw.rounded_rectangle((30, 80, 130, 160), radius=32, fill="#102c40")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@transaction.atomic
def _bootstrap_manager(province, factory):
    """Remember the original account independently of rotating credentials.

    The province row serializes concurrent seed commands. The audit marker is
    immutable in the application UI and contains only the account's stable pk.
    """
    Province.objects.select_for_update().get(pk=province.pk)
    marker = AuditEvent.objects.filter(action="demo.bootstrap.admin", object_type="account").first()
    if marker:
        manager = Account.objects.filter(pk=marker.object_id).first()
        if not manager or not manager.is_superuser or not manager.is_staff or not manager.is_active:
            raise CommandError("The original demo administrator is unavailable. Seeding will not recreate or elevate it.")
        return manager
    existing = list(Account.objects.filter(is_superuser=True).order_by("date_joined", "pk")[:2])
    if len(existing) > 1:
        raise CommandError("Several administrators exist; the demo seed cannot choose a bootstrap owner.")
    manager = existing[0] if existing else factory()
    if not manager.is_superuser or not manager.is_staff or not manager.is_active:
        raise CommandError("The demo manager identity is unavailable; no account privileges were changed.")
    AuditEvent.objects.create(actor=manager, action="demo.bootstrap.admin", object_type="account",
                              object_id=str(manager.pk), after={"is_test": True})
    return manager


class Command(BaseCommand):
    help = "Seed fictional Kamiwa test users and listings. Secret credentials are written to an ignored private file."

    def handle(self, *args, **options):
        if not settings.KAMIWA_TEST_PROVIDERS:
            raise CommandError("Demo seed is allowed only with test providers enabled.")
        path = Path(settings.BASE_DIR) / "artifacts" / "private" / "demo-accounts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"is_test": True, "accounts": []}
        accounts = {row["username"]: row for row in previous.get("accounts", [])}
        avatar_bytes = _test_image()
        now = timezone.now()

        def account(username, role, **extra):
            existing = Account.objects.filter(username=username).first()
            if existing:
                existing._seed_created = False
                return existing
            password = _password()
            email = extra.pop("email", username + "@example.test")
            subscription_until = extra.pop("subscription_until", now + timedelta(days=30))
            # The savepoint permits a random internal-code collision to be retried.
            for attempt in range(100):
                try:
                    with transaction.atomic():
                        user = Account(username=username, role=role, first_name="کاربر", last_name="آزمایشی",
                                       email=email, province=provinces["تهران"], city="تهران",
                                       national_id="0000000000", birth_date=date(1990, 1, 1),
                                       email_verified=True, mobile_verified=True, identity_status="approved",
                                       subscription_until=subscription_until, **extra)
                        user.set_password(password)
                        user.save()
                    break
                except IntegrityError:
                    if attempt == 99:
                        raise
            accounts[username] = {"username": username, "password": password, "role": role,
                                  "label": user.get_role_display(), "is_test": True}
            # Save each new credential immediately so interruption cannot lose it.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({"is_test": True, "accounts": list(accounts.values())}, output, ensure_ascii=False, indent=2)
            if os.name != "nt":
                path.chmod(0o600)
            user._seed_created = True
            return user

        provinces = {name: Province.objects.get_or_create(name=name)[0] for name in PROVINCES}
        categories = {slug: Category.objects.get_or_create(slug=slug, defaults={"name": name})[0]
                      for slug, name in [("truck", "کامیون"), ("tractor", "کشنده"), ("trailer", "تریلر"), ("carrier", "بارکش")]}
        for slug, name, parent in [("flatbed", "کفی", "trailer"), ("refrigerated", "یخچال‌دار", "trailer"), ("tipper", "کمپرسی", "truck")]:
            Category.objects.get_or_create(slug=slug, defaults={"name": name, "parent": categories[parent]})
        for role, name, price in [("buyer", "اشتراک خریدار", 150000), ("plus", "اشتراک پلاس", 450000), ("pro", "اشتراک پرو", 950000)]:
            Plan.objects.get_or_create(role=role, defaults={"name": name, "price": price, "duration_days": 30, "inspection_fee": 2500000})
        manager = _bootstrap_manager(provinces["تهران"], lambda: account(
            "manager", "staff", is_staff=True, is_superuser=True,
            email=settings.KAMIWA_ADMIN_EMAIL, subscription_until=None))
        support = account("support", "staff", is_staff=True)
        if support._seed_created:
            support.user_permissions.add(Permission.objects.get(codename="support_tickets", content_type__app_label="market"))
        reviewer = account("reviewer", "staff", is_staff=True)
        if reviewer._seed_created:
            reviewer.user_permissions.add(*Permission.objects.filter(codename__in=["moderate_listing", "review_identity"], content_type__app_label="market"))
        buyer = account("buyer", "buyer")
        plus = account("personal", "plus")
        pro = account("showroom", "pro")
        expired = account("expired", "buyer")
        if expired.subscription_until and expired.subscription_until > now:
            expired.subscription_until = now - timedelta(days=1)
            expired.save(update_fields=["subscription_until"])
        pending = account("pending", "buyer")
        if pending.identity_status == "approved":
            pending.identity_status = "pending"
            pending.email_verified = pending.mobile_verified = False
            pending.save(update_fields=["identity_status", "email_verified", "mobile_verified"])
        for seller, slug, display in [(pro, "kamiwa", "نمایشگاه کامیوا · داده آزمایشی"), (plus, "ahmadi_personal", plus.get_full_name())]:
            page, created = SellerPage.objects.get_or_create(user=seller, defaults={"slug": slug, "display_name": display,
                "bio": "این صفحه و تمام آگهی‌های آن داده نمونه برای آزمون کامیوا هستند.", "address": "تهران، نشانی نمونه آزمایشی",
                "activity": "خرید و فروش کشنده و کامیون", "moderation_status": "approved", "contact_email": seller.email})
            if created:
                page.avatar.save("demo-portrait.png", ContentFile(avatar_bytes), save=False)
                page.license_document.save("demo-license.png", ContentFile(avatar_bytes), save=False)
                page.save()
            if not seller.identity_document:
                seller.identity_document.save("demo-identity.png", ContentFile(avatar_bytes))
            if not seller.birth_certificate:
                seller.birth_certificate.save("demo-birth-certificate.png", ContentFile(avatar_bytes))
        if not pending.identity_document:
            pending.identity_document.save("demo-identity.png", ContentFile(avatar_bytes))
        if not pending.birth_certificate:
            pending.birth_certificate.save("demo-birth-certificate.png", ContentFile(avatar_bytes))
        experts = {}
        for p_index, province in enumerate(provinces.values()):
            for slot in range(3):
                name = "expert" + _letters(p_index) + string.ascii_lowercase[slot]
                expert_user = account(name, "expert")
                if expert_user.province_id != province.pk:
                    expert_user.province = province
                    expert_user.save(update_fields=["province"])
                profile, _ = ExpertProfile.objects.get_or_create(user=expert_user, defaults={"province": province, "credential": "اعتبارنامه نمونه برای آزمایش رابط", "active": True})
                experts.setdefault(province.pk, []).append(profile)
        specs = [
            ("کشنده ولوو FH500 مدل ۱۴۰۱", "Volvo", "FH500", "tractor", 8950000000, "truck-white.png"),
            ("ولوو FH500 سفید، آماده بازدید", "Volvo", "FH500", "tractor", 8700000000, "truck-white.png"),
            ("کشنده ولوو FH500 کم‌کارکرد", "Volvo", "FH500", "tractor", 9300000000, "truck-red.png"),
            ("ولوو FH500 مدل ۱۴۰۲", "Volvo", "FH500", "tractor", 9100000000, "truck-white.png"),
            ("کشنده FH500 با سرویس کامل", "Volvo", "FH500", "tractor", 8500000000, "truck-red.png"),
            ("ولوو FH500 آماده معامله", "Volvo", "FH500", "tractor", 9000000000, "truck-white.png"),
            ("ولوو FH500 قرمز مدل ۱۴۰۱", "Volvo", "FH500", "tractor", 9200000000, "truck-red.png"),
            ("تریلر کفی سه محور", "ماموت", "کفی", "trailer", 1650000000, "trailer.png"),
            ("کامیون باری بنز آکسور", "Mercedes-Benz", "Axor", "truck", 6700000000, "truck-white.png"),
            ("کشنده اسکانیا R450", "Scania", "R450", "tractor", 8300000000, "truck-red.png"),
            ("بارکش ده چرخ، آماده کار", "FAW", "J6", "carrier", 4200000000, "truck-white.png"),
            ("تریلر کفی سالم سه محور", "مارال", "کفی", "trailer", 1750000000, "trailer.png"),
        ]
        listings = []
        for index, (title, brand, model, category, price, image) in enumerate(specs):
            owner = pro if index % 3 else plus
            listing, created = Listing.objects.get_or_create(owner=owner, title=title, defaults={"category": categories[category], "province": provinces["تهران"],
                "brand": brand, "model": model, "year": 1401 if index != 3 else 1402, "mileage": 180000 + index * 1000,
                "price": price, "city": "تهران", "description": "آگهی نمونه آزمایشی؛ مشخصات و قیمت برای بررسی جریان‌های پلتفرم هستند و پیشنهاد فروش واقعی محسوب نمی‌شوند.",
                "image_path": "kamiwa/images/" + image, "moderation_status": "approved", "status": "active"})
            listings.append(listing)
            if created:
                PriceHistory.objects.create(listing=listing, actor=owner, old_price=None, new_price=price)
        for index in [0, 2, 5]:
            listing = listings[index]
            inspection, _ = InspectionRequest.objects.get_or_create(listing=listing, requester=buyer, defaults={"expert": experts[provinces["تهران"].pk][0], "status": "final" if index != 5 else "reported", "coordination_note": "درخواست و بازدید نمونه آزمایشی"})
            if inspection.expert_id:
                InspectionReport.objects.get_or_create(inspection=inspection, defaults={"expert": inspection.expert, "submitted_by": inspection.expert.user,
                    "price": listing.price - 150000000, "summary": "گزارش نمونه: وضعیت بدنه و موتور برای آزمون ثبت شده است.",
                    "status": "final" if index != 5 else "pending", "approved_by": manager if index != 5 else None,
                    "finalized_at": now - timedelta(days=2) if index != 5 else None})
        Listing.objects.get_or_create(owner=pro, title="آگهی نمونه در انتظار تأیید", defaults={"category": categories["truck"], "province": provinces["تهران"], "brand": "FAW", "model": "J6", "year": 1400, "mileage": 210000, "price": 5800000000, "image_path": "kamiwa/images/truck-white.png"})
        Listing.objects.get_or_create(owner=plus, title="تریلر فروخته‌شده نمونه", defaults={"category": categories["trailer"], "province": provinces["تهران"], "brand": "ماموت", "model": "کفی", "year": 1399, "mileage": 0, "price": 1200000000, "status": "sold", "moderation_status": "approved", "sold_at": now - timedelta(days=4), "image_path": "kamiwa/images/trailer.png"})
        ticket, created = Ticket.objects.get_or_create(owner=pro, subject="راهنمای ثبت آگهی کارشناسی‌شده", defaults={"status": "answered"})
        if created:
            TicketMessage.objects.create(ticket=ticket, author=pro, body="چگونه برای آگهی خود درخواست کارشناسی ثبت کنم؟")
            TicketMessage.objects.create(ticket=ticket, author=support, body="از صفحه آگهی، درخواست کارشناسی را انتخاب و پرداخت آزمایشی را تکمیل کنید.")
        self.stdout.write(self.style.SUCCESS("Fictional demo data is ready. Credentials are stored only in artifacts/private/demo-accounts.json."))
