import io
import json
import time
from datetime import date, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pyotp
from PIL import Image
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from market import jobs, security
from market.files import PrivateStorage, authorized_private_file, sanitize_image, validate_private_file
from market.models import Account, AuditEvent, CredentialRotation, OTP, OutboxMessage, Province
from market.providers import decrypt_payload, encrypt_payload, queue_message, read_test_message


class SecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.province = Province.objects.create(name='تهران')
        cls.user = Account.objects.create_user('securitybuyer', email='securitybuyer@example.test',
            mobile='09120000001', password='TestSecurePassword92', role='buyer')
        cls.other = Account.objects.create_user('otherbuyer', email='otherbuyer@example.test',
            mobile='09120000002', password='TestSecurePassword92', role='buyer')
        cls.admin = Account.objects.create_superuser('securityadmin', email='admin@example.test',
            password='TestSecurePassword92', role='staff')

    def setUp(self):
        cache.clear()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings_override = override_settings(PRIVATE_MEDIA_ROOT=self.temp.name,
            MEDIA_ROOT=self.temp.name + '/public', KAMIWA_TEST_PROVIDERS=True,
            STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                      'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def image(self):
        buffer = io.BytesIO()
        exif = Image.Exif()
        exif[270] = 'private metadata'
        Image.new('RGB', (80, 80), 'white').save(buffer, 'JPEG', exif=exif)
        return SimpleUploadedFile('identity.jpg', buffer.getvalue(), content_type='image/jpeg')

    def test_registration_rejects_role_escalation_and_invalid_credentials(self):
        with self.assertRaises(ValidationError):
            security.register_account({'role': 'staff'})
        with self.assertRaises(ValidationError):
            security.register_account({'username': 'user_12', 'password': 'ValidPassword39'})
        for value in ('shortA1', 'lowercaseonly8', 'UPPERCASEONLY8', 'NoDigitsHere'):
            with self.assertRaises(ValidationError):
                security.RequiredPasswordValidator().validate(value)

    def test_registration_stores_document_without_public_url_or_metadata(self):
        user = security.register_account(dict(username='validnewuser', password1='AValidNewPassword98', role='buyer',
            first_name='نام', last_name='آزمایشی', email='newuser@example.test', mobile='09120000003',
            national_id='0123456789', birth_date=date(1990, 1, 1), province=self.province, city='تهران', identity_document=self.image()))
        self.assertEqual(user.username, 'validnewuser')
        self.assertTrue(10000 <= user.internal_code <= 99999)
        self.assertTrue(user.check_password('AValidNewPassword98'))
        self.assertFalse(user.is_staff)
        with self.assertRaises(ValueError):
            _ = user.identity_document.url
        with user.identity_document.open('rb') as source:
            self.assertEqual(len(Image.open(source).getexif()), 0)

    def test_upload_rejects_disguised_script_and_strips_image_exif(self):
        with self.assertRaises(ValidationError):
            validate_private_file(SimpleUploadedFile('photo.jpg', b'<script>alert(1)</script>'))
        with self.assertRaises(ValidationError):
            validate_private_file(SimpleUploadedFile('fake.pdf', b'not a pdf'))
        clean = sanitize_image(self.image())
        self.assertEqual(len(Image.open(clean).getexif()), 0)

    def test_private_document_authorization(self):
        self.user.identity_document.save('proof.jpg', self.image())
        self.assertEqual(authorized_private_file(self.user, 'identity', self.user.pk).name, self.user.identity_document.name)
        with self.assertRaises(PermissionDenied):
            authorized_private_file(self.other, 'identity', self.user.pk)
        self.assertTrue(authorized_private_file(self.admin, 'identity', self.user.pk))
        self.admin.restricted_until = timezone.now() + timedelta(hours=1)
        self.admin.save(update_fields=['restricted_until'])
        with self.assertRaises(PermissionDenied):
            authorized_private_file(self.admin, 'identity', self.user.pk)

    def test_otp_attempt_limit_and_ciphertext(self):
        otp = security.issue_otp(self.user, 'email')
        message = OutboxMessage.objects.get(idempotency_key=f'otp:{otp.pk}')
        code = decrypt_payload(message.payload_ciphertext)['code']
        self.assertNotIn(code, message.payload_ciphertext)
        self.assertNotEqual(code, otp.code_hash)
        for unused in range(5):
            self.assertFalse(security.consume_otp(self.user, 'email', 'wrong'))
        self.assertFalse(security.consume_otp(self.user, 'email', code))
        otp.refresh_from_db()
        self.assertEqual(otp.attempts, 5)

    def test_otp_resend_cooldown_and_single_use(self):
        otp = security.issue_otp(self.user, 'sms')
        with self.assertRaises(ValidationError):
            security.issue_otp(self.user, 'sms')
        code = decrypt_payload(OutboxMessage.objects.get(idempotency_key=f'otp:{otp.pk}').payload_ciphertext)['code']
        self.assertTrue(security.consume_otp(self.user, 'sms', code))
        self.assertFalse(security.consume_otp(self.user, 'sms', code))
        self.user.refresh_from_db()
        self.assertTrue(self.user.mobile_verified)
        self.assertFalse(self.user.email_verified)

    def test_otp_expiry(self):
        otp = security.issue_otp(self.user, 'email')
        OTP.objects.filter(pk=otp.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        code = decrypt_payload(OutboxMessage.objects.get(idempotency_key=f'otp:{otp.pk}').payload_ciphertext)['code']
        self.assertFalse(security.consume_otp(self.user, 'email', code))

    def test_mfa_setup_and_replay_protection(self):
        setup = security.begin_mfa_setup(self.admin)
        moment = (int(time.time()) // 30) * 30 + 5
        totp = pyotp.TOTP(setup['secret'])
        with patch('market.security.time.time', return_value=moment):
            self.assertTrue(security.confirm_mfa_setup(self.admin, setup['secret'], totp.at(moment)))
            self.assertFalse(security.check_mfa(self.admin, totp.at(moment)))
        with patch('market.security.time.time', return_value=moment + 30):
            self.assertTrue(security.check_mfa(self.admin, totp.at(moment + 30)))
            self.assertFalse(security.check_mfa(self.admin, totp.at(moment + 30)))
        self.assertNotIn(setup['secret'], self.admin.mfa_secret_encrypted)

    def test_unenrolled_admin_has_direct_access_but_enrolled_admin_requires_mfa(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/administration/').status_code, 200)
        self.admin.mfa_secret_encrypted = encrypt_payload({'secret': pyotp.random_base32()})
        self.admin.save(update_fields=['mfa_secret_encrypted'])
        self.assertRedirects(self.client.get('/administration/'), reverse('market:mfa_challenge'), fetch_redirect_response=False)

    def test_password_reset_invalidates_existing_sessions(self):
        self.client.force_login(self.user)
        session = self.client.session
        session['session_version'] = self.user.session_version
        session.save()
        otp = security.issue_otp(self.user, 'email', purpose='password_reset')
        code = decrypt_payload(OutboxMessage.objects.get(idempotency_key=f'otp:{otp.pk}').payload_ciphertext)['code']
        self.assertTrue(security.reset_password(self.user, code, 'AnotherValidSecret58'))
        response = self.client.get(reverse('market:dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_admin_email_change_requires_reauth_mfa_and_new_email_code(self):
        with self.assertRaises(PermissionDenied):
            security.begin_admin_email_change(self.user, 'new@example.test', 'bad', '000000')
        secret = pyotp.random_base32()
        self.admin.mfa_secret_encrypted = encrypt_payload({'secret': secret})
        self.admin.save(update_fields=['mfa_secret_encrypted'])
        otp = security.begin_admin_email_change(self.admin, 'newadmin@example.test', 'TestSecurePassword92', pyotp.TOTP(secret).now())
        with self.assertRaises(PermissionDenied):
            security.complete_admin_email_change(self.admin, 'newadmin@example.test', 'wrong', 'TestSecurePassword92')
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, 'admin@example.test')
        code = decrypt_payload(OutboxMessage.objects.get(idempotency_key=f'otp:{otp.pk}').payload_ciphertext)['code']
        with patch('market.security.time.time', return_value=time.time() + 30):
            fresh_code = pyotp.TOTP(secret).at(time.time())
            self.assertTrue(security.complete_admin_email_change(self.admin, 'newadmin@example.test', code, 'TestSecurePassword92', fresh_code))
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, 'newadmin@example.test')

    def test_unenrolled_admin_email_change_requires_password_and_new_email_otp(self):
        self.client.force_login(self.admin)
        url = reverse('market:admin_email_change')
        response = self.client.get(url)
        self.assertNotIn('mfa_code', response.context['form'].fields)
        data = {'email': 'newadmin@example.test', 'password': 'WrongPassword71'}
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertFalse(OTP.objects.filter(purpose='admin_email_change').exists())
        data['password'] = 'TestSecurePassword92'
        self.assertEqual(self.client.post(url, data).status_code, 302)
        otp = OTP.objects.get(user=self.admin, purpose='admin_email_change')
        code = decrypt_payload(OutboxMessage.objects.get(idempotency_key=f'otp:{otp.pk}').payload_ciphertext)['code']
        data['email_code'] = 'wrong'
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.admin.refresh_from_db()
        self.assertNotEqual(self.admin.email, data['email'])
        data['email_code'] = code
        data['password'] = 'WrongPassword71'
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.admin.refresh_from_db()
        self.assertNotEqual(self.admin.email, data['email'])
        data['password'] = 'TestSecurePassword92'
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, data['email'])
        self.assertTrue(self.admin.email_verified)

    @override_settings(KAMIWA_ADMIN_ROTATION=True)
    def test_rotation_is_idempotent_encrypted_and_revokes_sessions(self):
        now = timezone.now()
        Account.objects.filter(pk=self.admin.pk).update(date_joined=now - timedelta(days=31))
        self.client.force_login(self.admin)
        old_version = self.admin.session_version
        self.assertEqual(jobs.rotate_admin_credentials(now), 1)
        self.assertEqual(jobs.rotate_admin_credentials(now), 0)
        self.admin.refresh_from_db()
        rotation = CredentialRotation.objects.get(user=self.admin)
        payload = decrypt_payload(rotation.outbox.payload_ciphertext)
        self.assertTrue(self.admin.check_password(payload['password']))
        self.assertNotEqual(self.admin.username, 'securityadmin')
        self.assertEqual(self.admin.session_version, old_version + 1)
        self.assertNotIn(payload['password'], rotation.outbox.payload_ciphertext)
        self.assertFalse(AuditEvent.objects.filter(after__icontains=payload['password']).exists())
        self.assertEqual(jobs.deliver_outbox(now=now + timedelta(seconds=5)), 1)
        self.assertEqual(jobs.deliver_outbox(now=now + timedelta(seconds=6)), 0)
        self.client.get(reverse('market:dashboard'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_delivery_retry_and_secret_expiration(self):
        now = timezone.now()
        message = queue_message('email', self.user.email, {'simulation': 'failure', 'body': 'test'})
        self.assertEqual(jobs.deliver_outbox(now=now + timedelta(seconds=1)), 0)
        message.refresh_from_db()
        self.assertEqual(message.status, 'failed')
        self.assertEqual(message.attempts, 1)
        self.assertEqual(jobs.deliver_outbox(now=now + timedelta(seconds=2)), 0)
        message.payload_ciphertext = encrypt_payload({'body': 'test'})
        message.save(update_fields=['payload_ciphertext'])
        self.assertEqual(jobs.deliver_outbox(now=message.next_attempt_at + timedelta(seconds=1)), 1)
        OutboxMessage.objects.filter(pk=message.pk).update(created_at=now - timedelta(hours=25))
        self.assertEqual(jobs.purge_expired_secrets(now), 1)
        message.refresh_from_db()
        self.assertEqual(message.payload_ciphertext, '')
        with self.assertRaises(PermissionDenied):
            read_test_message(self.user, message)

    def test_outbox_is_private_and_provider_disable_is_closed(self):
        message = queue_message('email', self.user.email, {'body': 'test private message'})
        self.assertEqual(read_test_message(self.user, message)['body'], 'test private message')
        with self.assertRaises(PermissionDenied):
            read_test_message(self.other, message)
        with override_settings(KAMIWA_TEST_PROVIDERS=False):
            with self.assertRaises(PermissionDenied):
                read_test_message(self.user, message)

    def test_private_download_keeps_sandbox_and_no_store_headers(self):
        self.user.identity_document.save('identity.jpg', self.image())
        self.client.force_login(self.user)
        response = self.client.get(reverse('market:private_file', args=['identity', self.user.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('sandbox', response['Content-Security-Policy'])
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn('no-store', response['Cache-Control'])
        # Consume through the test client's streaming wrapper, which disconnects
        # request_finished connection cleanup around closing the response.
        self.assertTrue(b''.join(response.streaming_content))
