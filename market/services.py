"""Permission-checked domain operations. HTTP input must only enter through these APIs.

Every operation changing publication capacity serializes on the seller account before
locking the listing. PostgreSQL is the deployment database; SQLite is for smoke tests.
"""
import re
import secrets
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Account, AuditEvent, Category, ExpertProfile, Favorite, InspectionReport,
    InspectionRequest, Listing, ListingImage, ListingVideo, Payment, Plan, PriceHistory,
    SellerPage, SlugHistory, Ticket, TicketMessage, ViewEvent,
)


def _account(actor):
    if not actor or not getattr(actor, "is_authenticated", False):
        raise PermissionDenied("ابتدا وارد حساب خود شوید.")
    current = Account.objects.get(pk=actor.pk)
    if not current.is_active:
        raise PermissionDenied("این حساب غیرفعال است.")
    return current


def require_entitled(actor):
    actor = _account(actor)
    if not actor.is_restricted and ((actor.role == "staff" and actor.is_staff) or
                                    (actor.role == "expert" and ExpertProfile.objects.filter(user=actor, active=True).exists())):
        return actor
    if not actor.is_entitled:
        raise PermissionDenied("مشاهده بازار به تأیید هویت و اشتراک فعال نیاز دارد.")
    return actor


def _permission(actor, permission):
    actor = _account(actor)
    if not actor.is_staff or actor.is_restricted or not actor.has_perm("market." + permission):
        raise PermissionDenied("شما مجوز انجام این عملیات را ندارید.")
    return actor


def _seller(actor, entitled=True):
    actor = require_entitled(actor) if entitled else _account(actor)
    if not actor.is_seller or actor.is_restricted:
        raise PermissionDenied("این عملیات مخصوص حساب فروشنده است.")
    return actor


def audit(actor, action, obj, before=None, after=None):
    """Only explicitly chosen non-sensitive field values belong in audit records."""
    from .rls import system_context
    with system_context():
        return AuditEvent.objects.create(actor=actor, action=action,
                                    object_type=obj._meta.model_name, object_id=str(obj.pk),
                                    before=before or {}, after=after or {})


def visible_listings(actor=None, statuses=("active",)):
    if actor is not None:
        require_entitled(actor)
    now = timezone.now()
    if not set(statuses).issubset({"active", "sold"}):
        raise ValidationError("فقط آگهی‌های فعال و فروخته‌شده در صفحه عمومی مجازند.")
    return Listing.objects.filter(
        status__in=statuses, moderation_status="approved", owner__is_active=True,
        owner__role__in=["pro", "plus"],
        owner__identity_status="approved", owner__email_verified=True,
        owner__mobile_verified=True, owner__subscription_until__gt=now,
        owner__page__moderation_status="approved",
    ).filter(Q(owner__restricted_until__isnull=True) | Q(owner__restricted_until__lte=now)).select_related(
        "owner", "owner__page", "category", "province")


def _owned(actor, listing, entitled=True):
    actor = _account(actor)
    if actor.pk != listing.owner_id:
        _permission(actor, "moderate_listing")
    else:
        _seller(actor, entitled=entitled)
    return actor


def _capacity(owner, category, exclude=None):
    """Reserve pending submissions too; pending cross-category edits reserve both roots."""
    limit = {"pro": 50, "plus": 25}.get(owner.role, 0)
    if not limit:
        raise PermissionDenied("این حساب اجازه انتشار آگهی ندارد.")
    root_id = category.root_id
    roots = {c.pk: c.root_id for c in Category.objects.all()}
    used = 0
    for row in Listing.objects.filter(owner=owner, status="active").exclude(pk=exclude).exclude(
            moderation_status__in=["rejected", "draft"]).values("category_id", "pending_data"):
        targets = {roots.get(row["category_id"])}
        proposed = row["pending_data"].get("category_id")
        if proposed:
            targets.add(roots.get(int(proposed)))
        if root_id in targets:
            used += 1
    if used >= limit:
        raise ValidationError("ظرفیت این دسته تکمیل شده است؛ یک آگهی را آرشیو یا فروخته‌شده کنید.")
    return limit - used


def quota_usage(actor):
    actor = _account(actor)
    roots = list(Category.objects.filter(parent=None))
    lookup = {c.pk: c.root_id for c in Category.objects.all()}
    counts = {c.pk: 0 for c in roots}
    limit = {"pro": 50, "plus": 25}.get(actor.role, 0)
    for row in Listing.objects.filter(owner=actor, status="active").exclude(
            moderation_status__in=["draft", "rejected"]).values("category_id", "pending_data"):
        targets = {lookup.get(row["category_id"])}
        if row["pending_data"].get("category_id"):
            targets.add(lookup.get(int(row["pending_data"]["category_id"])))
        for root_id in targets:
            if root_id in counts:
                counts[root_id] += 1
    return [{"category": c, "name": c.name, "used": counts[c.pk], "limit": limit,
             "remaining": max(0, limit - counts[c.pk])} for c in roots]


CONTENT_FIELDS = ("title", "category_id", "brand", "model", "year", "mileage",
                  "province_id", "city", "negotiability", "description")


def _listing_values(data):
    values = {key: data[key] for key in CONTENT_FIELDS if key in data}
    for key in ("category", "province"):
        if key in data:
            values[key + "_id"] = getattr(data[key], "pk", data[key])
    return values


def _money(value):
    try:
        value = Decimal(str(value))
        if not value.is_finite() or value < 0 or value >= Decimal("1e18") or value != value.to_integral_value():
            raise ValueError
        return value
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("قیمت باید عدد صحیح نامنفی و حداکثر ۱۸ رقم باشد.")


