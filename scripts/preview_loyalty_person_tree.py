"""Loopback-only people-tree preview: fictional identities, memory DB, blocked network."""

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
        "DJANGO_SECRET_KEY": "fictional-person-tree-preview-not-for-deployment",
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
    raise RuntimeError("No outbound connections in the fictional tree preview.")


socket.socket.connect = reject_network
socket.socket.connect_ex = reject_network
socket.create_connection = reject_network

import django  # noqa: E402

django.setup()
from datetime import timedelta  # noqa: E402
from wsgiref.simple_server import make_server  # noqa: E402

from django.conf import settings  # noqa: E402
from django.contrib.auth import login  # noqa: E402
from django.contrib.staticfiles.handlers import StaticFilesHandler  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.wsgi import get_wsgi_application  # noqa: E402
from django.shortcuts import redirect  # noqa: E402
from django.urls import path  # noqa: E402
from django.utils import timezone  # noqa: E402

from config import urls  # noqa: E402
from tests.test_loyalty_stage8 import join, user  # noqa: E402

settings.TEMPLATES[0]["APP_DIRS"] = False
settings.TEMPLATES[0]["OPTIONS"]["loaders"] = [
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]
call_command("migrate", verbosity=0, interactive=False)
names = ["أحمد", "خالد", "عمر", "محمد", "نورة", "سارة", "علي", "عبدالله"]
members = []
for i in range(31):
    guest = user(f"person-preview-{i}")
    guest.first_name = names[i % len(names)]
    guest.last_name = f"تجريبي {i + 1}"
    guest.save()
    parent = members[(i - 1) // 2] if i else None
    member = join(
        guest,
        **(
            {"invitation_token": parent.invitation_token, "confirmed_inviter": parent.pk}
            if parent
            else {}
        ),
    )
    member.invitation_token = f"fictional-person-preview-{i}"
    member.first_completed_at = timezone.now() - timedelta(days=1)
    member.save(update_fields=["invitation_token", "first_completed_at"])
    members.append(member)
owner = user("person-preview-owner")
owner.is_staff = owner.is_superuser = True
owner.save()


def switch(request):
    login(request, owner, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("loyalty:tree")


urls.urlpatterns.insert(0, path("__fictional_people__/", switch))
settings.ROOT_URLCONF = urls
application = StaticFilesHandler(get_wsgi_application())
print("FICTIONAL_PEOPLE_READY http://127.0.0.1:8170/__fictional_people__/", flush=True)
with make_server("127.0.0.1", 8170, application) as server:
    server.serve_forever()
