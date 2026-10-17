"""Append old deferred corrections; never rewrite financial history or send notices."""

from decimal import Decimal, InvalidOperation

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
from django.db.models import Max, Sum


def restore_sqlite_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "sqlite":
        for action in ("UPDATE", "DELETE"):
            schema_editor.execute(
                f"CREATE TRIGGER IF NOT EXISTS loyalty_ledger_{action.lower()}_immutable "
                f"BEFORE {action} ON loyalty_ledger BEGIN "
                "SELECT RAISE(ABORT, 'Loyalty history is append-only'); END;"
            )


def reverse_empty_only(apps, schema_editor):
    if apps.get_model("loyalty", "Ledger").objects.using(schema_editor.connection.alias).exists():
        raise IrreversibleError("A populated append-only ledger cannot be downgraded")


def materialize_deferred(apps, schema_editor):
    alias = schema_editor.connection.alias
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SELECT pg_advisory_xact_lock(190009, 8)")
    Ledger = apps.get_model("loyalty", "Ledger")
    Member = apps.get_model("loyalty", "Member")
    Referral = apps.get_model("loyalty", "Referral")
    Revision = apps.get_model("loyalty", "ReferralRevision")
    ledger = Ledger.objects.using(alias)
    totals = ledger.aggregate(c=Sum("credit"), d=Sum("debit"), n=Max("sequence"))
    program = (totals["c"] or Decimal(0)) - (totals["d"] or Decimal(0))
    sequence = totals["n"] or 0
    for member in Member.objects.using(alias).exclude(deferred_corrections=[]).order_by("pk"):
        sums = ledger.filter(member_id=member.pk).aggregate(c=Sum("credit"), d=Sum("debit"))
        personal = (sums["c"] or Decimal(0)) - (sums["d"] or Decimal(0))
        revision = Revision.objects.using(alias).filter(member_id=member.pk).order_by("-pk").first()
        tree = revision or Referral.objects.using(alias).get(member_id=member.pk)
        for index, claim in enumerate(member.deferred_corrections):
            try:
                due = Decimal(claim["remaining"])
                original = ledger.get(
                    pk=claim["original"],
                    member_id=member.pk,
                    reservation_id=claim["booking"],
                    component=claim["component"],
                    event_type="earn",
                )
                if not due.is_finite() or due <= 0 or due.quantize(Decimal("0.01")) != due:
                    raise ValueError
                earned = ledger.filter(
                    member_id=member.pk,
                    reservation_id=original.reservation_id,
                    component=original.component,
                    event_type__in=["earn", "adjust"],
                ).aggregate(c=Sum("credit"), d=Sum("debit"))
                if due > (earned["c"] or Decimal(0)) - (earned["d"] or Decimal(0)):
                    raise ValueError
            except (KeyError, InvalidOperation, ValueError, Ledger.DoesNotExist) as exc:
                raise RuntimeError("Invalid legacy loyalty correction; migration aborted") from exc
            sequence += 1
            personal -= due
            program -= due
            group = f"LSA{sequence:06}"
            ledger.create(
                sequence=sequence,
                operation_number=f"{group}-01",
                operation_group=group,
                branch=1,
                member_id=member.pk,
                customer_id=member.customer_id,
                membership_number=member.number,
                tree_number=tree.tree_number,
                account_name=original.account_name,
                reservation_id=original.reservation_id,
                property_id=original.property_id,
                booking_total=original.booking_total,
                booking_customer_id=original.booking_customer_id,
                booking_guest_name=original.booking_guest_name,
                reservation_reference=original.reservation_reference,
                property_name=original.property_name,
                rate_percent=original.rate_percent,
                credit=Decimal(0),
                debit=due,
                member_balance_after=personal,
                program_balance_after=program,
                original_id=original.pk,
                event_type="adjust",
                operation_type=original.operation_type,
                component=original.component,
                event_reference=f"signed-balance-migration:{member.pk}:{index}",
                rules_version="loyalty-signed-balance-20261017-v2",
                executed_by="system",
                reason="إظهار فرق التصحيح السابق في رصيد الحساب بدل تأجيله خارج كشف العمليات",
                evidence={"legacy_deferred_correction": claim},
            )
        Member.objects.using(alias).filter(pk=member.pk).update(deferred_corrections=[])
    # SQLite constraint rebuilds drop its table-local history triggers.
    restore_sqlite_guards(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [("loyalty", "0004_movementnotice")]
    operations = [
        migrations.RunPython(migrations.RunPython.noop, restore_sqlite_guards),
        migrations.RemoveConstraint(model_name="ledger", name="loyalty_nonnegative_balances"),
        migrations.AddConstraint(
            model_name="ledger",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(member_balance_after__gte=0)
                    | models.Q(credit__gt=0)
                    | models.Q(event_type="adjust", original__isnull=False)
                    | models.Q(event_type="redeem")
                ),
                name="loyalty_negative_correction_only",
            ),
        ),
        # Accounting postings are intentionally irreversible, like the ledger itself.
        migrations.RunPython(materialize_deferred, reverse_empty_only),
    ]