@transaction.atomic
def submit_listing(actor, data, listing=None):
    if listing:
        existing = Listing.objects.get(pk=listing.pk)
        actor = _owned(actor, existing)
        owner = Account.objects.select_for_update().get(pk=existing.owner_id)
    else:
        actor = _seller(actor)
        owner = Account.objects.select_for_update().get(pk=actor.pk)
    if actor.pk == owner.pk:
        require_entitled(owner)
    values = _listing_values(data)
    if listing:
        listing = Listing.objects.select_for_update().get(pk=listing.pk)
        if listing.owner_id != owner.pk:
            raise PermissionDenied("ویرایش آگهی دیگران مجاز نیست.")
        if listing.status == "deleted" or ((listing.status in {"disabled", "sold"} or listing.sold_at) and actor.pk == owner.pk):
            raise ValidationError("این آگهی قابل ویرایش نیست.")
        candidate = Listing.objects.get(pk=listing.pk)
        for key, value in values.items():
            setattr(candidate, key, value)
        candidate.full_clean()
        if listing.status == "active":
            _capacity(owner, candidate.category, exclude=listing.pk)
        if "price" in data and _money(data["price"]) != listing.price:
            _set_price(actor, listing, data["price"])
        if listing.moderation_status == "approved":
            listing.pending_data = values
        else:
            for key, value in values.items():
                setattr(listing, key, value)
            listing.moderation_status = "pending"
            listing.pending_data = values
    else:
        listing = Listing(owner=owner, price=_money(data.get("price")), **values)
        listing.full_clean()
        _capacity(owner, listing.category)
        listing.pending_data = values
    listing.rejection_reason = ""
    listing.save()
    if not listing.price_history.exists():
        PriceHistory.objects.create(listing=listing, actor=actor, old_price=None, new_price=listing.price)
    _update_listing_media(actor, owner, listing, data)
    audit(actor, "listing.submit", listing, after={"status": listing.status, "pending": True})
    return listing


MAX_LISTING_IMAGES = 5
MAX_LISTING_VIDEOS = 2


def _delete_files_later(fields):
    pairs = [(field.storage, field.name) for field in fields if field]
    transaction.on_commit(lambda: [storage.delete(name) for storage, name in pairs])


def _update_listing_media(actor, owner, listing, data):
    """Remove chosen media, then add new photos (max 5) and Pro-only videos (max 2) for review."""
    from .files import sanitize_image, validate_video, VIDEO_SUFFIXES
    remove_images = listing.images.filter(pk__in=[int(i) for i in data.get("remove_images", []) if str(i).isdigit()])
    remove_videos = listing.videos.filter(pk__in=[int(i) for i in data.get("remove_videos", []) if str(i).isdigit()])
    _delete_files_later([item.image for item in remove_images] + [item.video for item in remove_videos])
    removed = {"images": remove_images.count(), "videos": remove_videos.count()}
    remove_images.delete()
    remove_videos.delete()
    images = list(data.get("images") or []) + ([data["image"]] if data.get("image") else [])
    if listing.images.count() + len(images) > MAX_LISTING_IMAGES:
        raise ValidationError("هر آگهی حداکثر ۵ تصویر می‌تواند داشته باشد؛ ابتدا تصاویر قبلی را حذف کنید.")
    videos = list(data.get("videos") or [])
    if videos and owner.role != "pro":
        raise PermissionDenied("افزودن ویدیو فقط برای حساب پرو ممکن است.")
    if listing.videos.count() + len(videos) > MAX_LISTING_VIDEOS:
        raise ValidationError("هر آگهی حداکثر ۲ ویدیو می‌تواند داشته باشد؛ ابتدا ویدیوهای قبلی را حذف کنید.")
    position = listing.images.count()
    for offset, upload in enumerate(images):
        ListingImage.objects.create(listing=listing, image=sanitize_image(upload), position=position + offset, approved=False)
    position = listing.videos.count()
    for offset, upload in enumerate(videos):
        info = validate_video(upload)
        suffix = Path(upload.name).suffix.lower()
        upload.name = secrets.token_hex(16) + suffix
        ListingVideo.objects.create(listing=listing, video=upload, content_type=VIDEO_SUFFIXES[suffix], seconds=round(info["seconds"]),
                                    width=info["width"], height=info["height"], position=position + offset, approved=False)
    if images or videos or any(removed.values()):
        audit(actor, "listing.media", listing, after={"added_images": len(images), "added_videos": len(videos), **{"removed_" + k: v for k, v in removed.items()}})


def create_listing(actor, data, image=None):
    return submit_listing(actor, {**data, **({"image": image} if image else {})})


def submit_listing_edit(actor, listing, data, image=None):
    return submit_listing(actor, {**data, **({"image": image} if image else {})}, listing)


@transaction.atomic
def approve_listing(actor, listing, approve=True, reason=""):
    actor = _permission(actor, "moderate_listing")
    owner = Account.objects.select_for_update().get(pk=listing.owner_id)
    listing = Listing.objects.select_for_update().get(pk=listing.pk)
    if listing.status == "deleted":
        raise ValidationError("آگهی حذف شده قابل تأیید نیست.")
    old = listing.moderation_status
    if approve:
        if not owner.is_entitled:
            raise ValidationError("فروشنده باید هویت تأییدشده و اشتراک فعال داشته باشد.")
        values = {k: v for k, v in listing.pending_data.items() if k in CONTENT_FIELDS}
        for key, value in values.items():
            setattr(listing, key, value)
        listing.full_clean()
        if listing.status == "active":
            _capacity(owner, listing.category, exclude=listing.pk)
        listing.moderation_status = "approved"
        listing.rejection_reason = ""
        listing.images.filter(approved=False).update(approved=True)
        listing.videos.filter(approved=False).update(approved=True)
    else:
        if not reason.strip():
            raise ValidationError("دلیل رد آگهی را وارد کنید.")
        # An already-approved version continues to be shown after a rejected edit.
        if old != "approved":
            listing.moderation_status = "rejected"
        listing.rejection_reason = reason.strip()[:2000]
        rejected_images, rejected_videos = listing.images.filter(approved=False), listing.videos.filter(approved=False)
        _delete_files_later([item.image for item in rejected_images] + [item.video for item in rejected_videos])
        rejected_images.delete()
        rejected_videos.delete()
    listing.pending_data = {}
    listing.save()
    audit(actor, "listing.approve" if approve else "listing.reject", listing,
          before={"moderation": old}, after={"moderation": listing.moderation_status})
    return listing


@transaction.atomic
def change_listing_status(actor, listing, status):
    listing = Listing.objects.get(pk=listing.pk)
    actor = _owned(actor, listing, entitled=(status == "active"))
    owner = Account.objects.select_for_update().get(pk=listing.owner_id)
    listing = Listing.objects.select_for_update().get(pk=listing.pk)
    if status not in Listing.Status.values:
        raise ValidationError("وضعیت نامعتبر است.")
    old = listing.status
    if old == status:
        return listing
    if old == "deleted" or (old == "disabled" and status != "deleted") or (
            (old == "sold" or listing.sold_at) and status not in {"archived", "deleted"}):
        raise ValidationError("آگهی فروخته‌شده، حذف‌شده یا غیرفعال دائم دوباره فعال نمی‌شود.")
    if status == "active":
        require_entitled(owner)
        _capacity(owner, listing.category, exclude=listing.pk)
        if listing.pending_data.get("category_id"):
            _capacity(owner, Category.objects.get(pk=listing.pending_data["category_id"]), exclude=listing.pk)
    listing.status = status
    if status == "sold":
        listing.sold_at = timezone.now()
    listing.save(update_fields=["status", "sold_at", "updated_at"])
    if status == "sold" and owner.role == "plus":
        extra = list(Listing.objects.filter(owner=owner, status="sold").order_by("-sold_at", "-pk").values_list("pk", flat=True)[50:])
        if extra:
            Listing.objects.filter(pk__in=extra).update(status="deleted")
    audit(actor, "listing.status", listing, before={"status": old}, after={"status": status})
    return listing


