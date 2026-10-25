"""Kamiwa domain. Private files deliberately have no public URL."""
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models.functions import Lower
from django.utils import timezone
from django.templatetags.static import static
from .files import PrivateStorage, validate_private_file, validate_public_image


private_storage = PrivateStorage()
letters = RegexValidator(r"\A[A-Za-z]+\Z", "فقط حروف انگلیسی مجاز است.")


class Province(models.Model):
    name = models.CharField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")

    def clean(self):
        if self.parent_id and (self.parent_id == self.pk or self.parent.parent_id):
            raise ValidationError("زیرگروه باید به یک دسته اصلی متصل باشد.")
        if self.pk:
            old_parent = Category.objects.filter(pk=self.pk).values_list("parent_id", flat=True).first()
            if old_parent != self.parent_id and (self.listings.exists() or self.children.exists()):
                raise ValidationError("دسته دارای آگهی یا زیرگروه قابل انتقال نیست؛ یک دسته جدید بسازید.")

    @property
    def root_id(self):
        return self.parent_id or self.pk

    def __str__(self):
        return self.name


class Account(AbstractUser):
    class Role(models.TextChoices):
        BUYER = "buyer", "خریدار"
        PLUS = "plus", "پلاس"
        PRO = "pro", "پرو"
        EXPERT = "expert", "کارشناس"
        STAFF = "staff", "مدیریت"

    class Identity(models.TextChoices):
        PENDING = "pending", "در انتظار بررسی"
        APPROVED = "approved", "تأیید شده"
        REJECTED = "rejected", "رد شده"

    username = models.CharField(max_length=150, unique=True, validators=[letters])
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.BUYER)
    internal_code = models.PositiveIntegerField(unique=True, editable=False, null=True)
    mobile = models.CharField(max_length=20, blank=True)
    national_id = models.CharField(max_length=10, blank=True)
    birth_date = models.DateField(null=True, blank=True)
    province = models.ForeignKey(Province, on_delete=models.PROTECT, null=True, blank=True)
    city = models.CharField(max_length=100, blank=True)
    identity_document = models.FileField("تصویر کارت ملی", storage=private_storage, upload_to="identity/%Y/%m/", blank=True, validators=[validate_private_file])
    birth_certificate = models.FileField("تصویر شناسنامه", storage=private_storage, upload_to="identity/%Y/%m/", blank=True, validators=[validate_private_file])
    # Identity/contact edits wait here until staff approve them; the approved values stay in effect meanwhile.
    pending_profile = models.JSONField(default=dict, blank=True)
    pending_profile_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_verified = models.BooleanField(default=False)
    mobile_verified = models.BooleanField(default=False)
    identity_status = models.CharField(max_length=12, choices=Identity.choices, default=Identity.PENDING)
    identity_reason = models.TextField(blank=True)
    subscription_until = models.DateTimeField(null=True, blank=True)
    restricted_until = models.DateTimeField(null=True, blank=True)
    restriction_reason = models.TextField(blank=True)
    session_version = models.PositiveIntegerField(default=1)
    mfa_secret_encrypted = models.TextField(blank=True)
    mfa_last_counter = models.BigIntegerField(default=-1)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("username"), name="account_username_ci"),
                       models.UniqueConstraint(Lower("email"), condition=~models.Q(email=""), name="account_email_ci"),
                       models.UniqueConstraint(fields=["mobile"], condition=~models.Q(mobile=""), name="account_mobile_unique"),
                       models.CheckConstraint(condition=models.Q(internal_code__gte=10000, internal_code__lte=99999) | models.Q(internal_code__isnull=True), name="account_code_range")]
        permissions = [("review_identity", "Review and edit private identity"), ("moderate_listing", "Moderate listings and seller profiles"), ("finalize_report", "Assign experts and finalize reports"), ("support_tickets", "Read and reply to all support tickets"), ("manage_accounts", "Restrict and disable accounts"), ("manage_billing", "Manage plan prices and payments")]

    ONLINE_WINDOW = timedelta(minutes=5)

    @property
    def is_online(self):
        return bool(self.last_seen_at and timezone.now() - self.last_seen_at <= self.ONLINE_WINDOW)

    def save(self, *args, **kwargs):
        self.username = self.username.lower()
        self.email = self.email.strip().lower()
        if self.internal_code is None:
            # Collision is resolved by retrying registration inside an inner atomic block.
            self.internal_code = secrets.randbelow(90000) + 10000
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.pk:
            original_role = Account.objects.filter(pk=self.pk).values_list("role", flat=True).first()
            if original_role and original_role != self.role and (
                    SellerPage.objects.filter(user_id=self.pk).exists()
                    or ExpertProfile.objects.filter(user_id=self.pk).exists()):
                raise ValidationError({"role": "تغییر نوع حساب دارای صفحه فروشنده یا پروفایل کارشناس به فرایند انتقال اختصاصی نیاز دارد."})

    @property
    def is_seller(self):
        return self.role in {self.Role.PRO, self.Role.PLUS}

    @property
    def is_verified(self):
        return self.identity_status == self.Identity.APPROVED and self.email_verified and self.mobile_verified

    @property
    def page_slug(self):
        return self.page.slug if hasattr(self, "page") else ""

    @property
    def display_name(self):
        return self.page.display_name if hasattr(self, "page") else self.get_full_name() or self.username

    @property
    def profile_photo(self):
        return self.page.avatar if hasattr(self, "page") else None

    @property
    def identity_rejection_reason(self):
        return self.identity_reason

    @property
    def is_restricted(self):
        return bool(self.restricted_until and self.restricted_until > timezone.now())

    @property
    def is_entitled(self):
        return bool(self.is_active and not self.is_restricted and self.identity_status == self.Identity.APPROVED and self.email_verified and self.mobile_verified and self.subscription_until and self.subscription_until > timezone.now())

    @property
    def days_remaining(self):
        if not self.subscription_until:
            return 0
        return max(0, int((self.subscription_until - timezone.now()).total_seconds() + 86399) // 86400)


class ActivityDay(models.Model):
    """Approximate signed-in minutes per user per day, sampled by the session middleware."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="activity_days")
    day = models.DateField(default=timezone.localdate)
    minutes = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "day"], name="activity_user_day")]


class SellerPage(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="page")
    slug = models.CharField(max_length=17, unique=True)
    display_name = models.CharField(max_length=160)
    bio = models.TextField(blank=True)
    address = models.TextField(blank=True)
    activity = models.TextField(blank=True)
    avatar = models.ImageField(upload_to="avatars/%Y/%m/", blank=True, validators=[validate_public_image])
    license_document = models.FileField(storage=private_storage, upload_to="licenses/%Y/%m/", blank=True, validators=[validate_private_file])
    contact_phone = models.CharField(max_length=24, blank=True)
    contact_whatsapp = models.CharField(max_length=24, blank=True)
    contact_telegram = models.CharField(max_length=64, blank=True)
    contact_email = models.EmailField(blank=True)
    moderation_status = models.CharField(max_length=12, default="pending", choices=[("pending", "در انتظار"), ("approved", "تأیید شده"), ("rejected", "رد شده")])
    pending_data = models.JSONField(default=dict, blank=True)
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("slug"), name="seller_slug_ci")]

    def __str__(self):
        return self.display_name


class SlugHistory(models.Model):
    page = models.ForeignKey(SellerPage, on_delete=models.CASCADE, related_name="slug_history")
    slug = models.CharField(max_length=17, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Listing(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "فعال"
        SOLD = "sold", "فروخته شده"
        PAUSED = "paused", "توقف موقت"
        DISABLED = "disabled", "غیرفعال دائم"
        ARCHIVED = "archived", "آرشیو"
        DELETED = "deleted", "حذف شده"

    class Moderation(models.TextChoices):
        DRAFT = "draft", "پیش‌نویس"
        PENDING = "pending", "در انتظار تأیید"
        APPROVED = "approved", "تأیید شده"
        REJECTED = "rejected", "رد شده"

    class Negotiability(models.TextChoices):
        FIXED = "fixed", "قیمت قطعی"
        SMALL = "small", "کمی تخفیف"
        NEGOTIABLE = "negotiable", "قابل مذاکره"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="listings")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="listings")
    province = models.ForeignKey(Province, on_delete=models.PROTECT)
    title = models.CharField(max_length=180)
    brand = models.CharField(max_length=80)
    model = models.CharField(max_length=100)
    year = models.PositiveIntegerField(validators=[MinValueValidator(1300)])
    mileage = models.PositiveIntegerField(default=0)
    price = models.DecimalField(max_digits=18, decimal_places=0, validators=[MinValueValidator(0)])
    negotiability = models.CharField(max_length=12, choices=Negotiability.choices, default=Negotiability.NEGOTIABLE)
    description = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    image_path = models.CharField(max_length=250, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    moderation_status = models.CharField(max_length=12, choices=Moderation.choices, default=Moderation.PENDING)
    pending_data = models.JSONField(default=dict, blank=True)
    rejection_reason = models.TextField(blank=True)
    sold_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "moderation_status", "province"]), models.Index(fields=["owner", "category", "status"])]
        constraints = [models.CheckConstraint(condition=models.Q(price__gte=0), name="listing_nonnegative_price")]

    @property
    def final_report(self):
        if hasattr(self, '_final_report_snapshot'):
            return self._final_report_snapshot
        # Final appraisals are public to entitled buyers; the inspection row behind them is not.
        from .rls import system_context
        with system_context():
            return InspectionReport.objects.filter(inspection__listing=self, status="final").order_by("-finalized_at").first()

    @property
    def appraisal_price(self):
        report = self.final_report
        return report.price if report else None

    @property
    def appraisal_date(self):
        report = self.final_report
        return report.finalized_at if report else None

    @property
    def model_name(self):
        return self.model

    @property
    def image_url(self):
        if hasattr(self, '_image_url_snapshot'):
            return self._image_url_snapshot
        image = self.images.filter(approved=True).first() if self.pk else None
        if image:
            return image.image.url
        from .site_images import listing_placeholder
        return listing_placeholder(self.pk)

    @property
    def expert_price(self):
        return self.appraisal_price

    @property
    def expert_date(self):
        return self.appraisal_date

    @property
    def seller(self):
        return self.owner

    @property
    def negotiation(self):
        return self.negotiability

    def __str__(self):
        return self.title


class ListingImage(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="listings/%Y/%m/", validators=[validate_public_image])
    position = models.PositiveSmallIntegerField(default=0)
    approved = models.BooleanField(default=False)

    class Meta:
        ordering = ["position", "id"]


class ListingVideo(models.Model):
    """Pro-only listing video (MP4/MOV, at most one minute and 1080p); published with the listing."""
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="videos")
    video = models.FileField(upload_to="listings/videos/%Y/%m/")
    content_type = models.CharField(max_length=40, default="video/mp4")
    seconds = models.PositiveSmallIntegerField(default=0)
    width = models.PositiveSmallIntegerField(default=0)
    height = models.PositiveSmallIntegerField(default=0)
    position = models.PositiveSmallIntegerField(default=0)
    approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]


class PriceHistory(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="price_history")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    old_price = models.DecimalField(max_digits=18, decimal_places=0, null=True)
    new_price = models.DecimalField(max_digits=18, decimal_places=0)
    created_at = models.DateTimeField(auto_now_add=True)


class ExpertProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="expert_profile")
    province = models.ForeignKey(Province, on_delete=models.PROTECT, related_name="experts")
    credential = models.CharField(max_length=200)
    active = models.BooleanField(default=True)
    wallet_balance = models.DecimalField(max_digits=18, decimal_places=0, default=0, validators=[MinValueValidator(0)])
    iban = models.CharField("شماره شبا", max_length=26, blank=True)

    def clean(self):
        if self.user_id and self.user.role != "expert":
            raise ValidationError("حساب کارشناس باید نقش کارشناسی داشته باشد.")
        if self.active and self.province_id and ExpertProfile.objects.filter(province_id=self.province_id, active=True).exclude(pk=self.pk).count() >= 3:
            raise ValidationError("در هر استان حداکثر سه کارشناس فعال مجاز است.")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            Province.objects.select_for_update().get(pk=self.province_id)
            self.clean()
            super().save(*args, **kwargs)

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class InspectionRequest(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name="inspections")
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inspection_requests")
    expert = models.ForeignKey(ExpertProfile, on_delete=models.PROTECT, null=True, blank=True, related_name="requests")
    status = models.CharField(max_length=16, default="awaiting_payment", choices=[("awaiting_payment", "در انتظار پرداخت"), ("paid", "پرداخت شده"), ("assigned", "تخصیص کارشناس"), ("reported", "گزارش ثبت شده"), ("final", "نهایی"), ("cancelled", "لغو شده")])
    coordination_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class WalletTransaction(models.Model):
    """Append-only expert wallet ledger; balance_after makes every row auditable on its own."""
    KINDS = [("credit", "شارژ بابت کارشناسی"), ("withdrawal", "برداشت"), ("refund", "بازگشت برداشت ردشده")]
    expert = models.ForeignKey("ExpertProfile", on_delete=models.PROTECT, related_name="wallet_transactions")
    kind = models.CharField(max_length=12, choices=KINDS)
    amount = models.DecimalField(max_digits=18, decimal_places=0, validators=[MinValueValidator(1)])
    balance_after = models.DecimalField(max_digits=18, decimal_places=0)
    inspection = models.ForeignKey("InspectionRequest", on_delete=models.PROTECT, null=True, blank=True, related_name="wallet_credits")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["inspection"], condition=models.Q(kind="credit"), name="wallet_one_credit_per_inspection"),
                       models.CheckConstraint(condition=models.Q(balance_after__gte=0), name="wallet_balance_not_negative")]


class WithdrawalRequest(models.Model):
    STATUSES = [("pending", "در حال انجام"), ("paid", "واریز شد"), ("rejected", "رد شد")]
    expert = models.ForeignKey("ExpertProfile", on_delete=models.PROTECT, related_name="withdrawals")
    amount = models.DecimalField(max_digits=18, decimal_places=0, validators=[MinValueValidator(1)])
    iban = models.CharField(max_length=26)
    status = models.CharField(max_length=10, choices=STATUSES, default="pending")
    reference = models.CharField(max_length=120, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class InspectionReport(models.Model):
    inspection = models.OneToOneField(InspectionRequest, on_delete=models.PROTECT, related_name="report")
    expert = models.ForeignKey(ExpertProfile, on_delete=models.PROTECT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submitted_reports")
    price = models.DecimalField(max_digits=18, decimal_places=0, validators=[MinValueValidator(1)])
    summary = models.TextField()
    private_document = models.FileField(storage=private_storage, upload_to="reports/%Y/%m/", blank=True, validators=[validate_private_file])
    status = models.CharField(max_length=12, default="pending", choices=[("pending", "در انتظار تأیید"), ("final", "نهایی")])
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="approved_reports")
    finalized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(price__gt=0), name="report_positive_price")]


class Ticket(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tickets")
    subject = models.CharField(max_length=180)
    status = models.CharField(max_length=12, default="open", choices=[("open", "باز"), ("answered", "پاسخ داده شده"), ("closed", "بسته شده")])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class TicketMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class Favorite(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorites")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="favorites")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "listing"], name="favorite_unique")]


class ViewEvent(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="views", null=True, blank=True)
    page = models.ForeignKey(SellerPage, on_delete=models.CASCADE, related_name="views", null=True, blank=True)
    day = models.DateField(default=timezone.localdate)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(listing__isnull=False, page__isnull=True) | models.Q(listing__isnull=True, page__isnull=False)), name="view_exactly_one_target"), models.UniqueConstraint(fields=["user", "listing", "day"], name="view_listing_daily"), models.UniqueConstraint(fields=["user", "page", "day"], name="view_page_daily")]


class Plan(models.Model):
    role = models.CharField(max_length=12, unique=True, choices=[("buyer", "خریدار"), ("plus", "پلاس"), ("pro", "پرو")])
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=18, decimal_places=0, validators=[MinValueValidator(1)])
    duration_days = models.PositiveIntegerField(default=30, validators=[MinValueValidator(1)])
    inspection_fee = models.DecimalField(max_digits=18, decimal_places=0, default=2500000, validators=[MinValueValidator(1)])
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    @property
    def quota(self):
        return {"buyer": 0, "plus": 25, "pro": 50}.get(self.role, 0)


class Payment(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=16, choices=[("subscription", "اشتراک"), ("inspection", "کارشناسی")])
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, null=True, blank=True)
    inspection = models.ForeignKey(InspectionRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="payments")
    amount = models.DecimalField(max_digits=18, decimal_places=0)
    duration_days = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, default="pending", choices=[("pending", "در انتظار"), ("delayed", "با تأخیر"), ("success", "موفق آزمایشی"), ("failure", "ناموفق")])
    reference = models.CharField(max_length=100, unique=True)
    is_test = models.BooleanField(default=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_positive_amount"),
            models.CheckConstraint(condition=(models.Q(kind="subscription", plan__isnull=False, inspection__isnull=True, duration_days__gt=0) | models.Q(kind="inspection", plan__isnull=True, inspection__isnull=False, duration_days=0)), name="payment_target_shape"),
        ]


class OutboxMessage(models.Model):
    channel = models.CharField(max_length=12, choices=[("email", "ایمیل"), ("sms", "پیامک")])
    target = models.CharField(max_length=254)
    payload_ciphertext = models.TextField()
    status = models.CharField(max_length=12, default="pending")
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    idempotency_key = models.CharField(max_length=160, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=80)
    object_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=64)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class CredentialRotation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="rotations")
    period_key = models.CharField(max_length=100, unique=True)
    previous_username = models.CharField(max_length=150)
    new_username = models.CharField(max_length=150)
    payload_ciphertext = models.TextField(blank=True)
    status = models.CharField(max_length=12, default="pending")
    applied_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    outbox = models.ForeignKey(OutboxMessage, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class OTP(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE, related_name="otps")
    target = models.CharField(max_length=254)
    channel = models.CharField(max_length=12)
    purpose = models.CharField(max_length=40, default="verify")
    code_hash = models.CharField(max_length=256)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
