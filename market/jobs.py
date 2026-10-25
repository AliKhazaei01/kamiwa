"""Bounded database-backed jobs; safe for retries and concurrent timer invocations."""
import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Account, AuditEvent, CredentialRotation, OutboxMessage
from .providers import TestMessageProvider, encrypt_payload, queue_message


def rotate_admin_credentials(now=None):
    if not settings.KAMIWA_ADMIN_ROTATION:
        return 0
    now = now or timezone.now()
    rotated = 0
    for user_id in Account.objects.filter(is_superuser=True, is_active=True).values_list('id', flat=True):
        with transaction.atomic():
            user = Account.objects.select_for_update().get(pk=user_id)
            last = user.rotations.filter(applied_at__isnull=False).order_by('-applied_at').first()
            reference = last.applied_at if last else user.date_joined
            if now < reference + timedelta(days=settings.KAMIWA_ROTATION_DAYS):
                continue
            period = f'admin:{user.pk}:{reference.isoformat()}'
            rotation, created = CredentialRotation.objects.get_or_create(user=user, period_key=period, defaults={'previous_username': user.username, 'new_username': ''})
            if not created:
                continue
            for _ in range(30):
                username = 'admin' + ''.join(secrets.choice(string.ascii_lowercase) for _ in range(10))
                if not Account.objects.filter(username=username).exists():
                    break
            password = 'Aa7' + ''.join(secrets.choice(string.ascii_letters + string.digits + '!@#%_-') for _ in range(21))
            old_username = user.username
            user.username = username
            user.set_password(password)
            user.session_version += 1
            user.save(update_fields=['username', 'password', 'session_version'])
            message = queue_message('email', user.email, {'subject': 'اطلاعات ماهانه مدیریت کامیوا — آزمایشی', 'username': username, 'password': password, 'body': 'نام کاربری و رمز مدیریت تغییر کردند. تأیید دومرحله‌ای همچنان لازم است.', 'is_test': settings.KAMIWA_TEST_PROVIDERS}, f'rotation:{rotation.pk}')
            rotation.new_username = username
            rotation.applied_at = now
            rotation.status = 'applied'
            rotation.outbox = message
            rotation.save(update_fields=['new_username', 'applied_at', 'status', 'outbox'])
            AuditEvent.objects.create(actor=None, action='admin_credentials_rotated', object_type='Account', object_id=str(user.pk), before={'username': old_username}, after={'username': username})
            rotated += 1
    return rotated


def deliver_outbox(limit=30, now=None):
    now = now or timezone.now()
    count = 0
    candidates = list(OutboxMessage.objects.filter(status__in=['pending', 'failed'], next_attempt_at__lte=now, attempts__lt=8).order_by('id').values_list('id', flat=True)[:limit])
    for message_id in candidates:
        with transaction.atomic():
            message = OutboxMessage.objects.select_for_update().get(pk=message_id)
            if message.status not in {'pending', 'failed'} or message.next_attempt_at > now:
                continue
            message.attempts += 1
            try:
                TestMessageProvider().send(message)
            except (ConnectionError, RuntimeError):
                message.status = 'failed'
                message.error_code = 'DELIVERY_UNAVAILABLE'
                message.next_attempt_at = now + timedelta(seconds=min(3600, 30 * 2 ** message.attempts))
            else:
                message.status = 'sent'
                message.sent_at = now
                message.error_code = ''
                CredentialRotation.objects.filter(outbox=message, status='applied').update(status='sent', sent_at=now)
                count += 1
            message.save(update_fields=['attempts', 'status', 'error_code', 'next_attempt_at', 'sent_at'])
    return count


def purge_expired_secrets(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings.KAMIWA_PRIVATE_MESSAGE_TTL_HOURS)
    return OutboxMessage.objects.filter(created_at__lt=cutoff).exclude(payload_ciphertext='').update(payload_ciphertext='', status='expired')


def run_jobs():
    result = {'rotated': rotate_admin_credentials(), 'sent': deliver_outbox(), 'expired_payloads': purge_expired_secrets()}
    return result