set_listing_status = change_listing_status


def _set_price(actor, listing, price):
    price = _money(price)
    if price != listing.price:
        PriceHistory.objects.create(listing=listing, actor=actor, old_price=listing.price, new_price=price)
        before = str(listing.price)
        listing.price = price
        listing.save(update_fields=["price", "updated_at"])
        audit(actor, "listing.price", listing, {"price": before}, {"price": str(price)})


@transaction.atomic
def update_price(actor, listing, price):
    listing = Listing.objects.get(pk=listing.pk)
    actor = _owned(actor, listing)
    Account.objects.select_for_update().get(pk=listing.owner_id)
    listing = Listing.objects.select_for_update().get(pk=listing.pk)
    if listing.status != "active":
        raise ValidationError("قیمت فقط برای آگهی فعال قابل ویرایش است.")
    _set_price(actor, listing, price)
    return listing


PAGE_FIELDS = ("slug", "display_name", "bio", "address", "activity", "contact_phone",
               "contact_whatsapp", "contact_telegram", "contact_email")


def normalize_slug(role, value):
    base = str(value).lower().removesuffix("_personal")
    if not re.fullmatch(r"[a-z]{1,8}", base):
        raise ValidationError("آیدی صفحه باید ۱ تا ۸ حرف انگلیسی باشد.")
    return base + ("_personal" if role == "plus" else "")


def _slug_available(slug, page=None):
    if SellerPage.objects.filter(slug__iexact=slug).exclude(pk=getattr(page, "pk", None)).exists():
        raise ValidationError("این آیدی صفحه قبلاً استفاده شده است.")
    historical = SlugHistory.objects.filter(slug__iexact=slug)
    if page:
        historical = historical.exclude(page=page)
    if historical.exists():
        raise ValidationError("این آیدی به صفحه دیگری تعلق دارد.")


@transaction.atomic
def submit_page(actor, data):
    actor = _seller(actor, entitled=False)
    Account.objects.select_for_update().get(pk=actor.pk)
    page = SellerPage.objects.select_for_update().filter(user=actor).first()
    values = {k: data[k] for k in PAGE_FIELDS if k in data}
    values["slug"] = normalize_slug(actor.role, values.get("slug", page.slug if page else ""))
    _slug_available(values["slug"], page)
    if actor.role == "plus":
        values["display_name"] = actor.get_full_name() or actor.username
    # Validate caller-supplied text through model validation before serializing it.
    candidate = SellerPage.objects.get(pk=page.pk) if page else SellerPage(user=actor)
    for k, v in values.items():
        setattr(candidate, k, v)
    candidate.full_clean(exclude=["avatar", "license_document", "pending_data"])
    if data.get("avatar") and hasattr(data["avatar"], "read"):
        from .files import sanitize_image
        candidate.avatar.save(secrets.token_hex(12) + ".jpg", sanitize_image(data["avatar"]), save=False)
        values["avatar"] = candidate.avatar.name
    if data.get("license_document") and hasattr(data["license_document"], "read"):
        from .files import validate_private_file
        upload = data["license_document"]
        validate_private_file(upload)
        candidate.license_document.save(secrets.token_hex(12) + "." + upload.name.rsplit(".", 1)[-1], upload, save=False)
        values["license_document"] = candidate.license_document.name
    if actor.role == "pro" and not all((candidate.display_name, candidate.address, candidate.activity,
                                        values.get("license_document") or candidate.license_document)):
        raise ValidationError("مشخصات نمایشگاه و جواز کسب برای پرو الزامی است.")
    if actor.role == "plus" and not (values.get("avatar") or candidate.avatar):
        raise ValidationError("عکس پرسنلی برای حساب پلاس الزامی است.")
    if page is None:
        page = candidate
        page.moderation_status = "pending"
    elif page.moderation_status != "approved":
        for k, v in values.items():
            setattr(page, k, v)
        page.moderation_status = "pending"
    page.pending_data = values
    page.rejection_reason = ""
    page.save()
    audit(actor, "page.submit", page, after={"pending": True})
    return page


def submit_page_edit(actor, page, data):
    if page.user_id != actor.pk:
        raise PermissionDenied("ویرایش صفحه دیگران مجاز نیست.")
    return submit_page(actor, data)


@transaction.atomic
def moderate_page(actor, page, approve=True, reason=""):
    actor = _permission(actor, "moderate_listing")
    Account.objects.select_for_update().get(pk=page.user_id)
    page = SellerPage.objects.select_for_update().get(pk=page.pk)
    old = page.moderation_status
    if approve:
        values = dict(page.pending_data)
        if page.user.role == "plus":
            values["display_name"] = page.user.get_full_name() or page.user.username
        slug = normalize_slug(page.user.role, values.get("slug", page.slug))
        _slug_available(slug, page)
        if page.slug != slug:
            # All historic slugs stay reserved against takeover and redirect to this page.
            SlugHistory.objects.get_or_create(slug=page.slug, defaults={"page": page})
            SlugHistory.objects.filter(page=page, slug=slug).delete()
        for k, v in values.items():
            if k in PAGE_FIELDS or k in {"avatar", "license_document"}:
                setattr(page, k, v)
        page.slug = slug
        page.moderation_status = "approved"
        page.rejection_reason = ""
    else:
        if not reason.strip():
            raise ValidationError("دلیل رد صفحه را وارد کنید.")
        if old != "approved":
            page.moderation_status = "rejected"
        page.rejection_reason = reason.strip()[:2000]
    page.pending_data = {}
    page.save()
    audit(actor, "page.approve" if approve else "page.reject", page, {"moderation": old}, {"moderation": page.moderation_status})
    return page


