"""Persian web endpoints. Object mutation is delegated to locked domain services."""
from functools import wraps
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, F
from django.db.models.functions import TruncDate
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_http_methods

from . import forms, services, security, site_images
from .rls import as_system, system_context
from .jalali import format_jalali, to_jalali
from .models import (Account, Category, ExpertProfile, Favorite, InspectionReport,
    InspectionRequest, Listing, ListingImage, OutboxMessage, Payment, Plan, Province, SellerPage,
    SlugHistory, Ticket, ViewEvent)


def _form_error(form, exc):
    if isinstance(exc, ValidationError):
        form.add_error(None, "؛ ".join(exc.messages))
    else:
        form.add_error(None, "این اطلاعات هم‌زمان تغییر کرده یا قبلاً استفاده شده است؛ دوباره بررسی کنید.")


def _positive_id(value):
    """Accept bounded decimal IDs; Unicode digit lookalikes must not cause a 500."""
    text = str(value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if text.isascii() and text.isdecimal() and len(text) <= 19:
        number = int(text)
        if 0 < number <= 9223372036854775807:
            return number
    return None


def _return_to(request, default="market:home"):
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
        return redirect(target)
    return redirect(default)


def _generic(request, title, form, **context):
    return render(request, "market/form.html", {"title": title, "form": form, "button_label": "ذخیره", **context})


def entitled(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.user.is_restricted:
            return render(request, "market/access.html", {"title": "حساب شما موقتاً محدود است", "reason": request.user.restriction_reason, "restricted_until": request.user.restricted_until}, status=403)
        if not request.user.is_active:
            raise PermissionDenied
        if request.user.role not in {"staff", "expert"} and not request.user.is_staff:
            if not request.user.is_verified:
                messages.info(request, "برای دسترسی به بازار، تأیید تماس و هویت را تکمیل کنید.")
                return redirect("market:verify")
            if not request.user.is_entitled:
                messages.info(request, "برای مشاهده آگهی‌ها اشتراک فعال لازم است.")
                return redirect("market:subscriptions")
        return view(request, *args, **kwargs)
    return wrapped


def _listing_for_actor(request, pk, include_owned=True):
    if include_owned:
        owned = Listing.objects.filter(pk=pk, owner=request.user)
        if not request.user.is_restricted and request.user.has_perm("market.moderate_listing"):
            owned = Listing.objects.filter(pk=pk)
        if owned.exists():
            return owned.select_related("owner", "owner__page", "province", "category").get()
    qs = services.visible_listings(request.user, statuses=("active", "sold"))
    return get_object_or_404(qs.select_related("owner", "owner__page", "province", "category"), pk=pk)


def _own_listing(request, pk):
    if request.user.is_restricted or not request.user.is_active:
        raise PermissionDenied
    qs = Listing.objects.all() if request.user.has_perm("market.moderate_listing") else Listing.objects.filter(owner=request.user)
    return get_object_or_404(qs, pk=pk)


def _decorate(request, listings):
    listings = list(listings)
    ids = {row.pk for row in listings}
    favorites = set(Favorite.objects.filter(user=request.user, listing_id__in=ids).values_list("listing_id", flat=True)) if ids and request.user.is_authenticated else set()
    selected = set(_compare_ids(request))
    reports = {}
    _attach_images(listings)
    # Final appraisals and view totals are public figures; the rows behind them are private.
    with system_context():
        if ids:
            for report in InspectionReport.objects.filter(inspection__listing_id__in=ids, status='final').select_related('inspection').order_by('-finalized_at', '-pk'):
                reports.setdefault(report.inspection.listing_id, report)
        views = dict(ViewEvent.objects.filter(listing_id__in=ids).values("listing_id").annotate(total=Count("pk")).values_list("listing_id", "total")) if ids else {}
    for row in listings:
        row.view_total = views.get(row.pk, 0)
        row.is_favorited = row.pk in favorites
        row.in_compare = row.pk in selected
        row._final_report_snapshot = reports.get(row.pk)
    return listings


def _attach_images(listings):
    """Set each listing's cover image URL with one query instead of one per card."""
    ids = {row.pk for row in listings}
    images = {}
    if ids:
        for image in ListingImage.objects.filter(listing_id__in=ids, approved=True).order_by('position', 'pk'):
            images.setdefault(image.listing_id, image.image.url)
    for row in listings:
        # Listings without an approved photo show a stable picture from the licensed site pool.
        row._image_url_snapshot = images.get(row.pk) or site_images.listing_placeholder(row.pk)


def _compare_ids(request):
    raw = request.session.get("compare_ids", [])
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(x for x in raw if type(x) is int and 0 < x <= 9223372036854775807))[:3]


# Line icon (includes/icon.html via the glyph filter) shown on each root category tile.
CATEGORY_EMOJI = {"truck": "truck", "tractor": "lorry", "trailer": "package", "carrier": "pickup"}


def landing(request):
    if request.user.is_authenticated:
        return redirect("market:home")
    visible = services.visible_listings()
    roots = list(Category.objects.filter(parent=None).order_by("pk"))
    by_root = {}
    for category_id, parent_id in visible.values_list("category_id", "category__parent_id"):
        by_root[parent_id or category_id] = by_root.get(parent_id or category_id, 0) + 1
    stats = {"listings": visible.count(),
             "sellers": SellerPage.objects.filter(moderation_status="approved", user__is_active=True).count(),
             "experts": ExpertProfile.objects.filter(active=True).count(),
             "provinces": Province.objects.count()}
    return render(request, "market/landing.html", {"plans": Plan.objects.filter(active=True),
        "category_tiles": [{"category": c, "count": by_root.get(c.pk, 0), "emoji": CATEGORY_EMOJI.get(c.slug, "truck")} for c in roots],
        "stats": stats, "provinces": Province.objects.all()})


@never_cache
@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("market:dashboard")
    initial_role = request.GET.get("role", "buyer")
    form = forms.RegistrationForm(request.POST or None, request.FILES or None, initial={"role": initial_role if initial_role in {"buyer", "pro", "plus"} else "buyer"})
    if request.method == "POST" and form.is_valid():
        if not security.throttle(f"register:{request.META.get('REMOTE_ADDR', '')}", limit=10, window=3600):
            form.add_error(None, "تعداد درخواست‌ها زیاد است؛ کمی بعد دوباره تلاش کنید.")
        else:
            try:
                user = security.register_account(form.cleaned_data)
                security.establish_session(request, user)
                messages.success(request, "حساب ساخته شد. تأیید ایمیل و شماره همراه را تکمیل کنید.")
                return redirect("market:verify")
            except (ValidationError, IntegrityError) as exc:
                _form_error(form, exc)
    return render(request, "market/register.html", {"form": form})


@never_cache
@require_http_methods(["GET"])
@as_system  # deliberately reports whether a name is taken; rate limited below
def username_check(request):
    """Live sign-up feedback; rate limited because it reveals whether a name is taken."""
    username = request.GET.get("username", "").strip()
    if not security.throttle(f"username-check:{request.META.get('REMOTE_ADDR', '')}", limit=60, window=300):
        return JsonResponse({"ok": False, "message": "درخواست‌ها زیاد است؛ کمی بعد دوباره امتحان کنید."}, status=429)
    if not username:
        return JsonResponse({"ok": False, "message": "نام کاربری را وارد کنید."})
    if len(username) > 150 or not username.isascii() or not username.isalpha():
        return JsonResponse({"ok": False, "message": "فقط حروف انگلیسی (بدون عدد، فاصله یا نماد) مجاز است."})
    if Account.objects.filter(username__iexact=username).exists():
        return JsonResponse({"ok": False, "message": "این نام کاربری قبلاً ثبت شده است."})
    return JsonResponse({"ok": True, "message": "این نام کاربری آزاد است."})


@never_cache
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("market:dashboard")
    form = forms.LoginForm(request.POST or None)
    if request.method == "POST":
        allowed = security.throttle(f"login:{request.META.get('REMOTE_ADDR', '')}", limit=10, window=300)
        if not allowed:
            form.add_error(None, "تعداد تلاش‌های ورود زیاد است؛ پنج دقیقه دیگر تلاش کنید.")
        elif form.is_valid():
            user = authenticate(request, username=form.cleaned_data["username"], password=form.cleaned_data["password"])
            if user is None:
                form.add_error(None, "نام کاربری یا رمز عبور صحیح نیست یا حساب غیرفعال است.")
            elif user.mfa_secret_encrypted:
                request.session.cycle_key()
                request.session["pending_user_id"] = user.pk
                request.session["pending_login_at"] = timezone.now().timestamp()
                request.session["pending_session_version"] = user.session_version
                return redirect("market:mfa_challenge")
            else:
                security.establish_session(request, user)
                security.notify_login(request, user)
                return redirect("market:inspections" if user.role == "expert" and not user.is_staff else "market:home")
    return render(request, "market/login.html", {"form": form})


@login_required
@require_POST
def logout_view(request):
    logout(request)
    return redirect("market:landing")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def verify(request):
    form = forms.OTPForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        channel = form.cleaned_data["channel"]
        try:
            if request.POST.get("action") == "send":
                security.issue_otp(request.user, channel)
                messages.success(request, "کد در صندوق پیام‌های آزمایشی شما ثبت شد؛ پیام واقعی ارسال نمی‌شود.")
                return redirect("market:verify")
            elif form.cleaned_data.get("code") and security.consume_otp(request.user, channel, form.cleaned_data["code"]):
                messages.success(request, "تأیید با موفقیت انجام شد.")
                return redirect("market:verify")
            else:
                form.add_error("code", "کد نامعتبر یا منقضی است.")
        except ValidationError as exc:
            _form_error(form, exc)
    return render(request, "market/verify.html", {"form": form})


def _filtered(request, qs, *, location=True):
    if location:
        province = request.GET.get("province")
        if province is None:
            province = request.session.get("province_id") or request.user.province_id
            if not province:
                province = Province.objects.filter(name="تهران").values_list("pk", flat=True).first()
        province_id = _positive_id(province)
        if province == "all":
            if request.session.get("province_id") != "all":
                request.session["province_id"] = "all"
        elif province_id and Province.objects.filter(pk=province_id).exists():
            # Re-assigning an unchanged value would rewrite the session row on every page view.
            if request.session.get("province_id") != province_id:
                request.session["province_id"] = province_id
            qs = qs.filter(province_id=province_id)
    query = request.GET.get("q", "").strip()[:150]
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(brand__icontains=query) | Q(model__icontains=query) | Q(owner__page__slug__icontains=query) | Q(owner__page__display_name__icontains=query))
    category = request.GET.get("category", "")
    if category:
        item = Category.objects.filter(Q(pk=_positive_id(category)) if _positive_id(category) else Q(slug=category[:80])).first()
        if item is not None:
            qs = qs.filter(Q(category=item) | Q(category__parent=item))
        else:
            qs = qs.none()
    brand = request.GET.get("brand", "")[:80]
    if brand:
        qs = qs.filter(brand__iexact=brand)
    for key, lookup in (("min_price", "price__gte"), ("max_price", "price__lte")):
        value = request.GET.get(key, "")
        if value:
            try:
                number = Decimal(value)
                if number.is_finite() and 0 <= number <= Decimal("999999999999999999"):
                    qs = qs.filter(**{lookup: number})
            except (InvalidOperation, ValueError):
                pass
    if request.GET.get("inspected") or request.GET.get("expert") or request.GET.get("sort") == "inspected":
        with system_context():
            inspected = list(InspectionReport.objects.filter(status="final").values_list("inspection__listing_id", flat=True).distinct())
        qs = qs.filter(pk__in=inspected)
    sort = request.GET.get("sort", "newest")
    order = {"newest": "-created_at", "cheapest": "price", "expensive": "-price", "price_asc": "price", "price_desc": "-price"}.get(sort, "-created_at")
    return qs.order_by(order, "-pk").distinct()


