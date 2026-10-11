"""Database-level history protection; never modifies roles or permissions."""

from django.db import migrations

PG_SQL = """
CREATE FUNCTION loyalty_reject_history_edit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Loyalty history is append-only' USING ERRCODE='23514'; END $$;
CREATE TRIGGER loyalty_ledger_immutable BEFORE UPDATE OR DELETE ON loyalty_ledger
FOR EACH ROW EXECUTE FUNCTION loyalty_reject_history_edit();
CREATE TRIGGER loyalty_ledger_no_truncate BEFORE TRUNCATE ON loyalty_ledger
FOR EACH STATEMENT EXECUTE FUNCTION loyalty_reject_history_edit();
CREATE TRIGGER loyalty_referral_immutable BEFORE UPDATE OR DELETE ON loyalty_referral
FOR EACH ROW EXECUTE FUNCTION loyalty_reject_history_edit();
CREATE TRIGGER loyalty_referral_no_truncate BEFORE TRUNCATE ON loyalty_referral
FOR EACH STATEMENT EXECUTE FUNCTION loyalty_reject_history_edit();
CREATE FUNCTION loyalty_validate_posting() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE program numeric; personal numeric; last_sequence bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(190009,8);
    SELECT COALESCE(SUM(credit-debit),0), COALESCE(MAX(sequence),0)
      INTO program,last_sequence FROM loyalty_ledger;
    SELECT COALESCE(SUM(credit-debit),0) INTO personal FROM loyalty_ledger
      WHERE member_id=NEW.member_id;
    IF NEW.sequence<>last_sequence+1 OR NEW.program_balance_after<>program+NEW.credit-NEW.debit
      OR NEW.member_balance_after<>personal+NEW.credit-NEW.debit THEN
        RAISE EXCEPTION 'Loyalty running balance mismatch' USING ERRCODE='23514';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM loyalty_member m JOIN loyalty_referral r ON r.member_id=m.id
      WHERE m.id=NEW.member_id AND m.customer_id=NEW.customer_id
        AND m.number=NEW.membership_number AND r.tree_number=NEW.tree_number) THEN
        RAISE EXCEPTION 'Loyalty account snapshot mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER loyalty_posting_validate BEFORE INSERT ON loyalty_ledger
FOR EACH ROW EXECUTE FUNCTION loyalty_validate_posting();
CREATE FUNCTION loyalty_validate_transfer() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE pair_count bigint;
BEGIN
    IF NEW.event_type<>'transfer' THEN RETURN NULL; END IF;
    SELECT COUNT(*) INTO pair_count FROM loyalty_ledger x JOIN loyalty_ledger y
      ON x.operation_group=y.operation_group AND x.id<>y.id
      WHERE x.operation_group=NEW.operation_group AND x.event_type='transfer'
        AND y.event_type='transfer' AND x.member_id=y.counterpart_id
        AND x.counterpart_id=y.member_id AND x.debit=y.credit AND x.debit>0
        AND x.credit=0 AND y.debit=0;
    IF pair_count<>1 OR (SELECT COUNT(*) FROM loyalty_ledger
        WHERE operation_group=NEW.operation_group)<>2 THEN
        RAISE EXCEPTION 'Both transfer sides must commit together' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER loyalty_transfer_pair AFTER INSERT ON loyalty_ledger
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION loyalty_validate_transfer();
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PG_SQL)
    elif schema_editor.connection.vendor == "sqlite":
        for table in ("loyalty_ledger", "loyalty_referral"):
            for action in ("UPDATE", "DELETE"):
                schema_editor.execute(f"CREATE TRIGGER {table}_{action.lower()}_immutable "
                    f"BEFORE {action} ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'Loyalty history is append-only'); END;")


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("""
          DROP TRIGGER loyalty_ledger_immutable ON loyalty_ledger;
          DROP TRIGGER loyalty_ledger_no_truncate ON loyalty_ledger;
          DROP TRIGGER loyalty_referral_immutable ON loyalty_referral;
          DROP TRIGGER loyalty_referral_no_truncate ON loyalty_referral;
          DROP TRIGGER loyalty_posting_validate ON loyalty_ledger;
          DROP TRIGGER loyalty_transfer_pair ON loyalty_ledger;
          DROP FUNCTION loyalty_reject_history_edit();
          DROP FUNCTION loyalty_validate_posting();
          DROP FUNCTION loyalty_validate_transfer();
        """)
    elif schema_editor.connection.vendor == "sqlite":
        for table in ("loyalty_ledger", "loyalty_referral"):
            for action in ("update", "delete"):
                schema_editor.execute(f"DROP TRIGGER {table}_{action}_immutable;")


class Migration(migrations.Migration):
    dependencies = [("loyalty", "0001_initial")]
    operations = [migrations.RunPython(install, uninstall)]
