from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


def test_disabled_bridge_does_not_require_provider(settings):
    settings.MORNING07_N8N_ENABLED = False
    settings.ULTRAMSG_TOKEN = ""
    out = StringIO()
    call_command("check_morning07_readiness", stdout=out)
    assert "disabled" in out.getvalue()


def test_enabled_bridge_rejects_missing_provider_without_disclosing_values(settings):
    settings.MORNING07_N8N_ENABLED = True
    settings.MORNING07_N8N_WEBHOOK_URL = "https://lsa25.app.n8n.cloud/webhook/lsa07-morning-stay-v1"
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = "synthetic-instance"
    settings.ULTRAMSG_TOKEN = ""
    settings.CELERY_BROKER_URL = "redis://synthetic.invalid/0"
    with pytest.raises(CommandError, match="whatsapp_token_configured") as error:
        call_command("check_morning07_readiness")
    assert "synthetic-instance" not in str(error.value)


def test_enabled_bridge_reports_only_configuration_presence(settings):
    settings.MORNING07_N8N_ENABLED = True
    settings.MORNING07_N8N_WEBHOOK_URL = "https://lsa25.app.n8n.cloud/webhook/lsa07-morning-stay-v1"
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = "synthetic-instance"
    settings.ULTRAMSG_TOKEN = "synthetic-token"
    settings.CELERY_BROKER_URL = "redis://synthetic.invalid/0"
    out = StringIO()
    call_command("check_morning07_readiness", stdout=out)
    assert "ready; event-only" in out.getvalue()
    assert "synthetic" not in out.getvalue()
