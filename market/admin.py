"""Granular administration. Moderation and settlement never use raw bulk updates."""
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from . import services
from .models import (Account, AuditEvent, Category, ExpertProfile, InspectionReport,
                     InspectionRequest, Listing, Payment, Plan, Province, SellerPage, Ticket)


admin.site.site_header = "مدیریت کامیوا"
admin.site.site_title = "کامیوا"
admin.site.index_title = "مدیریت پلتفرم آزمایشی"


class DomainPermissionAdmin(admin.ModelAdmin):
    domain_permission = None

    def allowed(self, request):
        return request.user.is_active and not request.user.is_restricted and (
            request.user.is_superuser or (self.domain_permission and request.user.has_perm("market." + self.domain_permission)))

    def has_module_permission(self, request):
        return bool(self.allowed(request))

    def has_view_permission(self, request, obj=None):
        return bool(self.allowed(request))

    def has_change_permission(self, request, obj=None):
        return bool(self.allowed(request))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def service_action(self, request, queryset, service, **kwargs):
        for obj in queryset:
            try:
                service(request.user, obj, **kwargs)
            except (ValidationError, PermissionDenied) as exc:
                self.message_user(request, "; ".join(getattr(exc, "messages", [str(exc)])), level=messages.ERROR)


@admin.register(Account)
class AccountAdmin(UserAdmin):
    list_display = ("username", "internal_code", "role", "identity_status", "is_active", "restricted_until")
    list_filter = ("role", "identity_status", "is_active")
    search_fields = ("username", "internal_code")
    readonly_fields = ("internal_code", "email_verified", "mobile_verified", "identity_status", "identity_reason", "subscription_until", "date_joined", "last_login", "private_documents")
    fieldsets = (
        ("حساب", {"fields": ("username", "password", "internal_code", "role")}),
        ("هویت خصوصی", {"fields": ("first_name", "last_name", "national_id", "birth_date", "email", "mobile", "province", "city", "private_documents", "identity_status", "identity_reason", "email_verified", "mobile_verified")}),
        ("دسترسی", {"fields": ("is_active", "restricted_until", "restriction_reason", "subscription_until", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("زمان", {"fields": ("date_joined", "last_login")}),
    )
    add_fieldsets = (("حساب داخلی", {"classes": ("wide",), "fields": ("username", "password1", "password2", "role")}),)
    actions = ("approve_identities",)

    def authorized_actor(self, request):
        """Re-read revocation state and permissions instead of trusting cached users."""
        try:
            actor = services._account(request.user)
        except (PermissionDenied, Account.DoesNotExist):
            return None
        if actor.is_restricted or not actor.is_staff:
            return None
        return actor

    def has_module_permission(self, request):
        actor = self.authorized_actor(request)
        return bool(actor and (actor.has_perm("market.manage_accounts") or actor.has_perm("market.review_identity")))

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        actor = self.authorized_actor(request)
        return bool(actor and (actor.has_perm("market.manage_accounts") or actor.has_perm("market.review_identity"))
                    and (not obj or not obj.is_superuser or actor.is_superuser))

    def has_add_permission(self, request):
        actor = self.authorized_actor(request)
        return bool(actor and actor.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_fieldsets(self, request, obj=None):
        if request.user.is_superuser:
            return super().get_fieldsets(request, obj)
        fields = ["username", "internal_code", "role", "is_active", "restricted_until", "restriction_reason"]
        if request.user.has_perm("market.review_identity"):
            fields += ["first_name", "last_name", "national_id", "birth_date", "email", "mobile", "province", "city", "private_documents", "identity_status", "identity_reason", "email_verified", "mobile_verified"]
        return (("کاربر", {"fields": fields}),)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and obj.is_superuser:
            fields += ["email"]
        if not request.user.is_superuser:
            fields += ["role", "username"]
            if not request.user.has_perm("market.manage_accounts"):
                fields += ["is_active", "restricted_until", "restriction_reason"]
        return fields

    @admin.display(description="مدارک خصوصی؛ فقط با مجوز بررسی هویت")
    def private_documents(self, obj):
        links = []
        if obj.identity_document:
            links.append((reverse('market:private_file', args=['identity', obj.pk]), 'مدرک هویتی'))
        if hasattr(obj, 'page') and obj.page.license_document:
            links.append((reverse('market:private_file', args=['license', obj.page.pk]), 'جواز کسب'))
        return format_html_join(' | ', '<a href="{}">{}</a>', links) or 'مدرکی ثبت نشده است.'

    def user_change_password(self, request, id, form_url=""):
        actor = self.authorized_actor(request)
        if not actor or not actor.is_superuser:
            raise PermissionDenied("بازنشانی رمز در اختیار مدیر اصلی است.")
        response = super().user_change_password(request, id, form_url)
        if request.method == "POST" and response.status_code == 302:
            with transaction.atomic():
                user = Account.objects.select_for_update().get(pk=id)
                user.session_version += 1
                user.save(update_fields=["session_version"])
                services.audit(request.user, "account.admin_password_reset", user, after={"sessions_invalidated": True})
        return response

    @admin.action(description="تأیید هویت کاربران انتخاب‌شده")
    def approve_identities(self, request, queryset):
        for account in queryset:
            try:
                services.approve_identity(request.user, account)
            except (ValidationError, PermissionDenied) as exc:
                self.message_user(request, "; ".join(getattr(exc, "messages", [str(exc)])), messages.ERROR)

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        if not (self.has_change_permission(request, obj) if change else self.has_add_permission(request)):
            raise PermissionDenied("شما مجوز ویرایش این حساب را ندارید.")
        obj.clean()
        if change:
            original = Account.objects.select_for_update().get(pk=obj.pk)
            if original.is_superuser and obj.email != original.email:
                raise PermissionDenied("تغییر ایمیل مدیر از مسیر تأیید دومرحله‌ای انجام می‌شود.")
            if original.pk == request.user.pk and (not obj.is_active or obj.is_restricted):
                raise ValidationError("مدیر نمی‌تواند دسترسی جاری خود را مسدود کند.")
            identity_fields = {"first_name", "last_name", "national_id", "birth_date", "province", "city", "mobile", "email"}
            if identity_fields.intersection(form.changed_data):
                obj.identity_status = "pending"
            if "mobile" in form.changed_data:
                obj.mobile_verified = False
            if "email" in form.changed_data:
                obj.email_verified = False
            if set(form.changed_data).intersection({"is_active", "restricted_until", "is_staff", "is_superuser", "role"}):
                obj.session_version = original.session_version + 1
        super().save_model(request, obj, form, change)
        services.audit(request.user, "account.admin_edit", obj, after={"changed_fields": [x for x in form.changed_data if x not in {"password", "password1", "password2"}]})


@admin.register(Listing)
class ListingAdmin(DomainPermissionAdmin):
    domain_permission = "moderate_listing"
    list_display = ("title", "owner", "category", "status", "moderation_status", "price")
    list_filter = ("moderation_status", "status", "category", "province")
    search_fields = ("title", "owner__username", "owner__internal_code")
    readonly_fields = ("owner", "status", "moderation_status", "pending_data", "image_review", "rejection_reason", "sold_at", "created_at", "updated_at")
    exclude = ("image_path",)
    actions = ("approve_selected", "reject_selected", "delete_selected_ads")

    @admin.display(description="تصاویر فعلی و در انتظار بررسی")
    def image_review(self, obj):
        return format_html_join('', '<figure><img src="{}" width="160" height="100" alt="تصویر آگهی"><figcaption>{}</figcaption></figure>',
            ((picture.image.url, 'تأییدشده' if picture.approved else 'در انتظار تأیید') for picture in obj.images.all())) or 'تصویری بارگذاری نشده است.'

    @admin.action(description="تأیید آگهی‌ها / ویرایش‌های در انتظار")
    def approve_selected(self, request, queryset):
        self.service_action(request, queryset, services.approve_listing)

    @admin.action(description="رد درخواست انتشار با دلیل عمومی")
    def reject_selected(self, request, queryset):
        self.service_action(request, queryset, services.approve_listing, approve=False, reason="اطلاعات آگهی نیازمند اصلاح است؛ از طریق تیکت با پشتیبانی تماس بگیرید.")

    @admin.action(description="حذف منطقی آگهی‌های انتخاب‌شده")
    def delete_selected_ads(self, request, queryset):
        self.service_action(request, queryset, services.change_listing_status, status="deleted")

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        services._permission(request.user, "moderate_listing")
        owner = Account.objects.select_for_update().get(pk=obj.owner_id)
        original = Listing.objects.select_for_update().get(pk=obj.pk)
        if obj.status == "active":
            services._capacity(owner, obj.category, exclude=obj.pk)
        obj.full_clean()
        if original.price != obj.price:
            services._set_price(request.user, original, obj.price)
        super().save_model(request, obj, form, change)
        services.audit(request.user, "listing.admin_edit", obj, after={"changed_fields": list(form.changed_data)})


@admin.register(SellerPage)
class SellerPageAdmin(DomainPermissionAdmin):
    domain_permission = "moderate_listing"
    list_display = ("display_name", "slug", "user", "moderation_status")
    list_filter = ("moderation_status",)
    readonly_fields = ("user", "slug", "display_name", "bio", "address", "activity", "contact_phone", "contact_whatsapp", "contact_telegram", "contact_email", "moderation_status", "rejection_reason", "created_at", "updated_at", "pending_summary", "avatar_review", "license_review")
    exclude = ("license_document", "avatar", "pending_data")
    actions = ("approve_selected", "reject_selected")

    @admin.display(description="تصویر پروفایل فعلی و پیشنهادی")
    def avatar_review(self, obj):
        images = []
        if obj.avatar:
            images.append((obj.avatar.url, 'تصویر فعلی'))
        if obj.pending_data.get('avatar'):
            field = obj._meta.get_field('avatar')
            pending = field.attr_class(obj, field, obj.pending_data['avatar'])
            images.append((pending.url, 'تصویر در انتظار تأیید'))
        return format_html_join('', '<figure><img src="{}" width="120" height="120" alt="{}"></figure>', images) or 'تصویری ثبت نشده است.'

    @admin.display(description="جواز خصوصی؛ مجوز بررسی هویت لازم است")
    def license_review(self, obj):
        links = []
        if obj.license_document:
            links.append((reverse('market:private_file', args=['license', obj.pk]), 'جواز فعلی'))
        if obj.pending_data.get('license_document'):
            links.append((reverse('market:private_file', args=['license_pending', obj.pk]), 'جواز در انتظار بررسی'))
        return format_html_join(' | ', '<a href="{}">{}</a>', links) or 'جوازی ثبت نشده است.'

    @admin.display(description="تغییرات در انتظار")
    def pending_summary(self, obj):
        return {key: value for key, value in obj.pending_data.items() if key not in {"license_document", "avatar"}}

    @admin.action(description="تأیید صفحه فروشندگان")
    def approve_selected(self, request, queryset):
        self.service_action(request, queryset, services.moderate_page)

    @admin.action(description="رد درخواست صفحه فروشندگان")
    def reject_selected(self, request, queryset):
        self.service_action(request, queryset, services.moderate_page, approve=False, reason="اطلاعات صفحه نیازمند اصلاح است؛ با پشتیبانی تماس بگیرید.")


@admin.register(InspectionRequest)
class InspectionAdmin(DomainPermissionAdmin):
    domain_permission = "finalize_report"
    list_display = ("id", "listing", "requester", "expert", "status")
    readonly_fields = ("listing", "requester", "status", "coordination_note", "created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if "expert" in form.changed_data and obj.expert:
            services.assign_expert(request.user, obj, obj.expert)


@admin.register(InspectionReport)
class ReportAdmin(DomainPermissionAdmin):
    domain_permission = "finalize_report"
    list_display = ("id", "inspection", "expert", "price", "status", "finalized_at")
    readonly_fields = ("inspection", "expert", "submitted_by", "price", "summary", "status", "approved_by", "finalized_at", "created_at", "updated_at")
    exclude = ("private_document",)
    actions = ("finalize_selected",)

    @admin.action(description="تأیید نهایی گزارش کارشناسی")
    def finalize_selected(self, request, queryset):
        self.service_action(request, queryset, services.finalize_report)


@admin.register(Payment)
class PaymentAdmin(DomainPermissionAdmin):
    domain_permission = "manage_billing"
    list_display = ("id", "user", "kind", "amount", "status", "is_test", "created_at")
    readonly_fields = tuple(field.name for field in Payment._meta.fields)


@admin.register(Plan)
class PlanAdmin(DomainPermissionAdmin):
    domain_permission = "manage_billing"
    list_display = ("name", "role", "price", "duration_days", "inspection_fee", "active")
    readonly_fields = ("role",)

    def save_model(self, request, obj, form, change):
        services._permission(request.user, "manage_billing")
        obj.full_clean()
        super().save_model(request, obj, form, change)
        services.audit(request.user, "plan.edit", obj, after={"price": str(obj.price), "days": obj.duration_days, "fee": str(obj.inspection_fee)})


@admin.register(ExpertProfile)
class ExpertAdmin(DomainPermissionAdmin):
    domain_permission = "finalize_report"
    list_display = ("user", "province", "credential", "active")
    list_filter = ("province", "active")

    def has_add_permission(self, request):
        return bool(self.allowed(request))

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "user":
            kwargs["queryset"] = Account.objects.filter(role="expert", is_active=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        services._permission(request.user, "finalize_report")
        obj.full_clean()
        super().save_model(request, obj, form, change)
        services.audit(request.user, "expert.edit", obj, after={"active": obj.active, "province_id": obj.province_id})


@admin.register(Ticket)
class TicketAdmin(DomainPermissionAdmin):
    domain_permission = "support_tickets"
    list_display = ("subject", "owner", "status", "updated_at")
    readonly_fields = ("subject", "owner", "created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        services._permission(request.user, "support_tickets")
        super().save_model(request, obj, form, change)
        services.audit(request.user, "ticket.status", obj, after={"status": obj.status})


@admin.register(AuditEvent)
class AuditAdmin(DomainPermissionAdmin):
    list_display = ("created_at", "actor", "action", "object_type", "object_id")
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)


@admin.register(Province, Category)
class CatalogAdmin(admin.ModelAdmin):
    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)
        services.audit(request.user, "catalog.edit", obj, after={"changed_fields": list(form.changed_data)})
