"""Test-only provider adapters. Real delivery is explicitly not implemented."""
import base64
import hashlib
import json
from datetime import timedelta
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone


def _fernet():
    key = settings.KAMIWA_OUTBOX_KEY
    if not key:
        if not settings.TESTING:
            raise RuntimeError('KAMIWA_OUTBOX_KEY is required')
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_payload(payload):
    return _fernet().encrypt(json.dumps(payload, ensure_ascii=False).encode()).decode()


def decrypt_payload(ciphertext):
    try:
        return json.loads(_fernet().decrypt(ciphertext.encode()))
    except (InvalidToken, ValueError, TypeError):
        raise ValidationError('محتوای پیام قابل بازیابی نیست.')


def queue_message(channel, target, payload, idempotency_key=None):
    # Messages are often addressed to someone other than the person acting.
    from .rls import system_context
    with system_context():
        return _queue_message(channel, target, payload, idempotency_key)


def _queue_message(channel, target, payload, idempotency_key=None):
    from .models import OutboxMessage
    fields = dict(channel=channel, target=target, payload_ciphertext=encrypt_payload(payload))
    if idempotency_key:
        return OutboxMessage.objects.get_or_create(idempotency_key=idempotency_key, defaults=fields)[0]
    return OutboxMessage.objects.create(**fields)


class PaymentProvider(Protocol):
    def settle(self, actor, payment, outcome): ...


class SmsProvider(Protocol):
    def send(self, message): ...


class EmailProvider(Protocol):
    def send(self, message): ...


class TestPaymentProvider:
    def settle(self, actor, payment, outcome):
        from .services import settle_payment
        if not settings.KAMIWA_TEST_PROVIDERS or not payment.is_test:
            raise PermissionDenied
        if not actor.is_authenticated or actor.pk != payment.user_id or not actor.is_active or actor.is_restricted:
            raise PermissionDenied
        if outcome not in {'success', 'failure', 'delayed'}:
            raise ValidationError('نتیجه پرداخت معتبر نیست.')
        return settle_payment(payment, outcome=outcome)


class TestMessageProvider:
    def send(self, message):
        if not settings.KAMIWA_TEST_PROVIDERS:
            raise RuntimeError('Real message provider is not configured')
        payload = decrypt_payload(message.payload_ciphertext)
        simulation = payload.get('simulation', 'success')
        if simulation in {'failure', 'delayed'}:
            raise ConnectionError('SIMULATED_DELIVERY_FAILURE')
        # Ciphertext remains in the private test inbox until its TTL. No external I/O.
        return {'is_test': True, 'reference': f'test-message-{message.pk}'}


def get_payment_provider():
    if not settings.KAMIWA_TEST_PROVIDERS:
        raise RuntimeError('Real payment provider is not configured')
    return TestPaymentProvider()


def simulate_payment(actor, payment, outcome):
    return get_payment_provider().settle(actor, payment, outcome)


def read_test_message(actor, message):
    if not settings.KAMIWA_TEST_PROVIDERS or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied
    targets = {actor.email.lower(), actor.mobile}
    if not actor.is_superuser and message.target.lower() not in targets:
        raise PermissionDenied
    cutoff = timezone.now() - timedelta(hours=settings.KAMIWA_PRIVATE_MESSAGE_TTL_HOURS)
    if message.created_at < cutoff or not message.payload_ciphertext:
        raise PermissionDenied('مهلت نگهداری این پیام آزمایشی تمام شده است.')
    return decrypt_payload(message.payload_ciphertext)
