"""Loopback-only visual preview; fictional data, memory database, no external providers."""

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.update(
    {
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DATABASE_URL": "sqlite:///:memory:",
        "DJANGO_SECRET_KEY": "local-stage-nine-visual-preview-only",
        "DJANGO_DEBUG": "true",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        "REDIS_URL": "",
        "CACHE_URL": "",
        "CELERY_BROKER_URL": "memory://",
        "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
        "LOYALTY_ENABLED": "true",
        "LOYALTY_UI_ENABLED": "true",
        "LOYALTY_UI_PREVIEW": "true",
        "LOYALTY_LAUNCH_AT": "2026-10-01T00:00:00+03:00",
        "LOYALTY_TERMS_APPROVED": "false",
    }
)


def reject_network(*args, **kwargs):
    raise RuntimeError("Outbound network is disabled in this fictional preview.")


socket.socket.connect = reject_network
socket.socket.connect_ex = reject_network
socket.create_connection = reject_network

import django  # noqa: E402

django.setup()
from datetime import UTC, datetime, timedelta  # noqa: E402
from wsgiref.simple_server import make_server  # noqa: E402

from django.conf import settings  # noqa: E402
from django.contrib.auth import login  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.wsgi import get_wsgi_application  # noqa: E402
from django.shortcuts import redirect  # noqa: E402
from django.urls import path  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.loyalty.services import reconcile, transfer  # noqa: E402
from config import urls  # noqa: E402

# These helpers create only synthetic accounts and fictional provider references.
from tests.test_loyalty_stage8 import booking, completed_inviter, join, user  # noqa: E402

# Only this disposable preview reloads templates; production keeps its cache.
settings.TEMPLATES[0]["APP_DIRS"] = False
settings.TEMPLATES[0]["OPTIONS"]["loaders"] = [
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]

call_command("migrate", verbosity=0, interactive=False)
clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
timezone.now = lambda: clock[0]
a, b, c, d, new = [user(n) for n in range(1, 6)]
for u, name in zip([a, b, c, d, new], ["أحمد", "خالد", "سارة", "عمر", "نورة"], strict=False):
    u.first_name = name
    u.save()
am, token = completed_inviter(a, clock)
bm = join(b, invitation_token=token, confirmed_inviter=am.pk)
br = booking(b, "950.00")
reconcile(br.pk)
clock[0] += timedelta(days=3)
from apps.loyalty.services import issue_invite  # noqa: E402

bt = issue_invite(b, br.pk)
cm, dm = [join(u, invitation_token=bt, confirmed_inviter=bm.pk) for u in [c, d]]
reconcile(booking(c, "850.00").pk)
reconcile(booking(d, "650.00").pk)
transfer(a, "PREVIEW-TRANSFER-20", dm.number, "20.00")
admin = user("owner")
admin.first_name = "الإدارة"
admin.is_staff = admin.is_superuser = True
admin.save()


def switch(request, role):
    identities = {"guest": a, "new": new, "owner": admin, "checkout": a}
    login(request, identities[role], backend="django.contrib.auth.backends.ModelBackend")
    if role == "checkout":
        from tests.test_hyperpay import payable_intent

        intent = payable_intent()
        intent.customer = a
        intent.language = "ar"
        intent.save()
        return redirect("reservations:intent_detail", public_reference=intent.public_reference)
    return redirect("loyalty:tree" if role == "owner" else "accounts:rewards")


urls.urlpatterns.insert(0, path("__fictional_preview__/<str:role>/", switch))
settings.ROOT_URLCONF = urls
# One process/one thread keeps the memory database and disposable session together.
application = get_wsgi_application()
from django.contrib.staticfiles.handlers import StaticFilesHandler  # noqa: E402

print(
    "LOCAL_FICTIONAL_PREVIEW_READY http://127.0.0.1:8169/__fictional_preview__/guest/", flush=True
)
with make_server("127.0.0.1", 8169, StaticFilesHandler(application)) as server:
    server.serve_forever()