@entitled
def home(request):
    visible = services.visible_listings(request.user)
    qs = _filtered(request, visible)
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    page.object_list = _decorate(request, page.object_list)
    sellers = SellerPage.objects.filter(moderation_status="approved", user__is_active=True,
        user__identity_status="approved", user__email_verified=True, user__mobile_verified=True,
        user__subscription_until__gt=timezone.now()).filter(Q(user__restricted_until__isnull=True) | Q(user__restricted_until__lte=timezone.now())).select_related("user", "user__province")
    selected_province = request.session.get("province_id")
    if isinstance(selected_province, int):
        sellers = sellers.filter(user__province_id=selected_province)
    if request.GET.get("q"):
        sellers = sellers.filter(Q(slug__icontains=request.GET["q"][:150]) | Q(display_name__icontains=request.GET["q"][:150]))
    compare_ids = _compare_ids(request)
    seller_mode = request.GET.get("view") == "sellers"
    if seller_mode:
        page = Paginator(sellers.order_by("-created_at", "-pk"), 12).get_page(request.GET.get("page"))
        sellers = page.object_list
    query = request.GET.copy()
    query.pop("page", None)
    category_query = query.copy()
    category_query.pop("category", None)
    return render(request, "market/home.html", {"listings": [] if seller_mode else page.object_list, "page_obj": page, "categories": Category.objects.filter(parent=None), "provinces": Province.objects.all(), "brands": visible.order_by("brand").values_list("brand", flat=True).distinct(), "sellers": sellers, "seller_mode": seller_mode, "query_string": query.urlencode(), "category_query": category_query.urlencode(), "selected_province": str(request.session.get("province_id", "")), "result_count": page.paginator.count, "compare_listings": _decorate(request, visible.filter(pk__in=compare_ids)) if compare_ids else []})