@transaction.atomic
def update_profile(actor, data):
    actor = _account(actor)
    user = Account.objects.select_for_update().get(pk=actor.pk)
    allowed = {"username", "first_name", "last_name", "national_id", "birth_date", "mobile", "email", "province", "city", "identity_document", "birth_certificate"}
    changed = []
    for key in allowed.intersection(data):
        value = data[key]
        if key in {"identity_document", "birth_certificate"} and not value:
            continue
        if key == "email":
            value = value.strip().lower()
            if user.is_superuser and value != user.email:
                raise PermissionDenied("ایمیل مدیر باید از مسیر تأیید دومرحله‌ای تغییر کند.")
        if key == "username":
            value = value.lower()
        if getattr(user, key) != value:
            changed.append(key)
            setattr(user, key, value)
    if "email" in changed:
        user.email_verified = False
    if "mobile" in changed:
        user.mobile_verified = False
    if set(changed) - {"username"}:
        user.identity_status = "pending"
        user.identity_reason = ""
    for name in ("identity_document", "birth_certificate"):
        if name in changed:
            from .files import validate_private_file
            validate_private_file(getattr(user, name))
    user.full_clean(exclude=["password", "mfa_secret_encrypted"])
    user.save()
    audit(actor, "account.profile", user, after={"changed_fields": sorted(changed)})
    return user


PROFILE_FIELDS = ("first_name", "last_name", "national_id", "birth_date", "mobile", "email", "province", "city")
PROFILE_FILES = ("identity_document", "birth_certificate")
PROFILE_LABELS = {"first_name": "نام", "last_name": "نام خانوادگی", "national_id": "کد ملی", "birth_date": "تاریخ تولد",
                  "mobile": "شماره همراه", "email": "ایمیل", "province": "استان", "city": "شهر",
                  "identity_document": "تصویر کارت ملی", "birth_certificate": "تصویر شناسنامه"}


def _store_private(user, field_name, upload):
    from .files import validate_private_file
    validate_private_file(upload)
    field = user._meta.get_field(field_name)
    return field.storage.save(field.generate_filename(user, upload.name), upload)


def _discard_pending_files(pending):
    for name in PROFILE_FILES:
        path = pending.get(name)
        if path:
            Account._meta.get_field(name).storage.delete(path)


def _profile_value(user, key):
    value = getattr(user, key + "_id" if key == "province" else key)
    return value.isoformat() if hasattr(value, "isoformat") else value


@transaction.atomic
def submit_profile_change(actor, data):
    """Approved accounts keep their verified identity until staff approve the requested values."""
    actor = _account(actor)
    user = Account.objects.select_for_update().get(pk=actor.pk)
    if user.identity_status != "approved":
        return update_profile(user, data)
    if "username" in data and data["username"].lower() != user.username:
        user.username = data["username"].lower()
        user.full_clean(exclude=["password", "mfa_secret_encrypted"])
        user.save(update_fields=["username"])
    requested = {}
    for key in PROFILE_FIELDS:
        if key not in data:
            continue
        value = getattr(data[key], "pk", data[key])
        if key == "birth_date" and value is not None:
            value = value.isoformat()
        if key == "email":
            value = value.strip().lower()
            if user.is_superuser and value != user.email:
                raise PermissionDenied("ایمیل مدیر باید از مسیر تأیید دومرحله‌ای تغییر کند.")
        if value != _profile_value(user, key):
            requested[key] = value
    if requested.get("email") and Account.objects.filter(email__iexact=requested["email"]).exclude(pk=user.pk).exists():
        raise ValidationError("این ایمیل برای حساب دیگری ثبت شده است.")
    if requested.get("mobile") and Account.objects.filter(mobile=requested["mobile"]).exclude(pk=user.pk).exists():
        raise ValidationError("این شماره همراه برای حساب دیگری ثبت شده است.")
    previous = user.pending_profile or {}
    for name in PROFILE_FILES:
        if data.get(name):
            requested[name] = _store_private(user, name, data[name])
        elif previous.get(name):
            requested[name] = previous[name]
    if not requested:
        return user
    _discard_pending_files({k: v for k, v in previous.items() if k in PROFILE_FILES and requested.get(k) != v})
    user.pending_profile = requested
    user.pending_profile_at = timezone.now()
    user.save(update_fields=["pending_profile", "pending_profile_at"])
    audit(actor, "account.profile_request", user, after={"fields": sorted(requested)})
    return user


@transaction.atomic
def review_profile_change(actor, user, approve=True, reason=""):
    actor = _permission(actor, "review_identity")
    user = Account.objects.select_for_update().get(pk=user.pk)
    pending = {k: v for k, v in (user.pending_profile or {}).items() if k in PROFILE_FIELDS + PROFILE_FILES}
    if not pending:
        raise ValidationError("درخواست تغییری برای این کاربر ثبت نشده است.")
    if not approve:
        if not reason.strip():
            raise ValidationError("دلیل رد درخواست الزامی است.")
        _discard_pending_files(pending)
        user.pending_profile = {"rejected_reason": reason.strip()[:2000]}
        user.pending_profile_at = timezone.now()
        user.save(update_fields=["pending_profile", "pending_profile_at"])
        audit(actor, "account.profile_reject", user, after={"fields": sorted(pending)})
        return user
    if pending.get("email") and Account.objects.filter(email__iexact=pending["email"]).exclude(pk=user.pk).exists():
        raise ValidationError("این ایمیل اکنون برای حساب دیگری ثبت شده است.")
    if pending.get("mobile") and Account.objects.filter(mobile=pending["mobile"]).exclude(pk=user.pk).exists():
        raise ValidationError("این شماره همراه اکنون برای حساب دیگری ثبت شده است.")
    replaced = []
    for key, value in pending.items():
        if key in PROFILE_FILES:
            old = getattr(user, key)
            if old and old.name != value:
                replaced.append((old.storage, old.name))
            setattr(user, key, value)
        elif key == "province":
            user.province_id = value
        elif key == "birth_date":
            user.birth_date = date.fromisoformat(value)
        else:
            setattr(user, key, value)
    if "email" in pending:
        user.email_verified = False
    if "mobile" in pending:
        user.mobile_verified = False
    user.pending_profile = {}
    user.pending_profile_at = None
    user.full_clean(exclude=["password", "mfa_secret_encrypted"])
    user.save()
    # Superseded identity documents are removed once the new ones are in force.
    transaction.on_commit(lambda: [storage.delete(name) for storage, name in replaced])
    audit(actor, "account.profile_approve", user, after={"fields": sorted(pending)})
    return user


