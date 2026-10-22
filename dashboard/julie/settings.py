"""Application and server configuration. Secrets are required outside tests."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TESTING = os.environ.get("JULIE_TESTING") == "1"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or ("test-only" if TESTING else "")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY is required")
DEBUG = False
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "library",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "library.onboarding.InitialSetupMiddleware",
    "library.backup_guard.BackupGuardMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "julie.urls"
WSGI_APPLICATION = "julie.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "library.reset_context.maintenance",
                "library.onboarding.identity",
            ]
        },
    }
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "julie"),
        "USER": os.environ.get("POSTGRES_USER", "julie"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 30,
    }
}
if TESTING:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("JULIE_TEST_DB", str(BASE_DIR / "test.sqlite3")),
        }
    }
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LANGUAGE_CODE = "it"
TIME_ZONE = "Europe/Rome"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
MEDIA_ROOT = Path(os.environ.get("JULIE_DATA_ROOT", "/data"))
MAX_AUDIO_BYTES = 1024 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 20
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "library:index"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
NORMALIZE_TARGET_LUFS = float(os.environ.get("JULIE_TARGET_LUFS", "-20"))
NORMALIZE_CEILING_DBTP = float(os.environ.get("JULIE_CEILING_DBTP", "-1.5"))

PLAYOUT_MEDIA_ROOT = "/audio/library"

PLAYOUT_HOST = os.environ.get("JULIE_PLAYOUT_HOST", "liquidsoap")
PLAYOUT_PORT = int(os.environ.get("JULIE_PLAYOUT_PORT", "1234"))

ICECAST_STATS_URL = os.environ.get(
    "JULIE_ICECAST_STATS_URL", "http://icecast:8000/status-json.xsl"
)
RADIO_STREAM_URL = os.environ.get("JULIE_STREAM_URL", "/stream.mp3")
RADIO_MOUNT = "/stream.mp3"

BACKUP_ROOT = Path(
    os.environ.get(
        "JULIE_BACKUP_ROOT",
        str(MEDIA_ROOT / "backup-exports") if TESTING else "/backups",
    )
)
BACKUP_CONFIG_ROOT = Path(os.environ.get("JULIE_BACKUP_CONFIG_ROOT", "/config-source"))
BACKUP_CODE_ROOT = Path(os.environ.get("JULIE_BACKUP_CODE_ROOT", str(BASE_DIR)))

MAX_RESTORE_BYTES = int(os.environ.get("JULIE_MAX_RESTORE_BYTES", str(20 * 1024**3)))
FILE_UPLOAD_HANDLERS = [
    "library.restore_upload.RestoreUploadLimit",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]

# Enable proxy trust only when the app is reachable exclusively through a proxy
# which overwrites X-Forwarded-Proto. Public URL form fields do not alter trust.
REGIA_URL = os.environ.get("JULIE_REGIA_URL", "")
PUBLIC_API_URL = os.environ.get("JULIE_PUBLIC_API_URL", "")
CSRF_TRUSTED_ORIGINS = [
    value.strip()
    for value in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if value.strip()
]
if os.environ.get("JULIE_TRUST_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
if os.environ.get("JULIE_HTTPS_ONLY") == "1":
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r"^health/$"]