@login_required
def listing_detail(request, pk):
    listing = _listing_for_actor(request, pk)
    if listing.owner_id != request.user.pk:
        services.require_entitled(request.user)
        ViewEvent.objects.get_or_create(user=request.user, listing=listing, day=timezone.localdate())
    _decorate(request, [listing])
    try:
        services.require_entitled(request.user)
        eligible = True
    except PermissionDenied:
        eligible = False
    visible = services.visible_listings(statuses=("active", "sold")).filter(pk=listing.pk).exists()
    return render(request, "market/detail.html", {"listing": listing, "seller": getattr(listing.owner, "page", None), "report": listing.final_report, "market_position": services.market_position(listing), "price_history": listing.price_history.all() if listing.owner_id == request.user.pk else [], "can_edit": not request.user.is_restricted and (listing.owner_id == request.user.pk or request.user.has_perm("market.moderate_listing")), "can_interact": eligible and visible, "can_request_inspection": eligible and listing.status == "active" and (visible or listing.owner_id == request.user.pk)})


@entitled
def seller_page(request, slug):
    seller = SellerPage.objects.filter(slug__iexact=slug).select_related("user", "user__province").first()
    if seller is None:
        old = get_object_or_404(SlugHistory.objects.select_related("page"), slug__iexact=slug)
        return redirect("market:seller_page", slug=old.page.slug, permanent=True)
    if seller.user_id != request.user.pk and not request.user.has_perm("market.moderate_listing"):
        if seller.moderation_status != "approved" or not seller.user.is_entitled:
            raise Http404
        ViewEvent.objects.get_or_create(user=request.user, page=seller, day=timezone.localdate())
    statuses = ("sold",) if request.GET.get("status") == "sold" else ("active",)
    qs = _filtered(request, services.visible_listings(request.user, statuses=statuses).filter(owner=seller.user), location=False)
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    page.object_list = _decorate(request, page.object_list)
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "market/seller_page.html", {"seller": seller, "listings": page.object_list, "page_obj": page, "categories": Category.objects.filter(parent=None), "query_string": query.urlencode()})


@login_required
def dashboard(request):
    if request.user.role == "expert" and not request.user.is_staff:
        return redirect("market:inspections")
    if request.user.is_staff and request.user.is_active and not request.user.is_restricted:
        # Managers have no listings of their own; their overview is the management dashboard.
        return redirect("market:staff")
    if not request.user.is_seller:
        return render(request, "market/dashboard.html", {"stats": {"days_remaining": request.user.days_remaining}})
    qs = request.user.listings.exclude(status="deleted").select_related("category", "province").annotate(view_count=Count("views", distinct=True), favorite_count=Count("favorites", distinct=True)).order_by("-created_at", "-pk")
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    stats = {"listing_views": ViewEvent.objects.filter(listing__owner=request.user).count(), "page_views": ViewEvent.objects.filter(page__user=request.user).count(), "favorites": Favorite.objects.filter(listing__owner=request.user).count(), "active_listings": qs.filter(status="active", moderation_status="approved").count(), "days_remaining": request.user.days_remaining}
    page.object_list = list(page.object_list)
    _attach_images(page.object_list)
    positions = services.market_positions(page.object_list)
    for listing in page.object_list:
        listing.market_position = positions[listing.pk]
    today = timezone.localdate()
    start = today - timedelta(days=6)
    listing_views = dict(ViewEvent.objects.filter(listing__owner=request.user, day__gte=start).values("day").annotate(total=Count("pk")).values_list("day", "total"))
    page_views = dict(ViewEvent.objects.filter(page__user=request.user, day__gte=start).values("day").annotate(total=Count("pk")).values_list("day", "total"))
    favorite_counts = dict(Favorite.objects.filter(listing__owner=request.user, created_at__date__gte=start).annotate(day=TruncDate("created_at")).values("day").annotate(total=Count("pk")).values_list("day", "total"))
    activity_series = []
    for day in (start + timedelta(days=offset) for offset in range(7)):
        jy, jm, jd = to_jalali(day)
        activity_series.append({"date": day, "label": f"{jm}/{jd}", "jalali": f"{jy}/{jm:02d}/{jd:02d}", "views": listing_views.get(day, 0), "page_views": page_views.get(day, 0), "favorites": favorite_counts.get(day, 0)})
    # An even maximum keeps the middle grid line on a whole number.
    activity_max = max(views for point in activity_series for views in (point["views"], point["page_views"]))
    activity_max = max(2, activity_max + activity_max % 2)
    return render(request, "market/dashboard.html", {"listings": page.object_list, "page_obj": page, "stats": stats, "quotas": services.quota_usage(request.user) if request.user.is_seller else [], "status_choices": Listing.Status.choices, "activity_series": activity_series, "activity_max": activity_max, "activity_half": activity_max // 2})


@entitled
@require_http_methods(["GET", "POST"])
def listing_create(request):
    if not request.user.is_seller:
        raise PermissionDenied
    form = forms.ListingForm(request.POST or None, request.FILES or None, initial={"province": request.user.province_id, "city": request.user.city}, allow_video=request.user.role == "pro")
    if request.method == "POST" and form.is_valid():
        try:
            services.create_listing(request.user, dict(form.cleaned_data))
            messages.success(request, "آگهی برای بررسی مدیر ارسال شد.")
            return redirect("market:dashboard")
        except (ValidationError, IntegrityError) as exc:
            _form_error(form, exc)
    return render(request, "market/listing_form.html", {"form": form, "title": "ثبت آگهی جدید"})


@login_required
@require_http_methods(["GET", "POST"])
def listing_edit(request, pk):
    listing = _own_listing(request, pk)
    form = forms.ListingForm(request.POST or None, request.FILES or None, instance=listing, allow_video=listing.owner.role == "pro")
    if request.method == "POST" and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            data["remove_images"] = request.POST.getlist("remove_image")
            data["remove_videos"] = request.POST.getlist("remove_video")
            services.submit_listing_edit(request.user, Listing.objects.get(pk=pk), data)
            messages.success(request, "ویرایش برای بررسی ارسال شد؛ نسخه تأییدشده قبلی تا تصمیم مدیر باقی می‌ماند.")
            return redirect("market:dashboard")
        except (ValidationError, IntegrityError) as exc:
            _form_error(form, exc)
    return render(request, "market/listing_form.html", {"form": form, "listing": listing, "title": "ویرایش آگهی",
        "existing_images": listing.images.all(), "existing_videos": listing.videos.all()})


@login_required
@require_POST
def listing_status(request, pk):
    listing = _own_listing(request, pk)
    form = forms.ListingStatusForm(request.POST)
    if form.is_valid():
        try:
            services.set_listing_status(request.user, listing, form.cleaned_data["status"])
            messages.success(request, "وضعیت آگهی به‌روز شد.")
        except ValidationError as exc:
            messages.error(request, "؛ ".join(exc.messages))
    else:
        messages.error(request, "وضعیت انتخاب‌شده معتبر نیست.")
    return redirect("market:dashboard")


@login_required
@require_http_methods(["GET", "POST"])
def listing_price(request, pk):
    listing = _own_listing(request, pk)
    form = forms.PriceForm(request.POST or None, initial={"price": listing.price})
    if request.method == "POST" and form.is_valid():
        try:
            services.update_price(request.user, listing, form.cleaned_data["price"])
            messages.success(request, "قیمت جدید با ثبت تاریخچه اعمال شد.")
            return redirect("market:dashboard")
        except ValidationError as exc:
            _form_error(form, exc)
    return _generic(request, "به‌روزرسانی قیمت پیشنهادی", form, listing=listing, emoji="money-bag")


@entitled
def favorites(request):
    qs = services.visible_listings(request.user).filter(favorites__user=request.user)
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    page.object_list = _decorate(request, page.object_list)
    return render(request, "market/favorites.html", {"listings": page.object_list, "page_obj": page})


@entitled
@require_POST
def favorite_toggle(request, pk):
    listing = get_object_or_404(services.visible_listings(request.user, statuses=("active", "sold")), pk=pk)
    with transaction.atomic():
        Account.objects.select_for_update().get(pk=request.user.pk)
        existing = Favorite.objects.filter(user=request.user, listing=listing)
        favorited = not existing.exists()
        if favorited:
            Favorite.objects.create(user=request.user, listing=listing)
        else:
            existing.delete()
    message = "آگهی به علاقه‌مندی‌ها اضافه شد." if favorited else "آگهی از علاقه‌مندی‌ها حذف شد."
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"favorited": favorited, "message": message})
    messages.success(request, message)
    return _return_to(request)


