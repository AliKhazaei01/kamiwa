"""Bound, Persian forms. Actor-controlled and moderation fields are never exposed."""
import re

from captcha.fields import CaptchaField
from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import ValidationError
from django.utils import timezone

from .jalali import ASCII_DIGITS, MONTHS, PERSIAN_DIGITS, to_gregorian, to_jalali
from .models import Account, Category, ExpertProfile, Listing, Province, SellerPage

JALALI_MIN_YEAR = 1300


class JalaliDateWidget(forms.MultiWidget):
    """Day / month / year selects for a Solar Hijri date; the value stays a Gregorian date."""
    template_name = "market/widgets/jalali_date.html"

    def __init__(self, attrs=None):
        current = to_jalali(timezone.localdate())[0]
        days = [("", "روز")] + [(str(d), str(d).translate(PERSIAN_DIGITS)) for d in range(1, 32)]
        months = [("", "ماه")] + [(str(m), name) for m, name in enumerate(MONTHS, start=1)]
        years = [("", "سال")] + [(str(y), str(y).translate(PERSIAN_DIGITS)) for y in range(current, JALALI_MIN_YEAR - 1, -1)]
        super().__init__([forms.Select(attrs={"aria-label": "روز"}, choices=days),
                          forms.Select(attrs={"aria-label": "ماه"}, choices=months),
                          forms.Select(attrs={"aria-label": "سال"}, choices=years)], attrs)

    def decompress(self, value):
        if not value:
            return [None, None, None]
        if isinstance(value, str):
            from django.utils.dateparse import parse_date
            value = parse_date(value)
            if value is None:
                return [None, None, None]
        jy, jm, jd = to_jalali(value)
        return [str(jd), str(jm), str(jy)]


class JalaliDateField(forms.MultiValueField):
    widget = JalaliDateWidget

    def __init__(self, **kwargs):
        error = {"incomplete": "روز، ماه و سال را کامل انتخاب کنید."}
        fields = (forms.IntegerField(), forms.IntegerField(), forms.IntegerField())
        super().__init__(fields=fields, error_messages=error, require_all_fields=True, **kwargs)

    def compress(self, values):
        if not values:
            return None
        day, month, year = values
        try:
            value = to_gregorian(year, month, day)
        except (TypeError, ValueError):
            raise ValidationError("این تاریخ در تقویم شمسی وجود ندارد.")
        if year < JALALI_MIN_YEAR or value > timezone.localdate():
            raise ValidationError("تاریخ تولد باید بین ۱۳۰۰ و امروز باشد.")
        return value


def digits_only(value):
    """Accept Persian/Arabic digits from phone keyboards and store ASCII digits."""
    return re.sub(r"[\s\-]", "", str(value or "").translate(ASCII_DIGITS))


class PersianFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("rows", 4)


class LoginForm(PersianFormMixin, forms.Form):
    username = forms.CharField(label="نام کاربری", max_length=150, widget=forms.TextInput(attrs={"dir": "ltr", "autocomplete": "username"}))
    password = forms.CharField(label="رمز عبور", strip=False, widget=forms.PasswordInput(attrs={"dir": "ltr", "autocomplete": "current-password"}))
    captcha = CaptchaField(label="کد امنیتی")

    def clean_username(self):
        return self.cleaned_data["username"].lower()


