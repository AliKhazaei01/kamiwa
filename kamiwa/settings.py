import os
from pathlib import Path
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
DEBUG = os.environ.get('DJANGO_DEBUG', '0') == '1'
TESTING = 'test' in __import__('sys').argv
if not SECRET_KEY:
    if TESTING:
        SECRET_KEY = 'test-only-key-never-used-for-deployment'
    else:
        raise RuntimeError('DJANGO_SECRET_KEY must be configured')
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver').split(',')
INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'django.contrib.humanize', 'captcha', 'market',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'market.security.AccountSessionMiddleware',
    'market.rls.RowSecurityMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'market.security.SecurityHeadersMiddleware',
]
ROOT_URLCONF = 'kamiwa.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.debug', 'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages',
        'market.context_processors.common',
    ]},
}]
WSGI_APPLICATION = 'kamiwa.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'db.sqlite3', 'OPTIONS': {'timeout': 20}}}
db_url = os.environ.get('DATABASE_URL')
if db_url:
    db = urlparse(db_url)
    DATABASES = {'default': {
        'ENGINE': 'django.db.backends.postgresql', 'NAME': db.path.lstrip('/'),
        'USER': unquote(db.username or ''), 'PASSWORD': unquote(db.password or ''),
        'HOST': db.hostname or '', 'PORT': db.port or 5432,
        # Reuse connections across requests: each new PostgreSQL connection forks a backend.
        'CONN_MAX_AGE': int(os.environ.get('DATABASE_CONN_MAX_AGE', '60')),
        'CONN_HEALTH_CHECKS': True,
    }}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'market.Account'
AUTHENTICATION_BACKENDS = ['market.security.CaseInsensitiveBackend']
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'market.security.RequiredPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
]
LANGUAGE_CODE = 'fa'
LANGUAGES = [('fa', 'فارسی')]
TIME_ZONE = 'Asia/Tehran'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
PRIVATE_MEDIA_ROOT = BASE_DIR / 'private_media'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}
LOGIN_URL = 'market:login'
LOGIN_REDIRECT_URL = 'market:home'
LOGOUT_REDIRECT_URL = 'market:landing'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'
PUBLIC_HTTPS = os.environ.get('PUBLIC_HTTPS', '0') == '1'
SESSION_COOKIE_SECURE = PUBLIC_HTTPS
CSRF_COOKIE_SECURE = PUBLIC_HTTPS
SECURE_SSL_REDIRECT = PUBLIC_HTTPS
SECURE_HSTS_SECONDS = 31536000 if PUBLIC_HTTPS else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = PUBLIC_HTTPS
SECURE_HSTS_PRELOAD = PUBLIC_HTTPS
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_TRUSTED_ORIGINS = [x for x in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if x]
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_PERMISSIONS = 0o644
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o755
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 12
CAPTCHA_LENGTH = 5
CAPTCHA_TIMEOUT = 5
CAPTCHA_NOISE_FUNCTIONS = ('captcha.helpers.noise_dots',)
CAPTCHA_TEST_MODE = False
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
KAMIWA_TEST_PROVIDERS = os.environ.get('KAMIWA_TEST_PROVIDERS', '1') == '1'
KAMIWA_OUTBOX_KEY = os.environ.get('KAMIWA_OUTBOX_KEY', '')
KAMIWA_ADMIN_EMAIL = os.environ.get('KAMIWA_ADMIN_EMAIL', 'admin@example.com')
KAMIWA_ROTATION_DAYS = 30
# Rotated credentials are only delivered by email; with test providers that is a 24 h outbox
# message nobody can read without the old credentials, so rotation stays off until real email exists.
KAMIWA_ADMIN_ROTATION = os.environ.get('KAMIWA_ADMIN_ROTATION', '0' if KAMIWA_TEST_PROVIDERS else '1') == '1'
KAMIWA_PRIVATE_MESSAGE_TTL_HOURS = 24
PASSWORD_RESET_TIMEOUT = 30 * 60
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.db.DatabaseCache', 'LOCATION': 'kamiwa_cache', 'TIMEOUT': 300}}
if TESTING:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
LOGGING = {
    'version': 1, 'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {'django.security': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False}},
}
