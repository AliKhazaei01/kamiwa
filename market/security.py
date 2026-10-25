import re
import secrets
import hashlib
import time
from datetime import timedelta

import pyotp
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.hashers import make_password, check_password
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction, connection
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .providers import encrypt_payload, decrypt_payload, queue_message
from .rls import as_system, apply_user, system_context


class RequiredPasswordValidator:
    def validate(self, password, user=None):
        if len(password) < 8 or len(password) > 128 or not re.search('[a-z]', password) or not re.search('[A-Z]', password) or not re.search('[0-9]', password):
            raise ValidationError(self.get_help_text(), code='password_complexity')

    def get_help_text(self):
        return 'رمز باید ۸ تا ۱۲۸ نویسه و شامل حرف انگلیسی بزرگ، کوچک و عدد باشد.'


class CaseInsensitiveBackend(ModelBackend):
    # Sign-in looks the account up before anyone is signed in.
    @as_system
    def authenticate(self, request, username=None, password=None, **kwargs):
        from .models import Account
        username = (username or '').strip().lower()
        try:
            user = Account.objects.get(username__iexact=username)
        except Account.DoesNotExist:
            Account().set_password(password or '')
            return None
        if password is not None and user.check_password(password) and self.user_can_authenticate(user):
            return user


def throttle(key, limit=5, window=300):
    digest = hashlib.sha256(str(key).encode()).hexdigest()
    key = 'limit:' + digest
    with transaction.atomic():
        # DatabaseCache.incr alone is not atomic across Gunicorn requests.
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [int(digest[:15], 16)])
        if cache.add(key, 1, window):
            return True
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, window)
            count = 1
        return count <= limit


def establish_session(request, user):
    with system_context():
        login(request, user, backend='market.security.CaseInsensitiveBackend')
    # The rest of this request runs with the new user's row filters.
    apply_user(user)
    request.session['session_version'] = user.session_version
    request.session.setdefault('mfa_verified', False)


def notify_login(request, user):
    """Queue a "successful sign-in" email; the test provider keeps it in the private outbox."""
    if not user.email:
        return None
    from .jalali import format_jalali
    now = timezone.localtime()
    agent = request.META.get('HTTP_USER_AGENT', '')[:160]
    return queue_message('email', user.email, {
        'subject': 'ورود موفق به حساب کامیوا',
        'body': (f'ورود به حساب {user.username} در {format_jalali(now.date())} ساعت {now:%H:%M} انجام شد.\n'
                 f'نشانی IP: {request.META.get("REMOTE_ADDR", "")}\nمرورگر: {agent}\n'
                 'اگر این ورود کار شما نبود، فوراً رمز عبور را تغییر دهید و به پشتیبانی اطلاع دهید.'),
        'is_test': settings.KAMIWA_TEST_PROVIDERS,
    })


LAST_SEEN_STEP = timedelta(minutes=2)


def record_activity(user):
    """At most one write per user every two minutes; each sample counts as two active minutes."""
    from django.db.models import F, Q
    from .models import Account, ActivityDay
    now = timezone.now()
    if user.last_seen_at and now - user.last_seen_at < LAST_SEEN_STEP:
        return
    # The conditional update lets only one of several concurrent requests count the sample.
    stale = Q(last_seen_at__isnull=True) | Q(last_seen_at__lt=now - LAST_SEEN_STEP)
    if not Account.objects.filter(stale, pk=user.pk).update(last_seen_at=now):
        return
    user.last_seen_at = now
    day = timezone.localdate(now)
    if not ActivityDay.objects.filter(user_id=user.pk, day=day).update(minutes=F('minutes') + 2):
        try:
            with transaction.atomic():
                ActivityDay.objects.create(user_id=user.pk, day=day, minutes=2)
        except IntegrityError:
            ActivityDay.objects.filter(user_id=user.pk, day=day).update(minutes=F('minutes') + 2)


class AccountSessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated:
            stored = request.session.get('session_version')
            if stored is not None and stored != user.session_version:
                logout(request)
                return redirect('market:login')
            request.session.setdefault('session_version', user.session_version)
            if user.mfa_secret_encrypted:
                allowed = {reverse('market:mfa_challenge'), reverse('market:logout')}
                if request.path not in allowed and not request.path.startswith(('/static/', '/captcha/')) and not request.session.get('mfa_verified'):
                    request.session['pending_user_id'] = user.pk
                    return redirect('market:mfa_challenge')
            record_activity(user)
        return self.get_response(request)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.setdefault('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
        if request.user.is_authenticated:
            response['Cache-Control'] = 'private, no-store'
        return response


@as_system
def register_account(data):
    from .models import Account, SellerPage
    from .files import validate_private_file, sanitize_image
    from .services import _slug_available
    role = data.get('role', 'buyer')
    if role not in {'buyer', 'plus', 'pro'}:
        raise ValidationError('نوع حساب معتبر نیست.')
    username = str(data.get('username', '')).strip().lower()
    if not re.fullmatch('[a-zA-Z]+', username):
        raise ValidationError('نام کاربری فقط شامل حروف انگلیسی باشد.')
    fields = {k: data[k] for k in ('first_name', 'last_name', 'email', 'mobile', 'national_id', 'birth_date', 'province', 'city') if k in data}
    fields['email'] = fields.get('email', '').strip().lower()
    user = Account(username=username, role=role, **fields)
    password = data.get('password1', data.get('password', ''))
    validate_password(password, user)
    user.set_password(password)
    for name in ('identity_document', 'birth_certificate'):
        if data.get(name):
            validate_private_file(data[name])
            setattr(user, name, data[name])
    user.full_clean(exclude=['password', 'internal_code'])
    for attempt in range(30):
        try:
            with transaction.atomic():
                user.save()
                if user.is_seller:
                    base = str(data.get('slug', '')).lower()
                    if role == 'plus' and base.endswith('_personal'):
                        base = base[:-9]
                    if not re.fullmatch('[a-z]{1,8}', base):
                        raise ValidationError('آیدی صفحه باید یک تا هشت حرف انگلیسی باشد.')
                    slug = base + ('_personal' if role == 'plus' else '')
                    _slug_available(slug)
                    display_name = user.get_full_name() if role == 'plus' else data.get('display_name') or user.get_full_name()
                    page = SellerPage(user=user, slug=slug, display_name=display_name, address=data.get('address', ''), activity=data.get('activity', ''))
                    if data.get('avatar'):
                        page.avatar = sanitize_image(data['avatar'])
                    if data.get('license_document'):
                        validate_private_file(data['license_document'])
                        page.license_document = data['license_document']
                    page.full_clean()
                    page.save()
                return user
        except IntegrityError:
            if Account.objects.filter(username__iexact=username).exists() or Account.objects.filter(email__iexact=user.email).exists() or Account.objects.filter(mobile=user.mobile).exists():
                raise ValidationError('نام کاربری، ایمیل یا موبایل قبلاً ثبت شده است.')
            user.pk = None
            user.internal_code = None
    raise ValidationError('ساخت شناسه یکتا ممکن نشد؛ دوباره تلاش کنید.')


def issue_otp(user, channel, purpose='verify', target=None):
    from .models import Account, OTP
    if channel not in {'email', 'sms'}:
        raise ValidationError('روش ارسال نامعتبر است.')
    if target is None:
        target = user.email if channel == 'email' else user.mobile
    if not target:
        raise ValidationError('نشانی گیرنده ثبت نشده است.')
    with transaction.atomic():
        Account.objects.select_for_update().get(pk=user.pk)
        recent = OTP.objects.filter(user=user, channel=channel, purpose=purpose)
        now = timezone.now()
        if recent.filter(created_at__gt=now - timedelta(seconds=60)).exists() or recent.filter(created_at__gt=now - timedelta(hours=1)).count() >= 5:
            raise ValidationError('برای ارسال دوباره کمی صبر کنید.')
        recent.filter(consumed_at__isnull=True).update(consumed_at=now)
        code = f'{secrets.randbelow(1_000_000):06d}'
        otp = OTP.objects.create(user=user, target=target, channel=channel, purpose=purpose, code_hash=make_password(code), expires_at=now + timedelta(minutes=5))
        queue_message(channel, target, {'subject': 'کد تأیید آزمایشی کامیوا', 'code': code, 'purpose': purpose, 'body': f'کد تأیید شما: {code}\nاین پیام آزمایشی است و ارسال واقعی انجام نشده است.'}, f'otp:{otp.pk}')
        return otp


def consume_otp(user, channel, code, purpose='verify', target=None):
    from .models import Account, OTP
    target = target or (user.email if channel == 'email' else user.mobile)
    with transaction.atomic():
        locked = Account.objects.select_for_update().get(pk=user.pk)
        otp = OTP.objects.select_for_update().filter(user=user, channel=channel, purpose=purpose, target=target, consumed_at__isnull=True).order_by('-created_at').first()
        if not otp or otp.expires_at <= timezone.now() or otp.attempts >= 5:
            return False
        otp.attempts += 1
        valid = check_password(str(code), otp.code_hash)
        if valid:
            otp.consumed_at = timezone.now()
        otp.save(update_fields=['attempts', 'consumed_at'])
        if valid and purpose == 'verify':
            field = 'email_verified' if channel == 'email' else 'mobile_verified'
            current_target = locked.email if channel == 'email' else locked.mobile
            if current_target != target:
                return False
            setattr(locked, field, True)
            locked.save(update_fields=[field])
            setattr(user, field, True)
        return valid


def begin_mfa_setup(user):
    if not user.is_active or user.is_restricted:
        raise PermissionDenied('حساب محدود یا غیرفعال اجازه تغییر تنظیمات امنیتی ندارد.')
    secret = pyotp.random_base32()
    return {'secret': secret, 'uri': pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name='Kamiwa')}