@transaction.atomic
def approve_identity(actor, user, approve=True, reason=""):
    actor = _permission(actor, "review_identity")
    user = Account.objects.select_for_update().get(pk=user.pk)
    if approve and not (user.email_verified and user.mobile_verified):
        raise ValidationError("ابتدا ایمیل و شماره موبایل باید تأیید شوند.")
    if approve:
        if not all((user.first_name.strip(), user.last_name.strip(), user.national_id,
                    user.birth_date, user.province_id, user.city.strip(), user.identity_document,
                    user.birth_certificate, user.email, user.mobile)):
            raise ValidationError("اطلاعات هویتی، تصویر کارت ملی و شناسنامه، تاریخ تولد و محل فعالیت باید کامل باشند.")
        if not re.fullmatch(r"[0-9]{10}", user.national_id) or not re.fullmatch(r"09[0-9]{9}", user.mobile) or user.birth_date > timezone.localdate():
            raise ValidationError("کد ملی، شماره همراه یا تاریخ تولد معتبر نیست.")
        if user.role == "pro" and (not hasattr(user, "page") or not all((user.page.display_name, user.page.address, user.page.activity, user.page.license_document))):
            raise ValidationError("اطلاعات نمایشگاه و جواز کسب باید کامل باشند.")
        if user.role == "plus" and (not hasattr(user, "page") or not user.page.avatar):
            raise ValidationError("عکس پرسنلی برای حساب پلاس الزامی است.")
    if not approve and not reason.strip():
        raise ValidationError("دلیل رد هویت الزامی است.")
    old = user.identity_status
    user.identity_status = "approved" if approve else "rejected"
    user.identity_reason = "" if approve else reason.strip()[:2000]
    user.save(update_fields=["identity_status", "identity_reason"])
    if approve and user.role == "plus":
        SellerPage.objects.filter(user=user).update(display_name=user.get_full_name() or user.username)
    audit(actor, "identity.review", user, {"status": old}, {"status": user.identity_status})
    return user


@transaction.atomic
def restrict_account(actor, user, until=None, reason="", active=None):
    actor = _permission(actor, "manage_accounts")
    user = Account.objects.select_for_update().get(pk=user.pk)
    if user.is_superuser and not actor.is_superuser:
        raise PermissionDenied("مدیر بخش امکان محدود کردن مدیر اصلی را ندارد.")
    if user.pk == actor.pk:
        raise ValidationError("محدود کردن حساب خودتان مجاز نیست.")
    user.restricted_until = until
    user.restriction_reason = str(reason)[:2000]
    if active is not None:
        user.is_active = bool(active)
    user.session_version += 1
    user.save(update_fields=["restricted_until", "restriction_reason", "is_active", "session_version"])
    audit(actor, "account.restrict", user, after={"active": user.is_active, "until": until.isoformat() if until else None})
    return user


@transaction.atomic
def create_payment(actor, kind, plan=None, inspection=None):
    actor = _account(actor)
    actor = Account.objects.select_for_update().get(pk=actor.pk)
    if actor.is_restricted:
        raise PermissionDenied("حساب شما در حال حاضر محدود است.")
    if kind == "subscription":
        plan = Plan.objects.select_for_update().get(pk=getattr(plan, "pk", plan), active=True)
        if plan.role != actor.role or not plan.price or plan.price < 0:
            raise ValidationError("این بسته با نوع حساب شما سازگار نیست.")
        amount, duration = plan.price, plan.duration_days
        inspection = None
    elif kind == "inspection":
        require_entitled(actor)
        inspection = InspectionRequest.objects.select_for_update().get(pk=getattr(inspection, "pk", inspection))
        if inspection.requester_id != actor.pk:
            raise PermissionDenied("پرداخت این درخواست در اختیار شما نیست.")
        if inspection.status != "awaiting_payment":
            raise ValidationError("این درخواست قبلاً پرداخت یا لغو شده است.")
        plan = Plan.objects.get(role=actor.role, active=True)
        amount, duration = plan.inspection_fee, 0
    else:
        raise ValidationError("نوع پرداخت معتبر نیست.")
    if amount <= 0:
        raise ValidationError("تعرفه باید مثبت باشد.")
    payment = Payment.objects.create(user=actor, kind=kind, plan=plan if kind == "subscription" else None,
                                     inspection=inspection, amount=amount, duration_days=duration,
                                     reference="test-" + secrets.token_urlsafe(24), is_test=True)
    audit(actor, "payment.create", payment, after={"kind": kind, "amount": str(amount), "test": True})
    return payment


def create_subscription_payment(actor, plan):
    return create_payment(actor, "subscription", plan=plan)


@transaction.atomic
def settle_payment(payment, outcome="success"):
    """INTERNAL provider callback only; the caller must verify ownership/signature.

    Amount, duration and target come exclusively from the locked server-side invoice.
    Terminal outcomes stay terminal under replay; delay can later become success/failure.
    """
    if outcome not in {"success", "failure", "delayed", "delay"}:
        raise ValidationError("نتیجه پرداخت معتبر نیست.")
    if outcome == "delay":
        outcome = "delayed"
    account = Account.objects.select_for_update().get(pk=payment.user_id)
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if not payment.is_test:
        raise ValidationError("درگاه واقعی هنوز پیکربندی نشده است.")
    if payment.status in {"success", "failure"}:
        return payment
    if outcome == "success":
        if payment.kind == "subscription":
            if payment.plan.role != account.role:
                raise ValidationError("نوع حساب پس از ایجاد پرداخت تغییر کرده است.")
            account.subscription_until = max(account.subscription_until or timezone.now(), timezone.now()) + timedelta(days=payment.duration_days)
            account.save(update_fields=["subscription_until"])
        else:
            inspection = InspectionRequest.objects.select_for_update().get(pk=payment.inspection_id)
            if inspection.status != "awaiting_payment":
                # Distinct repeated checkout cannot charge the same request twice.
                payment.status = "failure"
                payment.settled_at = timezone.now()
                payment.save(update_fields=["status", "settled_at"])
                return payment
            inspection.status = "paid"
            inspection.save(update_fields=["status", "updated_at"])
    payment.status = outcome
    payment.settled_at = None if outcome == "delayed" else timezone.now()
    payment.save(update_fields=["status", "settled_at"])
    audit(None, "payment.settle", payment, after={"status": outcome, "test": True})
    return payment


