from importlib import import_module

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def posting_function():
    original = import_module("apps.loyalty.migrations.0002_history_guards").PG_SQL
    function = "CREATE OR REPLACE FUNCTION loyalty_validate_posting()" + original.split(
        "CREATE FUNCTION loyalty_validate_posting()", 1
    )[1].split("CREATE TRIGGER loyalty_posting_validate", 1)[0]
    return function


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(posting_function().replace(
            "r.tree_number=NEW.tree_number",
            "COALESCE((SELECT v.tree_number FROM loyalty_referralrevision v "
            "WHERE v.member_id=m.id ORDER BY v.id DESC LIMIT 1),r.tree_number)=NEW.tree_number"
        ))
        schema_editor.execute("""
            CREATE TRIGGER loyalty_revision_immutable BEFORE UPDATE OR DELETE
            ON loyalty_referralrevision FOR EACH ROW EXECUTE FUNCTION loyalty_reject_history_edit();
            CREATE TRIGGER loyalty_revision_no_truncate BEFORE TRUNCATE
            ON loyalty_referralrevision FOR EACH STATEMENT EXECUTE FUNCTION loyalty_reject_history_edit();
        """)
    elif schema_editor.connection.vendor == "sqlite":
        for action in ("UPDATE", "DELETE"):
            schema_editor.execute(
                f"CREATE TRIGGER loyalty_revision_{action.lower()}_immutable "
                f"BEFORE {action} ON loyalty_referralrevision BEGIN "
                "SELECT RAISE(ABORT, 'Loyalty history is append-only'); END;"
            )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(posting_function())
        schema_editor.execute("DROP TRIGGER loyalty_revision_immutable ON loyalty_referralrevision;")
        schema_editor.execute("DROP TRIGGER loyalty_revision_no_truncate ON loyalty_referralrevision;")
    elif schema_editor.connection.vendor == "sqlite":
        for action in ("update", "delete"):
            schema_editor.execute(f"DROP TRIGGER loyalty_revision_{action}_immutable;")


class Migration(migrations.Migration):
    dependencies = [
        ("loyalty", "0002_history_guards"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="ReferralRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tree_number", models.TextField()),
                ("depth", models.PositiveIntegerField()),
                ("branch_order", models.PositiveBigIntegerField()),
                ("operation", models.UUIDField()),
                ("reason", models.CharField(max_length=500)),
                ("fixed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("member", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="loyalty.member")),
                ("inviter", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="revised_invitees", to="loyalty.member")),
                ("root", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="revised_tree", to="loyalty.member")),
                ("linked_member", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="link_history", to="loyalty.member")),
                ("operator", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["member", "-id"], name="loyalty_current_referral")],
                "constraints": [
                    models.UniqueConstraint(fields=("operation", "member"), name="loyalty_link_once"),
                    models.CheckConstraint(condition=~models.Q(inviter=models.F("member")), name="loyalty_revision_no_self"),
                ],
            },
        ),
        migrations.RunPython(install, uninstall),
    ]