class IdentityFields(PersianFormMixin, forms.Form):
    first_name = forms.CharField(label="نام", max_length=150)
    last_name = forms.CharField(label="نام خانوادگی", max_length=150)
    national_id = forms.CharField(label="کد ملی", max_length=10, widget=forms.TextInput(attrs={"dir": "ltr", "inputmode": "numeric", "pattern": "[0-9۰-۹]*", "maxlength": "10", "autocomplete": "off", "data-digits-only": ""}))
    birth_date = JalaliDateField(label="تاریخ تولد (شمسی)")
    mobile = forms.CharField(label="شماره همراه", max_length=11, widget=forms.TextInput(attrs={"dir": "ltr", "inputmode": "numeric", "pattern": "[0-9۰-۹]*", "maxlength": "11", "autocomplete": "tel", "placeholder": "09xxxxxxxxx", "data-digits-only": ""}))
    email = forms.EmailField(label="ایمیل", widget=forms.EmailInput(attrs={"dir": "ltr", "autocomplete": "email"}))
    province = forms.ModelChoiceField(Province.objects.all(), label="استان", empty_label="انتخاب استان")
    city = forms.CharField(label="شهر", max_length=100)
    identity_document = forms.FileField(label="تصویر کارت ملی (خصوصی)", help_text="تصویر یا PDF، حداکثر ۸ مگابایت؛ فقط مدیران مجاز به آن دسترسی دارند.")
    birth_certificate = forms.FileField(label="تصویر صفحه اول شناسنامه (خصوصی)", help_text="تصویر یا PDF، حداکثر ۸ مگابایت؛ فقط مدیران مجاز به آن دسترسی دارند.")

    def clean_national_id(self):
        value = digits_only(self.cleaned_data["national_id"])
        if not re.fullmatch(r"[0-9]{10}", value):
            raise ValidationError("کد ملی باید ۱۰ رقم باشد.")
        return value

    def clean_mobile(self):
        value = digits_only(self.cleaned_data["mobile"])
        if not re.fullmatch(r"09[0-9]{9}", value):
            raise ValidationError("شماره همراه باید ۱۱ رقم و با ۰۹ شروع شود.")
        return value

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean_identity_document(self):
        from .files import validate_private_file
        upload = self.cleaned_data["identity_document"]
        validate_private_file(upload)
        return upload

    def clean_birth_certificate(self):
        from .files import validate_private_file
        upload = self.cleaned_data["birth_certificate"]
        validate_private_file(upload)
        return upload


class RegistrationForm(IdentityFields):
    role = forms.ChoiceField(label="نوع حساب", choices=[("buyer", "خریدار"), ("plus", "پلاس؛ فروشنده حرفه‌ای"), ("pro", "پرو؛ نمایشگاه")])
    username = forms.RegexField(r"\A[A-Za-z]+\Z", max_length=150, label="نام کاربری", help_text="فقط حروف انگلیسی؛ بزرگی و کوچکی حروف تفاوتی ندارد.", error_messages={"invalid": "نام کاربری فقط می‌تواند حروف انگلیسی داشته باشد."}, widget=forms.TextInput(attrs={"dir": "ltr", "autocomplete": "username", "autocapitalize": "none", "spellcheck": "false", "data-username-check": ""}))
    password1 = forms.CharField(label="رمز عبور", strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "dir": "ltr", "data-password-rules": ""}), help_text="حداقل ۸ نویسه؛ شامل حرف بزرگ، حرف کوچک انگلیسی و عدد.")
    password2 = forms.CharField(label="تکرار رمز عبور", strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "dir": "ltr", "data-password-match": "id_password1"}))
    slug = forms.RegexField(r"\A[A-Za-z]{1,8}(?:_personal)?\Z", max_length=17, required=False, label="آیدی صفحه فروشنده", help_text="۱ تا ۸ حرف انگلیسی؛ برای پلاس پسوند personal خودکار اضافه می‌شود.")
    display_name = forms.CharField(label="نام نمایشگاه", max_length=160, required=False)
    address = forms.CharField(label="آدرس دقیق نمایشگاه", widget=forms.Textarea, required=False)
    activity = forms.CharField(label="زمینه فعالیت نمایشگاه", widget=forms.Textarea, required=False)
    license_document = forms.FileField(label="جواز کسب (خصوصی)", required=False)
    avatar = forms.ImageField(label="عکس پرسنلی / تصویر نمایشگاه", required=False)

    def clean_username(self):
        username = self.cleaned_data["username"].lower()
        if Account.objects.filter(username__iexact=username).exists():
            raise ValidationError("این نام کاربری قبلاً ثبت شده است.")
        return username

    def clean(self):
        data = super().clean()
        password = data.get("password1")
        if password:
            try:
                password_validation.validate_password(password, Account(username=data.get("username", ""), first_name=data.get("first_name", ""), last_name=data.get("last_name", "")))
            except ValidationError as exc:
                self.add_error("password1", exc)
        if password != data.get("password2"):
            self.add_error("password2", "تکرار رمز عبور مطابقت ندارد.")
        role = data.get("role")
        if role in {"pro", "plus"}:
            if not data.get("slug"):
                self.add_error("slug", "آیدی صفحه فروشنده الزامی است.")
            else:
                base = data["slug"].lower().removesuffix("_personal")
                data["slug"] = base + ("_personal" if role == "plus" else "")
                from .models import SlugHistory
                if SellerPage.objects.filter(slug__iexact=data["slug"]).exists() or SlugHistory.objects.filter(slug__iexact=data["slug"]).exists():
                    self.add_error("slug", "این آیدی قبلاً استفاده شده است.")
        if role == "pro":
            for name in ("display_name", "address", "activity", "license_document"):
                if not data.get(name):
                    self.add_error(name, "برای حساب پرو الزامی است.")
        if role == "plus":
            data["display_name"] = " ".join(filter(None, (data.get("first_name"), data.get("last_name"))))
            if not data.get("avatar"):
                self.add_error("avatar", "عکس پرسنلی صاحب حساب پلاس الزامی است.")
        if data.get("license_document"):
            from .files import validate_private_file
            try:
                validate_private_file(data["license_document"])
            except ValidationError as exc:
                self.add_error("license_document", exc)
        return data