@transaction.atomic
def request_inspection(actor, listing, note=""):
    actor = require_entitled(actor)
    listing = Listing.objects.get(pk=listing.pk)
    if not visible_listings().filter(pk=listing.pk).exists():
        # A seller may request an appraisal before first moderation of their own ad.
        if listing.owner_id != actor.pk or listing.status != "active":
            raise PermissionDenied("این آگهی برای درخواست کارشناسی در دسترس نیست.")
    inspection = InspectionRequest.objects.create(listing=listing, requester=actor, coordination_note=str(note)[:2000])
    audit(actor, "inspection.request", inspection, after={"listing_id": listing.pk})
    if listing.owner_id != actor.pk and listing.owner.email:
        from .providers import queue_message
        queue_message("email", listing.owner.email, {"title": "درخواست کارشناسی جدید · آزمایشی",
                      "body": "برای هماهنگی بازدید، درخواست کارشناسی جدید را در پنل خود بررسی کنید.",
                      "inspection_id": inspection.pk, "is_test": True},
                      idempotency_key="inspection-notify-" + str(inspection.pk))
    return inspection


@transaction.atomic
def assign_expert(actor, inspection, expert):
    actor = _permission(actor, "finalize_report")
    inspection = InspectionRequest.objects.select_for_update().get(pk=inspection.pk)
    expert = ExpertProfile.objects.select_for_update().get(pk=getattr(expert, "pk", expert))
    if inspection.status not in {"paid", "assigned"}:
        raise ValidationError("تخصیص کارشناس فقط پس از پرداخت و پیش از ثبت گزارش ممکن است.")
    if not expert.active or not expert.user.is_active or expert.user.is_restricted or expert.user.role != "expert" or expert.province_id != inspection.listing.province_id:
        raise ValidationError("کارشناس باید فعال و مربوط به استان آگهی باشد.")
    inspection.expert = expert
    inspection.status = "assigned"
    inspection.save(update_fields=["expert", "status", "updated_at"])
    audit(actor, "inspection.assign", inspection, after={"expert_id": expert.pk})
    return inspection


@transaction.atomic
def submit_report(actor, inspection, data):
    actor = _account(actor)
    inspection = InspectionRequest.objects.select_for_update().get(pk=inspection.pk)
    if not inspection.expert_id:
        raise ValidationError("ابتدا کارشناس مسئول را تعیین کنید.")
    if actor.pk != inspection.expert.user_id:
        _permission(actor, "finalize_report")
    elif actor.is_restricted or actor.role != "expert" or not inspection.expert.active:
        raise PermissionDenied("دسترسی کارشناس در حال حاضر فعال نیست.")
    report = InspectionReport.objects.select_for_update().filter(inspection=inspection).first()
    if inspection.status == "final" or (report and report.status == "final"):
        raise ValidationError("گزارش نهایی قابل تغییر نیست؛ کارشناسی جدید درخواست کنید.")
    if inspection.status not in {"assigned", "reported"}:
        raise ValidationError("درخواست برای ثبت گزارش آماده نیست.")
    price = _money(data.get("price"))
    if price <= 0 or not str(data.get("summary", "")).strip():
        raise ValidationError("قیمت مثبت و خلاصه گزارش الزامی است.")
    before = {"price": str(report.price), "revision": report.updated_at.isoformat()} if report else {}
    report = report or InspectionReport(inspection=inspection, expert=inspection.expert, submitted_by=actor)
    report.price = price
    report.summary = str(data["summary"])[:20000]
    report.submitted_by = actor
    if data.get("private_document"):
        from .files import validate_private_file
        upload = data["private_document"]
        validate_private_file(upload)
        report.private_document = upload
    report.full_clean()
    report.save()
    inspection.status = "reported"
    inspection.save(update_fields=["status", "updated_at"])
    audit(actor, "report.submit", report, before=before, after={"price": str(price), "expert_id": inspection.expert_id})
    return report


@transaction.atomic
def finalize_report(actor, report):
    actor = _permission(actor, "finalize_report")
    inspection = InspectionRequest.objects.select_for_update().get(pk=report.inspection_id)
    report = InspectionReport.objects.select_for_update().get(pk=report.pk)
    if report.status == "final":
        return report
    if inspection.status != "reported" or not inspection.expert_id:
        raise ValidationError("گزارش برای تأیید آماده نیست.")
    report.full_clean()
    report.status, report.approved_by, report.finalized_at = "final", actor, timezone.now()
    report.save(update_fields=["status", "approved_by", "finalized_at", "updated_at"])
    inspection.status = "final"
    inspection.save(update_fields=["status", "updated_at"])
    audit(actor, "report.finalize", report, after={"price": str(report.price), "expert_id": report.expert_id})
    return report


def _report_snapshot(report):
    return {"report_id": report.pk, "price": str(report.price), "status": report.status,
            "expert_id": report.expert_id, "summary_chars": len(report.summary)}


def _notify_inspection(inspection, title, body):
    from .providers import queue_message
    for user in {inspection.requester, inspection.listing.owner}:
        if user.email:
            queue_message("email", user.email, {"title": title, "body": body, "inspection_id": inspection.pk, "is_test": True})


@transaction.atomic
def cancel_inspection(actor, inspection, reason=""):
    actor = _permission(actor, "finalize_report")
    inspection = InspectionRequest.objects.select_for_update().select_related("listing__owner", "requester").get(pk=inspection.pk)
    if inspection.status in {"cancelled", "final"}:
        raise ValidationError("درخواست نهایی یا لغوشده را نمی‌توان لغو کرد؛ برای نتیجه نهایی از کارشناسی مجدد استفاده کنید.")
    if not reason.strip():
        raise ValidationError("دلیل لغو الزامی است.")
    old = inspection.status
    inspection.status = "cancelled"
    inspection.save(update_fields=["status", "updated_at"])
    audit(actor, "inspection.cancel", inspection, {"status": old}, {"status": "cancelled", "reason": reason.strip()[:500]})
    _notify_inspection(inspection, "لغو درخواست کارشناسی · آزمایشی", "درخواست کارشناسی توسط مدیریت لغو شد. دلیل: " + reason.strip()[:500])
    return inspection