@entitled
def compare(request):
    selected = _compare_ids(request)
    listing_map = {row.pk: row for row in services.visible_listings(request.user, statuses=("active", "sold")).filter(pk__in=selected)}
    request.session["compare_ids"] = [pk for pk in selected if pk in listing_map]
    listings = _decorate(request, [listing_map[pk] for pk in selected if pk in listing_map])
    best = {}
    if len(listings) > 1:
        best = {
            "price": min(listings, key=lambda row: row.price).pk,
            "year": max(listings, key=lambda row: row.year).pk,
            "mileage": min(listings, key=lambda row: row.mileage).pk,
        }
    return render(request, "market/compare.html", {"listings": listings, "best": best})


@entitled
@require_POST
def compare_toggle(request, pk):
    ids = _compare_ids(request)
    visible_ids = set(services.visible_listings(request.user, statuses=("active", "sold")).filter(pk__in=ids).values_list("pk", flat=True))
    ids = [selected for selected in ids if selected in visible_ids]
    if pk in ids:
        ids.remove(pk)
        messages.success(request, "آگهی از مقایسه حذف شد.")
    else:
        get_object_or_404(services.visible_listings(request.user, statuses=("active", "sold")), pk=pk)
        if len(ids) >= 3:
            messages.error(request, "حداکثر سه آگهی را می‌توان هم‌زمان مقایسه کرد؛ ابتدا یک مورد را حذف کنید.")
        else:
            ids.append(pk)
            messages.success(request, "آگهی به مقایسه اضافه شد.")
    request.session["compare_ids"] = ids
    return _return_to(request, "market:compare")


@login_required
@require_http_methods(["GET", "POST"])
def subscriptions(request):
    plans = Plan.objects.filter(active=True)
    if request.method == "POST":
        plan = get_object_or_404(plans, pk=_positive_id(request.POST.get("plan")) or 0)
        try:
            payment = services.create_subscription_payment(request.user, plan)
            return redirect("market:checkout", pk=payment.pk)
        except ValidationError as exc:
            messages.error(request, "؛ ".join(exc.messages))
    return render(request, "market/plans.html", {"plans": plans})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def checkout(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("plan", "inspection"), pk=pk, user=request.user)
    if request.method == "POST":
        from .providers import simulate_payment
        outcome = request.POST.get("outcome", "")
        if outcome not in {"success", "failure", "delay", "delayed"}:
            messages.error(request, "نتیجه پرداخت آزمایشی معتبر نیست.")
        else:
            try:
                payment = simulate_payment(request.user, payment, "delayed" if outcome == "delay" else outcome)
                messages.info(request, "نتیجه پرداخت آزمایشی ثبت شد؛ هیچ مبلغ واقعی جابه‌جا نشد.")
                return redirect("market:checkout", pk=pk)
            except ValidationError as exc:
                messages.error(request, "؛ ".join(exc.messages))
    return render(request, "market/checkout.html", {"payment": payment})


def _inspections_for(user):
    qs = InspectionRequest.objects.select_related("listing", "requester", "expert__user")
    if not user.is_active:
        return qs.none()
    if user.is_restricted:
        return qs.filter(Q(requester=user) | Q(listing__owner=user)).distinct()
    if user.has_perm("market.finalize_report"):
        return qs
    return qs.filter(Q(requester=user) | Q(listing__owner=user) |
        Q(expert__user=user, expert__active=True, expert__user__role="expert")).distinct()


def _can_submit_report(user, inspection):
    if not user.is_active or user.is_restricted:
        return False
    return user.has_perm("market.finalize_report") or bool(
        user.role == "expert" and inspection.expert_id and inspection.expert.active
        and inspection.expert.user_id == user.pk)


EXPERT_OPEN = ("assigned",)


@login_required
def inspections(request):
    qs = _inspections_for(request.user).order_by("-created_at")
    history = request.GET.get("view") == "history"
    expert_view = request.user.role == "expert" and not request.user.is_staff
    if expert_view:
        # Experts see only work assigned to them: open requests, or the history of finished ones.
        qs = qs.filter(expert__user=request.user)
        qs = qs.exclude(status__in=EXPERT_OPEN) if history else qs.filter(status__in=EXPERT_OPEN)
    return render(request, "market/inspection_list.html", {"inspections": qs, "expert_view": expert_view, "history": history})