def confirm_mfa_setup(user, secret, code):
    from .models import Account
    if not throttle(f'mfa:{user.pk}', 8, 300):
        return False
    with transaction.atomic():
        locked = Account.objects.select_for_update().get(pk=user.pk)
        counter = _valid_totp_counter(secret, code)
        if not locked.is_active or locked.is_restricted or locked.mfa_secret_encrypted or counter is None:
            return False
        locked.mfa_secret_encrypted = encrypt_payload({'secret': secret})
        locked.mfa_last_counter = counter
        locked.save(update_fields=['mfa_secret_encrypted', 'mfa_last_counter'])
        user.mfa_secret_encrypted = locked.mfa_secret_encrypted
        user.mfa_last_counter = counter
        return True


def _valid_totp_counter(secret, code):
    totp = pyotp.TOTP(secret)
    current = int(time.time()) // totp.interval
    for counter in (current, current - 1, current + 1):
        if secrets.compare_digest(totp.at(counter * totp.interval), str(code)):
            return counter
    return None


def check_mfa(user, code):
    from .models import Account
    if not throttle(f'mfa:{user.pk}', 8, 300):
        return False
    with transaction.atomic():
        locked = Account.objects.select_for_update().get(pk=user.pk)
        if not locked.is_active or locked.is_restricted or not locked.mfa_secret_encrypted:
            return False
        secret = decrypt_payload(locked.mfa_secret_encrypted)['secret']
        counter = _valid_totp_counter(secret, code)
        if counter is None or counter <= locked.mfa_last_counter:
            return False
        locked.mfa_last_counter = counter
        locked.save(update_fields=['mfa_last_counter'])
        user.mfa_last_counter = counter
        return True


def _reauthenticate_admin(user, password, mfa_code=''):
    if not user.is_active or user.is_restricted or not user.is_staff or not user.is_superuser or not user.check_password(password):
        raise PermissionDenied('رمز عبور فعلی یا دسترسی مدیریت معتبر نیست.')
    if user.mfa_secret_encrypted and not check_mfa(user, mfa_code):
        raise PermissionDenied('کد تازه تأیید دومرحله‌ای لازم است.')


def begin_admin_email_change(user, new_email, password, mfa_code=''):
    from .models import Account
    from django.core.validators import validate_email
    if not throttle(f'admin-email-reauth:{user.pk}', 8, 300):
        raise ValidationError('تعداد تلاش‌های تأیید زیاد است؛ پنج دقیقه دیگر تلاش کنید.')
    with transaction.atomic():
        user = Account.objects.select_for_update().get(pk=user.pk)
        _reauthenticate_admin(user, password, mfa_code)
        new_email = new_email.strip().lower()
        validate_email(new_email)
        if Account.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
            raise ValidationError('این ایمیل قبلاً استفاده شده است.')
        return issue_otp(user, 'email', purpose='admin_email_change', target=new_email)


def complete_admin_email_change(user, new_email, code, password, mfa_code=''):
    from .models import Account, AuditEvent
    if not throttle(f'admin-email-reauth:{user.pk}', 8, 300):
        raise ValidationError('تعداد تلاش‌های تأیید زیاد است؛ پنج دقیقه دیگر تلاش کنید.')
    new_email = new_email.strip().lower()
    with transaction.atomic():
        locked = Account.objects.select_for_update().get(pk=user.pk)
        _reauthenticate_admin(locked, password, mfa_code)
        if not consume_otp(locked, 'email', code, purpose='admin_email_change', target=new_email):
            return False
        old_email = locked.email
        locked.email = new_email
        locked.email_verified = True
        locked.full_clean(exclude=['password'])
        locked.save(update_fields=['email', 'email_verified'])
        queue_message('email', old_email, {'subject': 'تغییر ایمیل مدیریت', 'body': 'ایمیل مدیریت حساب شما تغییر کرد.'})
        AuditEvent.objects.create(actor=user, action='admin_email_changed', object_type='Account', object_id=str(user.pk), before={'email': old_email}, after={'email': new_email})
        user.email = new_email
        return True


@as_system
def request_password_reset(username, email):
    from .models import Account
    user = Account.objects.filter(username__iexact=username.strip(), email__iexact=email.strip(), email_verified=True, is_active=True).first()
    if user and throttle(f'password-reset:{user.pk}', 3, 3600):
        try:
            issue_otp(user, 'email', purpose='password_reset')
        except ValidationError:
            pass


@as_system
def reset_password(user, code, new_password):
    from .models import Account
    validate_password(new_password, user)
    with transaction.atomic():
        locked = Account.objects.select_for_update().get(pk=user.pk)
        if not consume_otp(locked, 'email', code, purpose='password_reset'):
            return False
        locked.set_password(new_password)
        locked.session_version += 1
        locked.save(update_fields=['password', 'session_version'])
        return True