@transaction.atomic
def reinspect(actor, inspection, reason=""):
    """Discard the current report and send the request back for a (possibly different) expert."""
    actor = _permission(actor, "finalize_report")
    inspection = InspectionRequest.objects.select_for_update().select_related("listing__owner", "requester").get(pk=inspection.pk)
    if inspection.status not in {"assigned", "reported", "final"}:
        raise ValidationError("کارشناسی مجدد فقط برای درخواست دارای کارشناس یا گزارش ممکن است.")
    if not reason.strip():
        raise ValidationError("دلیل کارشناسی مجدد الزامی است.")
    report = InspectionReport.objects.select_for_update().filter(inspection=inspection).first()
    before = {"status": inspection.status, "expert_id": inspection.expert_id}
    if report:
        before["report"] = _report_snapshot(report)
        _delete_report_files(report)
        report.delete()
    inspection.expert = None
    inspection.status = "paid"
    inspection.save(update_fields=["expert", "status", "updated_at"])
    audit(actor, "inspection.reinspect", inspection, before, {"status": "paid", "reason": reason.strip()[:500]})
    _notify_inspection(inspection, "کارشناسی مجدد · آزمایشی", "مدیریت کارشناسی مجدد این آگهی را آغاز کرد و کارشناس جدید تعیین می‌شود.")
    return inspection


def _delete_report_files(report):
    if report.private_document:
        storage, name = report.private_document.storage, report.private_document.name
        transaction.on_commit(lambda: storage.delete(name))


@transaction.atomic
def staff_edit_report(actor, report, data):
    actor = _permission(actor, "finalize_report")
    report = InspectionReport.objects.select_for_update().get(pk=report.pk)
    price = _money(data.get("price"))
    summary = str(data.get("summary", "")).strip()
    if price <= 0 or not summary:
        raise ValidationError("قیمت مثبت و خلاصه گزارش الزامی است.")
    before = {"price": str(report.price), "summary_chars": len(report.summary)}
    report.price, report.summary = price, summary[:20000]
    if data.get("private_document"):
        from .files import validate_private_file
        validate_private_file(data["private_document"])
        _delete_report_files(report)
        report.private_document = data["private_document"]
    report.full_clean()
    report.save()
    audit(actor, "report.staff_edit", report, before, {"price": str(price), "summary_chars": len(report.summary)})
    return report


@transaction.atomic
def delete_report(actor, report, reason=""):
    """Remove a report; the assigned expert (if still valid) can submit a new one."""
    actor = _permission(actor, "finalize_report")
    report = InspectionReport.objects.select_for_update().get(pk=report.pk)
    inspection = InspectionRequest.objects.select_for_update().get(pk=report.inspection_id)
    if not reason.strip():
        raise ValidationError("دلیل حذف گزارش الزامی است.")
    snapshot = _report_snapshot(report)
    _delete_report_files(report)
    report.delete()
    inspection.status = "assigned" if inspection.expert_id else "paid"
    inspection.save(update_fields=["status", "updated_at"])
    audit(actor, "report.delete", inspection, {"report": snapshot}, {"status": inspection.status, "reason": reason.strip()[:500]})
    return inspection


def normalize_iban(value):
    """Iranian IBAN (شبا): IR + 24 digits with a valid ISO 13616 mod-97 checksum."""
    text = re.sub(r"[\s\-]", "", str(value or "")).upper().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not text.startswith("IR"):
        text = "IR" + text
    if not re.fullmatch(r"IR[0-9]{24}", text):
        raise ValidationError("شماره شبا باید IR و ۲۴ رقم باشد.")
    rearranged = text[4:] + "1827" + text[2:4]  # I=18, R=27
    if int(rearranged) % 97 != 1:
        raise ValidationError("شماره شبا معتبر نیست؛ ارقام را دوباره بررسی کنید.")
    return text


def _wallet_entry(expert, kind, amount, actor, inspection=None, note=""):
    from .models import WalletTransaction
    expert.wallet_balance += amount if kind in {"credit", "refund"} else -amount
    if expert.wallet_balance < 0:
        raise ValidationError("موجودی کیف پول کافی نیست.")
    expert.save(update_fields=["wallet_balance"])
    return WalletTransaction.objects.create(expert=expert, kind=kind, amount=amount, balance_after=expert.wallet_balance,
                                            inspection=inspection, actor=actor, note=str(note)[:300])


@transaction.atomic
def credit_wallet(actor, expert, amount, inspection=None, note=""):
    actor = _permission(actor, "manage_billing")
    expert = ExpertProfile.objects.select_for_update().get(pk=getattr(expert, "pk", expert))
    amount = _money(amount)
    if amount <= 0:
        raise ValidationError("مبلغ شارژ باید مثبت باشد.")
    if inspection is not None:
        inspection = InspectionRequest.objects.select_for_update().get(pk=getattr(inspection, "pk", inspection))
        if inspection.expert_id != expert.pk or inspection.status != "final":
            raise ValidationError("فقط کارشناسی نهایی‌شده همین کارشناس قابل پرداخت است.")
        if inspection.wallet_credits.filter(kind="credit").exists():
            raise ValidationError("هزینه این کارشناسی قبلاً به کیف پول واریز شده است.")
    entry = _wallet_entry(expert, "credit", amount, actor, inspection, note)
    audit(actor, "wallet.credit", expert, after={"amount": str(amount), "inspection_id": getattr(inspection, "pk", None), "test": True})
    if expert.user.email:
        from .providers import queue_message
        queue_message("email", expert.user.email, {"title": "شارژ کیف پول کارشناسی · آزمایشی",
                      "body": f"کیف پول شما {amount:,} تومان شارژ شد.", "is_test": True})
    return entry


@transaction.atomic
def request_withdrawal(actor, amount, iban):
    from .models import WithdrawalRequest
    actor = _account(actor)
    if actor.role != "expert" or actor.is_restricted or not actor.is_active:
        raise PermissionDenied("برداشت فقط برای کارشناس فعال ممکن است.")
    expert = ExpertProfile.objects.select_for_update().get(user=actor)
    if not expert.active:
        raise PermissionDenied("پروفایل کارشناسی شما فعال نیست.")
    amount = _money(amount)
    if amount <= 0:
        raise ValidationError("مبلغ برداشت باید مثبت باشد.")
    if amount > expert.wallet_balance:
        raise ValidationError("مبلغ برداشت بیشتر از موجودی کیف پول است.")
    iban = normalize_iban(iban)
    if expert.iban != iban:
        expert.iban = iban
        expert.save(update_fields=["iban"])
    # The amount leaves the balance immediately and waits as "in progress" until staff confirm the transfer.
    _wallet_entry(expert, "withdrawal", amount, actor, note="درخواست برداشت")
    withdrawal = WithdrawalRequest.objects.create(expert=expert, amount=amount, iban=iban)
    audit(actor, "wallet.withdraw_request", withdrawal, after={"amount": str(amount)})
    return withdrawal