@entitled
@require_http_methods(["GET", "POST"])
def inspection_create(request, pk):
    listing = _listing_for_actor(request, pk)
    form = forms.InspectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                inspection = services.request_inspection(request.user, listing, note=form.cleaned_data["coordination_note"])
                payment = services.create_payment(request.user, "inspection", inspection=inspection)
            return redirect("market:checkout", pk=payment.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _generic(request, "درخواست کارشناسی", form, listing=listing, emoji="shield", button_label="ثبت درخواست و پرداخت آزمایشی")


@login_required
def inspection_detail(request, pk):
    inspection = get_object_or_404(_inspections_for(request.user), pk=pk)
    report = getattr(inspection, "report", None)
    can_report = _can_submit_report(request.user, inspection)
    if report and report.status != "final" and not can_report:
        report = None
    payment = inspection.payments.filter(user=request.user).order_by("-pk").first()
    return render(request, "market/inspection_detail.html", {"inspection": inspection, "report": report, "payment": payment, "can_report": can_report and inspection.status in {"assigned", "reported"}})


@login_required
@require_http_methods(["GET", "POST"])
def report_create(request, pk):
    if request.user.is_restricted:
        raise PermissionDenied
    inspection = get_object_or_404(_inspections_for(request.user), pk=pk)
    if not _can_submit_report(request.user, inspection):
        raise PermissionDenied
    report = getattr(inspection, "report", None)
    if report and report.status == "final":
        raise PermissionDenied("گزارش نهایی قابل تغییر نیست.")
    initial = {"price": report.price, "summary": report.summary} if report else {}
    form = forms.ReportForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            services.submit_report(request.user, inspection, form.cleaned_data)
            messages.success(request, "گزارش برای تأیید نهایی مدیر ارسال شد.")
            return redirect("market:inspection_detail", pk=pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _generic(request, "ثبت گزارش کارشناسی", form, inspection=inspection, emoji="clipboard")


def _tickets_for(user):
    return Ticket.objects.all() if user.is_active and not user.is_restricted and user.has_perm("market.support_tickets") else Ticket.objects.filter(owner=user)


@login_required
def tickets(request):
    return render(request, "market/tickets.html", {"tickets": _tickets_for(request.user)})


@login_required
@require_http_methods(["GET", "POST"])
def ticket_create(request):
    form = forms.TicketForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            ticket = services.create_ticket(request.user, **form.cleaned_data)
            messages.success(request, "تیکت پشتیبانی ثبت شد.")
            return redirect("market:ticket_detail", pk=ticket.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _generic(request, "تیکت جدید", form, button_label="ارسال تیکت", emoji="chat")


@login_required
@require_http_methods(["GET", "POST"])
def ticket_detail(request, pk):
    ticket = get_object_or_404(_tickets_for(request.user), pk=pk)
    form = forms.ReplyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.reply_ticket(request.user, ticket, form.cleaned_data["body"])
            messages.success(request, "پیام ارسال شد.")
            return redirect("market:ticket_detail", pk=pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return render(request, "market/ticket_detail.html", {"ticket": ticket, "thread_messages": ticket.messages.select_related("author"), "form": form})


def _compare_profile(account):
    rows = []
    for row in _describe_pending({k: v for k, v in account.pending_profile.items() if k in services.PROFILE_LABELS}):
        key = row["key"]
        if key == "province":
            current = account.province.name if account.province else "—"
        elif key == "birth_date":
            from .jalali import format_jalali
            current = format_jalali(account.birth_date)
        elif key in services.PROFILE_FILES:
            current = "فایل فعلی" if getattr(account, key) else "—"
        else:
            current = getattr(account, key) or "—"
        rows.append({**row, "current": current})
    return rows


def _describe_pending(pending):
    rows = []
    for key, value in pending.items():
        if key == "province":
            value = Province.objects.filter(pk=value).values_list("name", flat=True).first() or "—"
        elif key == "birth_date":
            from datetime import date
            from .jalali import format_jalali
            value = format_jalali(date.fromisoformat(value))
        elif key in services.PROFILE_FILES:
            value = "فایل جدید بارگذاری شده"
        rows.append({"key": key, "label": services.PROFILE_LABELS[key], "value": value})
    return rows


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def profile(request):
    user = request.user
    initial = {name: getattr(user, name) for name in ("first_name", "last_name", "national_id", "birth_date", "mobile", "email", "province", "city", "username")}
    action = request.POST.get("action", "identity")
    form = forms.ProfileForm(request.POST if request.method == "POST" and action == "identity" else None, request.FILES or None, user=user, initial=initial)
    page = getattr(user, "page", None)
    page_form = forms.SellerPageForm(request.POST if request.method == "POST" and action == "page" else None, request.FILES or None, instance=page) if page else None
    active = page_form if action == "page" else form
    if request.method == "POST" and active is not None and active.is_valid():
        try:
            if action == "page":
                page_data = dict(active.cleaned_data)
                for upload_field in ("avatar", "license_document"):
                    if upload_field not in request.FILES:
                        page_data.pop(upload_field, None)
                services.submit_page_edit(user, SellerPage.objects.get(pk=page.pk), page_data)
                messages.success(request, "ویرایش صفحه برای بررسی مدیر ارسال شد.")
            elif action == "identity":
                was_approved = user.identity_status == "approved"
                updated = services.submit_profile_change(user, active.cleaned_data)
                if was_approved and updated.pending_profile and "rejected_reason" not in updated.pending_profile:
                    messages.success(request, "درخواست تغییر اطلاعات برای مدیر ارسال شد. تا زمان تأیید، اطلاعات فعلی شما معتبر می‌ماند.")
                elif was_approved:
                    messages.info(request, "تغییری در اطلاعات هویتی ثبت نشد.")
                else:
                    messages.success(request, "اطلاعات حساب ذخیره شد و برای بررسی هویت ارسال می‌شود.")
            else:
                raise PermissionDenied
            return redirect("market:profile")
        except (ValidationError, IntegrityError) as exc:
            _form_error(active, exc)
    pending = {key: value for key, value in (user.pending_profile or {}).items() if key in services.PROFILE_LABELS}
    return render(request, "market/profile.html", {"form": form, "page_form": page_form, "seller": page,
        "pending_changes": _describe_pending(pending), "pending_rejected": (user.pending_profile or {}).get("rejected_reason", "")})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def password_change(request):
    form = forms.KamiwaPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = Account.objects.select_for_update().get(pk=request.user.pk)
            user.set_password(form.cleaned_data["new_password1"])
            user.session_version += 1
            user.save(update_fields=["password", "session_version"])
        security.establish_session(request, user)
        messages.success(request, "رمز عبور تغییر کرد و نشست‌های قبلی باطل شدند.")
        return redirect("market:profile")
    return _generic(request, "تغییر رمز عبور", form, emoji="key")


@never_cache
@require_http_methods(["GET", "POST"])
def password_reset(request):
    form = forms.PasswordResetForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if security.throttle(f"reset:{request.META.get('REMOTE_ADDR', '')}", limit=5, window=900):
            security.request_password_reset(form.cleaned_data["username"], form.cleaned_data["email"])
        messages.info(request, "اگر اطلاعات حساب صحیح باشد، کد بازیابی در سرویس ایمیل آزمایشی ثبت می‌شود. در صورت نداشتن دسترسی، با مدیر تماس بگیرید.")
        return redirect("market:password_reset_confirm")
    return _generic(request, "بازیابی رمز عبور", form, button_label="درخواست کد بازیابی", emoji="key")


@never_cache
@require_http_methods(["GET", "POST"])
def password_reset_confirm(request):
    form = forms.PasswordResetConfirmForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        ok = False
        if security.throttle(f"reset-confirm:{request.META.get('REMOTE_ADDR', '')}", limit=5, window=900):
            with transaction.atomic():
                user = Account.objects.select_for_update().filter(username__iexact=form.cleaned_data["username"], email__iexact=form.cleaned_data["email"], email_verified=True, is_active=True).first()
                if user:
                    try:
                        ok = security.reset_password(user, form.cleaned_data["code"], form.cleaned_data["new_password1"])
                    except ValidationError as exc:
                        _form_error(form, exc)
        if ok:
            logout(request)
            messages.success(request, "رمز عبور تغییر کرد؛ با رمز جدید وارد شوید.")
            return redirect("market:login")
        form.add_error(None, "اطلاعات یا کد بازیابی نامعتبر یا منقضی است.")
    return _generic(request, "تعیین رمز عبور جدید", form, emoji="lock")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def mfa_setup(request):
    if request.method == "GET":
        request.session.pop("mfa_setup", None)
        return redirect("market:profile")
    if request.user.mfa_secret_encrypted:
        messages.info(request, "تأیید دومرحله‌ای حساب شما فعال است.")
        return redirect("market:profile")
    if request.POST.get("action") == "start":
        setup = security.begin_mfa_setup(request.user)
        setup.update(user_id=request.user.pk, session_version=request.user.session_version, started_at=timezone.now().timestamp())
        request.session["mfa_setup"] = setup
        form = forms.MFAForm()
    else:
        setup = request.session.get("mfa_setup")
        now = timezone.now().timestamp()
        if not isinstance(setup, dict) or setup.get("user_id") != request.user.pk or setup.get("session_version") != request.user.session_version or not isinstance(setup.get("started_at"), (int, float)) or not 0 <= now - setup["started_at"] <= 600:
            request.session.pop("mfa_setup", None)
            messages.info(request, "درخواست فعال‌سازی منقضی شده است؛ از تنظیمات حساب دوباره شروع کنید.")
            return redirect("market:profile")
        form = forms.MFAForm(request.POST)
    if form.is_bound and form.is_valid():
        if security.confirm_mfa_setup(request.user, setup["secret"], form.cleaned_data["code"]):
            request.session.pop("mfa_setup", None)
            request.session["mfa_verified"] = True
            messages.success(request, "تأیید دومرحله‌ای فعال شد.")
            return redirect("market:dashboard")
        form.add_error("code", "کد نامعتبر است؛ زمان برنامه تأییدکننده را بررسی کنید.")
    return _generic(request, "فعال‌سازی اختیاری تأیید دومرحله‌ای", form, mfa_setup=setup, button_label="تأیید و فعال‌سازی", back_url=reverse("market:profile"), description="این قابلیت اختیاری است. پس از فعال‌سازی، ورود به حساب به کد برنامه تأییدکننده نیاز دارد.")


@never_cache
@require_http_methods(["GET", "POST"])
@as_system  # the account is not signed in yet; only the pending session user is read
def mfa_challenge(request):
    def clear_pending():
        for key in ("pending_user_id", "pending_login_at", "pending_session_version"):
            request.session.pop(key, None)

    if request.user.is_authenticated:
        user = request.user
        if not user.mfa_secret_encrypted:
            return redirect("market:dashboard")
        expected_version = request.session.get("session_version")
    else:
        user_id = request.session.get("pending_user_id")
        started = request.session.get("pending_login_at", 0)
        if not isinstance(started, (float, int)) or timezone.now().timestamp() - started > 300:
            clear_pending()
            return redirect("market:login")
        user = Account.objects.filter(pk=user_id, is_active=True).first() if isinstance(user_id, int) else None
        expected_version = request.session.get("pending_session_version")
    if not user or user.session_version != expected_version:
        clear_pending()
        return redirect("market:login")
    form = forms.MFAForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = Account.objects.select_for_update().get(pk=user.pk)
            if user.session_version != expected_version or not user.is_active:
                clear_pending()
                return redirect("market:login")
            if security.check_mfa(user, form.cleaned_data["code"]):
                clear_pending()
                security.establish_session(request, user)
                request.session["mfa_verified"] = True
                security.notify_login(request, user)
                return redirect("market:dashboard")
        form.add_error("code", "کد تأیید نامعتبر است.")
    return _generic(request, "تأیید دومرحله‌ای ورود", form, button_label="ورود امن")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def admin_email_change(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    form = forms.AdminEmailForm(request.POST or None, mfa_enabled=bool(request.user.mfa_secret_encrypted))
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            if data.get("email_code"):
                if not security.complete_admin_email_change(request.user, data["email"], data["email_code"], data["password"], data.get("mfa_code", "")):
                    raise ValidationError("کد ایمیل نامعتبر است.")
                messages.success(request, "ایمیل مدیر تغییر کرد.")
                return redirect("market:profile")
            security.begin_admin_email_change(request.user, data["email"], data["password"], data.get("mfa_code", ""))
            messages.info(request, "کد تأیید ایمیل جدید در صندوق آزمایشی مدیر ثبت شد.")
            return redirect("market:admin_email_change")
        except PermissionDenied as exc:
            form.add_error(None, str(exc))
        except ValidationError as exc:
            _form_error(form, exc)
    return _generic(request, "تغییر امن ایمیل مدیر", form)


@login_required
@never_cache
def outbox(request, pk=None):
    if not settings.KAMIWA_TEST_PROVIDERS:
        raise Http404
    from datetime import timedelta
    from .providers import read_test_message
    qs = OutboxMessage.objects.filter(created_at__gte=timezone.now() - timedelta(hours=settings.KAMIWA_PRIVATE_MESSAGE_TTL_HOURS)).order_by("-created_at")
    if not request.user.is_superuser:
        qs = qs.filter(target__in=[request.user.email, request.user.mobile]).exclude(target="")
    selected = None
    payload = None
    if pk is not None:
        selected = get_object_or_404(qs, pk=pk)
        payload = read_test_message(request.user, selected)
    items = []
    for item in qs[:50]:
        try:
            read_test_message(request.user, item)
            items.append(item)
        except (PermissionDenied, ValidationError):
            continue
    return render(request, "market/outbox.html", {"outbox_messages": items, "selected_message": selected, "payload": payload})


@login_required
@never_cache
def private_file(request, kind, pk):
    from .files import authorized_private_file
    try:
        file = authorized_private_file(request.user, kind, pk)
    except ObjectDoesNotExist:
        raise Http404
    if not file:
        raise Http404
    try:
        response = FileResponse(file.open("rb"), as_attachment=True, filename=Path(file.name).name)
    except FileNotFoundError:
        raise Http404
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def staff(request):
    if not request.user.is_staff or not request.user.is_active or request.user.is_restricted:
        raise PermissionDenied
    if request.method == "POST":
        action = request.POST.get("action", "")
        pk = _positive_id(request.POST.get("pk"))
        if pk is None:
            raise Http404
        approve = request.POST.get("approve") in {"1", "true", "yes", "approve"}
        reason = request.POST.get("reason", "")[:2000]
        try:
            if action == "identity":
                services.approve_identity(request.user, get_object_or_404(Account, pk=pk), approve=approve, reason=reason)
            elif action == "listing":
                services.approve_listing(request.user, get_object_or_404(Listing, pk=pk), approve=approve, reason=reason)
            elif action == "page":
                services.moderate_page(request.user, get_object_or_404(SellerPage, pk=pk), approve=approve, reason=reason)
            elif action == "profile":
                services.review_profile_change(request.user, get_object_or_404(Account, pk=pk), approve=approve, reason=reason)
            elif action == "report":
                services.finalize_report(request.user, get_object_or_404(InspectionReport, pk=pk))
            elif action == "assign":
                expert_id = _positive_id(request.POST.get("expert"))
                if expert_id is None:
                    raise ValidationError("یک کارشناس انتخاب کنید.")
                services.assign_expert(request.user, get_object_or_404(InspectionRequest, pk=pk), get_object_or_404(ExpertProfile, pk=expert_id))
            else:
                raise ValidationError("عملیات معتبر نیست؛ مدیریت سایر تنظیمات از پنل مدیریت انجام می‌شود.")
            messages.success(request, "عملیات ثبت شد.")
        except ValidationError as exc:
            messages.error(request, "؛ ".join(exc.messages))
        return redirect("market:staff")
    context = {}
    can_find_users = request.user.has_perm("market.review_identity") or request.user.has_perm("market.manage_accounts")
    q = request.GET.get("q", "").strip().lstrip("#＃").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))[:150]
    if can_find_users and q:
        code = _positive_id(q)
        matches = Account.objects.filter(Q(internal_code=code) | Q(username__iexact=q) if code else Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
        matches = list(matches.order_by("username")[:50])
        if code and len(matches) == 1 and matches[0].internal_code == code:
            return redirect("market:staff_user", pk=matches[0].pk)
        context["search_results"] = matches
        context["searched"] = True
    if can_find_users:
        online_since = timezone.now() - Account.ONLINE_WINDOW
        context["show_online"] = True
        context["online_users"] = Account.objects.filter(last_seen_at__gte=online_since).order_by("-last_seen_at")[:50]
        context["online_count"] = Account.objects.filter(last_seen_at__gte=online_since).count()
        context["active_today"] = Account.objects.filter(activity_days__day=timezone.localdate()).count()
    if request.user.has_perm("market.review_identity"):
        context["pending_users"] = Account.objects.filter(identity_status="pending", role__in=["buyer", "plus", "pro"])[:100]
        context["profile_requests"] = [
            {"account": account, "rows": _compare_profile(account)}
            for account in Account.objects.exclude(pending_profile={}).exclude(pending_profile__has_key="rejected_reason").order_by("pending_profile_at")[:100]]
    if request.user.has_perm("market.moderate_listing"):
        context["pending_listings"] = Listing.objects.filter(Q(moderation_status="pending") | ~Q(pending_data={})).select_related("owner")[:100]
        context["pending_pages"] = SellerPage.objects.filter(Q(moderation_status="pending") | ~Q(pending_data={})).select_related("user")[:100]
    if request.user.has_perm("market.finalize_report"):
        context["pending_reports"] = InspectionReport.objects.filter(status="pending").select_related("inspection__listing", "expert__user")[:100]
        context["unassigned_inspections"] = InspectionRequest.objects.filter(status="paid", expert=None).select_related("listing__province")[:100]
        context["experts"] = ExpertProfile.objects.filter(active=True).select_related("user", "province")
    if request.user.has_perm("market.support_tickets"):
        context["support_count"] = Ticket.objects.exclude(status="closed").count()
    return render(request, "market/staff.html", context)


SECTION_PERMISSIONS = {
    "review_identity": "بررسی هویت و اطلاعات کاربران",
    "moderate_listing": "بررسی آگهی‌ها و صفحه فروشندگان",
    "finalize_report": "کارشناسی و گزارش کارشناسان",
    "support_tickets": "پشتیبانی و تیکت‌ها",
    "manage_accounts": "محدودسازی و غیرفعال‌سازی حساب‌ها",
    "manage_billing": "تعرفه‌ها و پرداخت‌ها",
}


def _staff_user_allowed(user):
    return (user.is_staff and user.is_active and not user.is_restricted
            and (user.has_perm("market.review_identity") or user.has_perm("market.manage_accounts")))


@login_required
@never_cache
def staff_user(request, pk):
    """One page with everything an authorised admin may see about an account."""
    if not _staff_user_allowed(request.user):
        raise PermissionDenied
    account = get_object_or_404(Account.objects.select_related("province"), pk=pk)
    can_identity = request.user.has_perm("market.review_identity")
    today = timezone.localdate()
    activity = {row.day: row.minutes for row in account.activity_days.filter(day__gte=today - timedelta(days=13))}
    activity_rows = []
    for offset in range(14):
        day = today - timedelta(days=offset)
        jy, jm, jd = to_jalali(day)
        activity_rows.append({"label": f"{jy}/{jm:02d}/{jd:02d}", "minutes": activity.get(day, 0)})
    listings = None
    if request.user.has_perm("market.moderate_listing") or can_identity:
        listings = account.listings.select_related("category", "province").order_by("-created_at")
    services.audit(request.user, "staff.view_account", account, after={"identity": can_identity})
    return render(request, "market/staff_user.html", {
        "account": account, "can_identity": can_identity, "listings": listings,
        "activity_rows": activity_rows, "activity_total": sum(activity.values()),
        "pending_rows": _compare_profile(account) if can_identity and account.pending_profile and "rejected_reason" not in account.pending_profile else [],
        "payments": account.payments.order_by("-created_at")[:20] if request.user.has_perm("market.manage_billing") else None,
        "birth_date_jalali": format_jalali(account.birth_date) if can_identity else "",
    })


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def staff_admins(request):
    """The main administrator assigns section admins and the sections each may manage."""
    if not (request.user.is_superuser and request.user.is_active and not request.user.is_restricted):
        raise PermissionDenied
    from django.contrib.auth.models import Permission
    section_perms = {perm.codename: perm for perm in Permission.objects.filter(content_type__app_label="market", codename__in=SECTION_PERMISSIONS)}
    if request.method == "POST":
        action = request.POST.get("action")
        selected = [code for code in request.POST.getlist("sections") if code in section_perms]
        try:
            with transaction.atomic():
                if action == "grant":
                    raw = request.POST.get("code", "").strip().lstrip("#＃").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
                    account = Account.objects.select_for_update().filter(internal_code=_positive_id(raw)).first() if _positive_id(raw) else None
                    if account is None and _positive_id(request.POST.get("pk")):
                        account = Account.objects.select_for_update().filter(pk=_positive_id(request.POST.get("pk"))).first()
                    if account is None:
                        raise ValidationError("حسابی با این کد پیدا نشد.")
                    if account.is_superuser:
                        raise ValidationError("دسترسی مدیر اصلی از این بخش تغییر نمی‌کند.")
                    if not account.is_active or account.is_restricted:
                        raise ValidationError("حساب غیرفعال یا محدود را نمی‌توان ادمین کرد.")
                    if not selected:
                        raise ValidationError("حداقل یک بخش را برای این ادمین انتخاب کنید.")
                    before = sorted(account.user_permissions.filter(codename__in=SECTION_PERMISSIONS).values_list("codename", flat=True))
                    if account.role != "staff":
                        account.role = "staff"
                        account.full_clean(exclude=["password", "mfa_secret_encrypted"])
                    account.is_staff = True
                    account.save(update_fields=["role", "is_staff"])
                    account.user_permissions.remove(*section_perms.values())
                    account.user_permissions.add(*(section_perms[code] for code in selected))
                    services.audit(request.user, "staff.sections", account, {"sections": before}, {"sections": sorted(selected)})
                    messages.success(request, f"دسترسی {account.username} به‌روز شد.")
                elif action == "revoke":
                    account = get_object_or_404(Account.objects.select_for_update(), pk=_positive_id(request.POST.get("pk")) or 0, is_superuser=False)
                    account.user_permissions.remove(*section_perms.values())
                    account.is_staff = False
                    account.session_version += 1
                    account.save(update_fields=["is_staff", "session_version"])
                    services.audit(request.user, "staff.revoke", account, after={"sections": []})
                    messages.success(request, f"دسترسی مدیریتی {account.username} برداشته شد و نشست‌های او بسته شد.")
                else:
                    raise ValidationError("عملیات معتبر نیست.")
        except ValidationError as exc:
            messages.error(request, "؛ ".join(exc.messages))
        return redirect("market:staff_admins")
    admins = []
    for account in Account.objects.filter(is_staff=True, is_superuser=False).prefetch_related("user_permissions").order_by("username"):
        codes = {perm.codename for perm in account.user_permissions.all()}
        admins.append({"account": account, "sections": [{"code": code, "label": label, "checked": code in codes} for code, label in SECTION_PERMISSIONS.items()]})
    return render(request, "market/staff_admins.html", {"admins": admins, "sections": SECTION_PERMISSIONS})


INSPECTION_ACTIONS = {"cancel": services.cancel_inspection, "reinspect": services.reinspect}


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def staff_inspections(request):
    """All inspection requests by province, with cancel / re-inspection / report actions."""
    if not (request.user.is_staff and request.user.is_active and not request.user.is_restricted and request.user.has_perm("market.finalize_report")):
        raise PermissionDenied
    if request.method == "POST":
        action = request.POST.get("action", "")
        pk = _positive_id(request.POST.get("pk"))
        reason = request.POST.get("reason", "")[:2000]
        try:
            if action in INSPECTION_ACTIONS:
                INSPECTION_ACTIONS[action](request.user, get_object_or_404(InspectionRequest, pk=pk or 0), reason)
            elif action == "delete_report":
                services.delete_report(request.user, get_object_or_404(InspectionReport, pk=pk or 0), reason)
            elif action == "assign":
                expert_id = _positive_id(request.POST.get("expert"))
                if expert_id is None:
                    raise ValidationError("یک کارشناس انتخاب کنید.")
                services.assign_expert(request.user, get_object_or_404(InspectionRequest, pk=pk or 0), get_object_or_404(ExpertProfile, pk=expert_id))
            elif action == "finalize":
                services.finalize_report(request.user, get_object_or_404(InspectionReport, pk=pk or 0))
            else:
                raise ValidationError("عملیات معتبر نیست.")
            messages.success(request, "عملیات ثبت شد.")
        except ValidationError as exc:
            messages.error(request, "؛ ".join(exc.messages))
        return redirect(request.get_full_path())
    qs = InspectionRequest.objects.select_related("listing__province", "listing__owner", "requester", "expert__user", "report").order_by("-created_at")
    province_id = _positive_id(request.GET.get("province"))
    if province_id:
        qs = qs.filter(listing__province_id=province_id)
    status = request.GET.get("status", "")
    if status in dict(InspectionRequest._meta.get_field("status").choices):
        qs = qs.filter(status=status)
    counts = {row["listing__province"]: row["total"] for row in InspectionRequest.objects.exclude(status="cancelled").values("listing__province").annotate(total=Count("pk"))}
    provinces = [{"province": p, "open": counts.get(p.pk, 0)} for p in Province.objects.all()]
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "market/staff_inspections.html", {
        "inspections": page.object_list, "page_obj": page, "query_string": query.urlencode(),
        "provinces": provinces, "selected_province": province_id, "selected_status": status,
        "statuses": InspectionRequest._meta.get_field("status").choices,
        "experts": ExpertProfile.objects.filter(active=True).select_related("user", "province"),
    })


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def staff_report_edit(request, pk):
    if not (request.user.is_staff and request.user.is_active and not request.user.is_restricted and request.user.has_perm("market.finalize_report")):
        raise PermissionDenied
    report = get_object_or_404(InspectionReport.objects.select_related("inspection__listing", "expert__user"), pk=pk)
    form = forms.ReportForm(request.POST or None, request.FILES or None, initial={"price": report.price, "summary": report.summary})
    if request.method == "POST" and form.is_valid():
        try:
            services.staff_edit_report(request.user, report, form.cleaned_data)
            messages.success(request, "گزارش ویرایش شد و تغییر در سابقه مدیریت ثبت شد.")
            return redirect("market:staff_inspections")
        except ValidationError as exc:
            _form_error(form, exc)
    return _generic(request, "ویرایش گزارش کارشناسی", form, inspection=report.inspection, emoji="clipboard")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def wallet(request):
    """Expert wallet: balance, withdrawal request and history."""
    if request.user.role != "expert" or not hasattr(request.user, "expert_profile"):
        raise PermissionDenied
    expert = request.user.expert_profile
    form = forms.WithdrawalForm(request.POST or None, initial={"iban": expert.iban})
    if request.method == "POST" and form.is_valid():
        try:
            services.request_withdrawal(request.user, form.cleaned_data["amount"], form.cleaned_data["iban"])
            messages.success(request, "درخواست برداشت ثبت شد و مبلغ از موجودی کم شد. واریز معمولاً ظرف ۲۴ تا ۴۸ ساعت انجام می‌شود.")
            return redirect("market:wallet")
        except (ValidationError, PermissionDenied) as exc:
            _form_error(form, exc if isinstance(exc, ValidationError) else ValidationError(str(exc)))
    expert.refresh_from_db()
    return render(request, "market/wallet.html", {"expert": expert, "form": form,
        "withdrawals": expert.withdrawals.all()[:50], "transactions": expert.wallet_transactions.select_related("inspection__listing")[:50]})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def staff_wallets(request):
    if not (request.user.is_staff and request.user.is_active and not request.user.is_restricted and request.user.has_perm("market.manage_billing")):
        raise PermissionDenied
    from .models import WithdrawalRequest
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "credit":
                expert = get_object_or_404(ExpertProfile, pk=_positive_id(request.POST.get("expert")) or 0)
                inspection_id = _positive_id(request.POST.get("inspection"))
                amount = forms.digits_only(request.POST.get("amount", ""))
                services.credit_wallet(request.user, expert, amount or 0, inspection=inspection_id, note=request.POST.get("note", ""))
                messages.success(request, "کیف پول کارشناس شارژ شد (آزمایشی).")
            elif action in {"paid", "reject"}:
                withdrawal = get_object_or_404(WithdrawalRequest, pk=_positive_id(request.POST.get("pk")) or 0)
                services.decide_withdrawal(request.user, withdrawal, paid=action == "paid", reference=request.POST.get("reference", ""))
                messages.success(request, "وضعیت درخواست برداشت به‌روز شد.")
            else:
                raise ValidationError("عملیات معتبر نیست.")
        except (ValidationError, InspectionRequest.DoesNotExist) as exc:
            messages.error(request, "؛ ".join(exc.messages) if isinstance(exc, ValidationError) else "کارشناسی پیدا نشد.")
        return redirect("market:staff_wallets")
    experts = ExpertProfile.objects.select_related("user", "province").order_by("province__name", "user__username")
    unpaid = InspectionRequest.objects.filter(status="final", expert__isnull=False).exclude(wallet_credits__kind="credit").select_related("listing", "expert__user")[:100]
    return render(request, "market/staff_wallets.html", {"experts": experts, "unpaid": unpaid,
        "pending": WithdrawalRequest.objects.filter(status="pending").select_related("expert__user"),
        "recent": WithdrawalRequest.objects.exclude(status="pending").select_related("expert__user", "decided_by")[:30]})