class ProfileForm(IdentityFields):
    username = forms.RegexField(r"\A[A-Za-z]+\Z", max_length=150, label="نام کاربری", widget=forms.TextInput(attrs={"dir": "ltr"}))
    identity_document = forms.FileField(label="تصویر جدید کارت ملی (اختیاری و خصوصی)", required=False)
    birth_certificate = forms.FileField(label="تصویر جدید شناسنامه (اختیاری و خصوصی)", required=False)

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user.is_superuser:
            self.fields["email"].disabled = True
            self.fields["email"].help_text = "تغییر ایمیل مدیر از مسیر امن تغییر ایمیل انجام می‌شود."

    def clean_username(self):
        username = self.cleaned_data["username"].lower()
        if Account.objects.filter(username__iexact=username).exclude(pk=self.user.pk).exists():
            raise ValidationError("این نام کاربری قبلاً ثبت شده است.")
        return username

    def clean_identity_document(self):
        upload = self.cleaned_data.get("identity_document")
        if upload:
            from .files import validate_private_file
            validate_private_file(upload)
        return upload

    def clean_birth_certificate(self):
        upload = self.cleaned_data.get("birth_certificate")
        if upload:
            from .files import validate_private_file
            validate_private_file(upload)
        return upload


class SellerPageForm(PersianFormMixin, forms.ModelForm):
    class Meta:
        model = SellerPage
        fields = ["slug", "display_name", "bio", "address", "activity", "avatar", "license_document", "contact_phone", "contact_whatsapp", "contact_telegram", "contact_email"]
        widgets = {"license_document": forms.FileInput}
        labels = {"slug": "آیدی صفحه", "display_name": "نام نمایشی", "bio": "درباره شما", "address": "آدرس", "activity": "زمینه فعالیت", "avatar": "عکس پروفایل", "license_document": "جواز کسب (خصوصی)", "contact_phone": "تلفن قابل نمایش", "contact_whatsapp": "شماره واتساپ", "contact_telegram": "آیدی تلگرام", "contact_email": "ایمیل قابل نمایش"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.user.role == "plus":
            self.fields["display_name"].disabled = True
            self.initial["display_name"] = self.instance.user.get_full_name()
            self.fields["display_name"].help_text = "نام صفحه پلاس از نام و نام خانوادگی تأییدشده شما گرفته می‌شود."

    def clean_slug(self):
        value = self.cleaned_data["slug"].lower()
        base = value.removesuffix("_personal")
        if not re.fullmatch(r"[a-z]{1,8}", base):
            raise ValidationError("آیدی باید ۱ تا ۸ حرف انگلیسی باشد.")
        return base + ("_personal" if self.instance.user.role == "plus" else "")

    def clean_avatar(self):
        upload = self.cleaned_data.get("avatar")
        if upload and hasattr(upload, "content_type"):
            from .files import validate_public_image
            validate_public_image(upload)
        if self.instance.user.role == "plus" and not upload:
            raise ValidationError("عکس پرسنلی حساب پلاس الزامی است.")
        return upload

    def clean_license_document(self):
        upload = self.cleaned_data.get("license_document")
        if upload and hasattr(upload, "content_type"):
            from .files import validate_private_file
            validate_private_file(upload)
        return upload

    def clean_contact_telegram(self):
        value = self.cleaned_data.get("contact_telegram", "").lstrip("@")
        if value and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
            raise ValidationError("آیدی تلگرام باید ۵ تا ۳۲ حرف، عدد انگلیسی یا زیرخط باشد.")
        return value

    def clean_contact_whatsapp(self):
        value = self.cleaned_data.get("contact_whatsapp", "").replace(" ", "").lstrip("+")
        if value and not re.fullmatch(r"[0-9]{8,15}", value):
            raise ValidationError("شماره واتساپ را با کد کشور و ارقام انگلیسی وارد کنید.")
        return value


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        if isinstance(data, (list, tuple)):
            return [super(MultipleFileField, self).clean(item, initial) for item in data if item]
        return [super().clean(data, initial)] if data else []


MAX_LISTING_IMAGES = 5
MAX_LISTING_VIDEOS = 2


class ListingForm(PersianFormMixin, forms.ModelForm):
    images = MultipleFileField(label="تصاویر آگهی", required=False, help_text="تا ۵ تصویر (JPG، PNG یا WebP، هر کدام حداکثر ۸ مگابایت). تصاویر پس از بررسی مدیر منتشر می‌شوند.", widget=MultipleFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}))
    videos = MultipleFileField(label="ویدیوهای آگهی (ویژه حساب پرو)", required=False, help_text="تا ۲ ویدیو MP4 یا MOV، هر کدام حداکثر یک دقیقه، ۱۰۸۰p و ۶۰ مگابایت.", widget=MultipleFileInput(attrs={"accept": "video/mp4,video/quicktime"}))

    def __init__(self, *args, allow_video=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_video:
            del self.fields["videos"]

    def clean_images(self):
        from .files import validate_public_image
        uploads = self.cleaned_data.get("images") or []
        if len(uploads) > MAX_LISTING_IMAGES:
            raise ValidationError("حداکثر ۵ تصویر برای هر آگهی مجاز است.")
        for upload in uploads:
            validate_public_image(upload)
        return uploads

    def clean_videos(self):
        from .files import validate_video
        uploads = self.cleaned_data.get("videos") or []
        if len(uploads) > MAX_LISTING_VIDEOS:
            raise ValidationError("حداکثر ۲ ویدیو برای هر آگهی مجاز است.")
        for upload in uploads:
            validate_video(upload)
        return uploads

    class Meta:
        model = Listing
        fields = ["title", "category", "brand", "model", "year", "mileage", "province", "city", "price", "negotiability", "description"]
        labels = {"title": "عنوان آگهی", "category": "دسته‌بندی", "brand": "برند", "model": "مدل", "year": "سال ساخت (شمسی)", "mileage": "کارکرد (کیلومتر)", "province": "استان", "city": "شهر", "price": "قیمت پیشنهادی (تومان)", "negotiability": "شرایط مذاکره", "description": "توضیحات"}


class ListingStatusForm(PersianFormMixin, forms.Form):
    status = forms.ChoiceField(choices=Listing.Status.choices, label="وضعیت آگهی")


class PriceForm(PersianFormMixin, forms.Form):
    price = forms.DecimalField(label="قیمت جدید (تومان)", max_digits=18, decimal_places=0, min_value=0)


class TicketForm(PersianFormMixin, forms.Form):
    subject = forms.CharField(label="عنوان", max_length=180)
    body = forms.CharField(label="متن پیام", max_length=10000, widget=forms.Textarea)


class ReplyForm(PersianFormMixin, forms.Form):
    body = forms.CharField(label="متن پاسخ", max_length=10000, widget=forms.Textarea)


class InspectionForm(PersianFormMixin, forms.Form):
    coordination_note = forms.CharField(label="توضیحات هماهنگی بازدید", max_length=2000, required=False, widget=forms.Textarea)


class ReportForm(PersianFormMixin, forms.Form):
    price = forms.DecimalField(label="قیمت کارشناسی (تومان)", max_digits=18, decimal_places=0, min_value=1)
    summary = forms.CharField(label="خلاصه گزارش", max_length=20000, widget=forms.Textarea)
    private_document = forms.FileField(label="فایل گزارش (خصوصی)", required=False)

    def clean_private_document(self):
        upload = self.cleaned_data.get("private_document")
        if upload:
            from .files import validate_private_file
            validate_private_file(upload)
        return upload


class OTPForm(PersianFormMixin, forms.Form):
    channel = forms.ChoiceField(label="روش تأیید", choices=[("email", "ایمیل آزمایشی"), ("sms", "پیامک آزمایشی")])
    code = forms.RegexField(r"\A[0-9]{6}\Z", label="کد تأیید", required=False, widget=forms.TextInput(attrs={"dir": "ltr", "inputmode": "numeric", "autocomplete": "one-time-code"}))


class KamiwaPasswordChangeForm(PersianFormMixin, PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["old_password"].label = "رمز عبور فعلی"
        self.fields["new_password1"].label = "رمز عبور جدید"
        self.fields["new_password2"].label = "تکرار رمز عبور جدید"


class MFAForm(PersianFormMixin, forms.Form):
    code = forms.RegexField(r"\A[0-9]{6}\Z", label="کد شش‌رقمی برنامه تأیید دومرحله‌ای", widget=forms.TextInput(attrs={"dir": "ltr", "inputmode": "numeric", "autocomplete": "one-time-code"}))


class AdminEmailForm(PersianFormMixin, forms.Form):
    email = forms.EmailField(label="ایمیل جدید", widget=forms.EmailInput(attrs={"dir": "ltr"}))
    password = forms.CharField(label="رمز عبور فعلی", strip=False, widget=forms.PasswordInput)
    mfa_code = forms.RegexField(r"\A[0-9]{6}\Z", label="کد تأیید دومرحله‌ای")
    email_code = forms.CharField(label="کد تأیید ایمیل جدید", required=False, max_length=6)

    def __init__(self, *args, mfa_enabled=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not mfa_enabled:
            self.fields.pop("mfa_code")
        else:
            self.fields["mfa_code"].help_text = "برای هر بار ارسال، کد تازه برنامه تأییدکننده را وارد کنید."


class PasswordResetForm(PersianFormMixin, forms.Form):
    username = forms.CharField(label="نام کاربری", max_length=150)
    email = forms.EmailField(label="ایمیل تأییدشده", widget=forms.EmailInput(attrs={"dir": "ltr"}))
    captcha = CaptchaField(label="کد امنیتی")


class PasswordResetConfirmForm(PersianFormMixin, forms.Form):
    username = forms.CharField(label="نام کاربری", max_length=150)
    email = forms.EmailField(label="ایمیل تأییدشده", widget=forms.EmailInput(attrs={"dir": "ltr"}))
    code = forms.RegexField(r"\A[0-9]{6}\Z", label="کد بازیابی", widget=forms.TextInput(attrs={"autocomplete": "one-time-code"}))
    new_password1 = forms.CharField(label="رمز عبور جدید", strip=False, widget=forms.PasswordInput)
    new_password2 = forms.CharField(label="تکرار رمز عبور جدید", strip=False, widget=forms.PasswordInput)

    def clean(self):
        data = super().clean()
        password = data.get("new_password1")
        if password:
            try:
                password_validation.validate_password(password, Account(username=data.get("username", "")))
            except ValidationError as exc:
                self.add_error("new_password1", exc)
        if password != data.get("new_password2"):
            self.add_error("new_password2", "تکرار رمز عبور مطابقت ندارد.")
        return data


class WithdrawalForm(PersianFormMixin, forms.Form):
    amount = forms.CharField(label="مبلغ برداشت (تومان)", widget=forms.TextInput(attrs={"inputmode": "numeric", "dir": "ltr", "data-digits-only": "", "maxlength": "15"}))
    iban = forms.CharField(label="شماره شبا", max_length=34, widget=forms.TextInput(attrs={"dir": "ltr", "placeholder": "IR000000000000000000000000", "autocomplete": "off"}))

    def clean_amount(self):
        value = digits_only(self.cleaned_data["amount"])
        if not value.isdigit() or int(value) <= 0:
            raise ValidationError("مبلغ را به تومان و فقط با رقم وارد کنید.")
        return int(value)

    def clean_iban(self):
        from .services import normalize_iban
        return normalize_iban(self.cleaned_data["iban"])