@transaction.atomic
def decide_withdrawal(actor, withdrawal, paid=True, reference=""):
    from .models import WithdrawalRequest
    actor = _permission(actor, "manage_billing")
    withdrawal = WithdrawalRequest.objects.select_for_update().get(pk=getattr(withdrawal, "pk", withdrawal))
    if withdrawal.status != "pending":
        raise ValidationError("این درخواست قبلاً بررسی شده است.")
    if not paid and not str(reference).strip():
        raise ValidationError("دلیل رد درخواست را بنویسید.")
    expert = ExpertProfile.objects.select_for_update().get(pk=withdrawal.expert_id)
    if not paid:
        _wallet_entry(expert, "refund", withdrawal.amount, actor, note="بازگشت درخواست برداشت ردشده")
    withdrawal.status = "paid" if paid else "rejected"
    withdrawal.reference = str(reference).strip()[:120]
    withdrawal.decided_by, withdrawal.decided_at = actor, timezone.now()
    withdrawal.save(update_fields=["status", "reference", "decided_by", "decided_at"])
    audit(actor, "wallet.withdraw_" + withdrawal.status, withdrawal, after={"amount": str(withdrawal.amount), "test": True})
    return withdrawal


def add_favorite(actor, listing, remove=False):
    actor = require_entitled(actor)
    if remove:
        Favorite.objects.filter(user=actor, listing=listing).delete()
        return None
    if not visible_listings().filter(pk=listing.pk).exists():
        raise PermissionDenied("این آگهی در بازار در دسترس نیست.")
    return Favorite.objects.get_or_create(user=actor, listing=listing)[0]


def compare_listings(actor, ids):
    require_entitled(actor)
    try:
        ids = list(dict.fromkeys(int(getattr(value, "pk", value)) for value in ids))
    except (TypeError, ValueError):
        raise ValidationError("شناسه مقایسه معتبر نیست.")
    if len(ids) > 3:
        raise ValidationError("حداکثر سه آگهی قابل مقایسه است.")
    values = {obj.pk: obj for obj in visible_listings().filter(pk__in=ids)}
    if len(values) != len(ids):
        raise PermissionDenied("یک یا چند آگهی دیگر در دسترس نیست.")
    return [values[pk] for pk in ids]


@transaction.atomic
def create_ticket(actor, subject, body):
    actor = _account(actor)
    if not str(subject).strip() or not str(body).strip() or len(subject) > 180 or len(body) > 10000:
        raise ValidationError("عنوان و متن معتبر برای تیکت وارد کنید.")
    ticket = Ticket.objects.create(owner=actor, subject=subject.strip())
    TicketMessage.objects.create(ticket=ticket, author=actor, body=body.strip())
    return ticket


@transaction.atomic
def reply_ticket(actor, ticket, body):
    actor = _account(actor)
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if actor.pk != ticket.owner_id:
        _permission(actor, "support_tickets")
    if not str(body).strip() or len(body) > 10000:
        raise ValidationError("متن پاسخ معتبر نیست.")
    message = TicketMessage.objects.create(ticket=ticket, author=actor, body=body.strip())
    ticket.status = "open" if actor.pk == ticket.owner_id else "answered"
    ticket.save(update_fields=["status", "updated_at"])
    return message


def _mileage_bounds(mileage):
    return (int((Decimal(mileage) * Decimal("0.8")).to_integral_value(rounding=ROUND_CEILING)),
            int(Decimal(mileage) * Decimal("1.2")))


def market_position(listing):
    return market_positions([listing])[listing.pk]


def market_positions(listings):
    """Market position for many listings with one sample query (dashboard pages)."""
    listings = list(listings)
    if not listings:
        return {}
    pairs = Q()
    for brand, model in {(row.brand, row.model) for row in listings}:
        pairs |= Q(brand__iexact=brand, model__iexact=model)
    candidates = list(visible_listings().filter(pairs).filter(
        category_id__in={row.category_id for row in listings},
        year__gte=max(1300, min(row.year for row in listings) - 1),
        year__lte=max(row.year for row in listings) + 1,
    ).order_by("price").values_list("pk", "category_id", "brand", "model", "year", "mileage", "price"))
    results = {}
    for listing in listings:
        low_mileage, high_mileage = _mileage_bounds(listing.mileage)
        brand, model = listing.brand.upper(), listing.model.upper()
        samples = [price for pk, category_id, row_brand, row_model, year, mileage, price in candidates
                   if pk != listing.pk and category_id == listing.category_id
                   and row_brand.upper() == brand and row_model.upper() == model
                   and max(1300, listing.year - 1) <= year <= listing.year + 1
                   and low_mileage <= mileage <= high_mileage]
        results[listing.pk] = _position(listing, samples)
    return results


def _position(listing, samples):
    result = {"count": len(samples), "sample_count": len(samples), "q25": None, "q75": None,
              "low": None, "high": None, "status": "insufficient", "label": "داده کافی نیست"}
    if len(samples) < 5:
        return result
    def percentile(p):
        index = Decimal(len(samples) - 1) * p
        lower = int(index)
        fraction = index - lower
        return samples[lower] + fraction * (samples[min(lower + 1, len(samples) - 1)] - samples[lower])
    low, high = percentile(Decimal("0.25")), percentile(Decimal("0.75"))
    status = "below" if listing.price < low else "above" if listing.price > high else "within"
    result.update(q25=low, q75=high, low=low, high=high, status=status,
                  label={"below": "پایین‌تر از بازه آگهی‌های مشابه", "above": "بالاتر از بازه آگهی‌های مشابه", "within": "در بازه آگهی‌های مشابه"}[status])
    return result


def record_view(actor, listing=None, page=None):
    actor = require_entitled(actor)
    if (listing is None) == (page is None):
        raise ValidationError("دقیقاً یک هدف برای بازدید مشخص کنید.")
    if listing:
        if not visible_listings(statuses=("active", "sold")).filter(pk=listing.pk).exists():
            raise PermissionDenied("آگهی در دسترس نیست.")
        if listing.owner_id == actor.pk:
            return None
        return ViewEvent.objects.get_or_create(user=actor, listing=listing, day=timezone.localdate())[0]
    if page.moderation_status != "approved" or not page.user.is_entitled:
        raise PermissionDenied("صفحه در دسترس نیست.")
    if page.user_id == actor.pk:
        return None
    return ViewEvent.objects.get_or_create(user=actor, page=page, day=timezone.localdate())[0]
