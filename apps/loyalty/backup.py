"""Stage ten: consistent snapshot, fixed LOCAL mirror, guarded recovery rehearsal.

No cloud credentials, public endpoints, provider calls, or permission changes.
The mirror is deliberately local-only until its cloud destination is approved.
"""

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.utils import timezone

VERSION = 1
CORE = ("loyalty.member", "loyalty.referral", "loyalty.ledger")
CONTEXT = (
    "loyalty.guestevent",
    "loyalty.referralrevision",
    "accounts.customerprofile",
    "properties.property",
    "reservations.bookingquote",
    "reservations.manualbookingdraft",
    "reservations.bookingintent",
    "reservations.reservation",
    "reservations.bookingmodificationrequest",
    "reservations.hostawayreservationoperation",
    "reservations.hostawaymodificationoperation",
    "reservations.refundobligation",
    "payments.reservationpaymentrequest",
    "payments.paymentrequestdelivery",
    "payments.offlinepaymentreceipt",
    "payments.manualreceiptreference",
    "payments.paymentattempt",
    "payments.hostawayfinancialentry",
)
# Explicit reverse edges preserve operating receipts/holds, not the whole website.
REVERSE = (
    ("loyalty.referralrevision", "member", "loyalty.member"),
    ("accounts.customerprofile", "user", "user"),
    ("reservations.manualbookingdraft", "quote", "reservations.bookingquote"),
    ("reservations.reservation", "booking_intent", "reservations.bookingintent"),
    ("reservations.bookingmodificationrequest", "reservation", "reservations.reservation"),
    ("reservations.hostawayreservationoperation", "reservation", "reservations.reservation"),
    (
        "reservations.hostawaymodificationoperation",
        "modification_request",
        "reservations.bookingmodificationrequest",
    ),
    ("reservations.refundobligation", "reservation", "reservations.reservation"),
    ("payments.paymentattempt", "booking_intent", "reservations.bookingintent"),
    ("payments.reservationpaymentrequest", "reservation", "reservations.reservation"),
    ("payments.paymentrequestdelivery", "request", "payments.reservationpaymentrequest"),
    ("payments.offlinepaymentreceipt", "reservation", "reservations.reservation"),
    ("payments.manualreceiptreference", "reservation", "reservations.reservation"),
    ("payments.hostawayfinancialentry", "payment_attempt", "payments.paymentattempt"),
)
MAX_ROWS = 100_000
MAX_BYTES = 64 * 1024 * 1024


class BackupError(ValidationError):
    """Stable error code only; never put private row contents in errors/logs."""


def _reject(code):
    raise BackupError("تعذر اعتماد النسخة؛ يلزم فحص سبب التوقف.", code=code)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _models():
    labels = (settings.AUTH_USER_MODEL.lower(), *CONTEXT, *CORE)
    return {label: apps.get_model(label) for label in labels}


def _fields(model):
    return [f.name for f in model._meta.concrete_fields if not f.primary_key]


def _canonical_record(row, models):
    """Django's millisecond serializer emits .000Z, then omits it on reload.

    Normalise only declared datetime fields, never JSON evidence or text. The
    instant is unchanged; snapshot checksum verification still precedes any
    comparisons of historical snapshots.
    """
    fields = dict(row["fields"])
    for field in models[row["model"]]._meta.concrete_fields:
        value = fields.get(field.name)
        if (
            field.get_internal_type() == "DateTimeField"
            and isinstance(value, str)
            and value.endswith(".000Z")
        ):
            fields[field.name] = value[:-5] + "Z"
    return {**row, "fields": fields}


def _schema_hash(
    *, payment_links=True, legacy_link_expiry=False, referral_revisions=True, guest_events=True
):
    # Deconstruction contains callable defaults; field metadata here is stable and JSON-safe.
    return _hash(
        {
            label: [
                (
                    f.name,
                    f.get_internal_type(),
                    (
                        False
                        if legacy_link_expiry
                        and label == "payments.reservationpaymentrequest"
                        and f.name == "expires_at"
                        else f.null
                    ),
                    f.max_length,
                    getattr(f, "decimal_places", None),
                    getattr(f, "max_digits", None),
                    f.related_model._meta.label_lower if f.is_relation else None,
                )
                for f in model._meta.concrete_fields
                if payment_links
                or label != "payments.paymentattempt"
                or f.name != "payment_request"
            ]
            for label, model in _models().items()
            if (referral_revisions or label != "loyalty.referralrevision")
            and (guest_events or label != "loyalty.guestevent")
            and (
                payment_links
                or label
                not in {
                    "payments.reservationpaymentrequest",
                    "payments.paymentrequestdelivery",
                    "payments.offlinepaymentreceipt",
                    "payments.manualreceiptreference",
                }
            )
        }
    )


def capture(*, source_id, using="default"):
    """Read a point-in-time business snapshot; never lock the posting path for upload."""
    if not isinstance(source_id, str) or not 3 <= len(source_id) <= 100:
        _reject("source_identity_required")
    db = connections[using]
    if db.vendor == "postgresql" and db.in_atomic_block:
        _reject("snapshot_requires_own_transaction")
    models = _models()
    user_label = settings.AUTH_USER_MODEL.lower()
    selected = defaultdict(set)

    def add(label, queryset):
        selected[label].update(queryset.values_list("pk", flat=True))
        if sum(map(len, selected.values())) > MAX_ROWS:
            _reject("snapshot_row_limit")

    with transaction.atomic(using=using):
        if db.vendor == "postgresql":
            with db.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        captured_at = timezone.now().isoformat()
        for label in CORE:
            add(label, models[label].objects.using(using).all())
        # Include pre-membership registration/proof events as well as member history.
        add("loyalty.guestevent", models["loyalty.guestevent"].objects.using(using).all())
        members = models["loyalty.member"].objects.using(using)
        member_users = set(members.values_list("customer_id", flat=True))
        add(
            "reservations.bookingintent",
            models["reservations.bookingintent"]
            .objects.using(using)
            .filter(customer_id__in=member_users),
        )
        # Closure follows every concrete FK and only explicitly approved reverse edges.
        while True:
            before = sum(map(len, selected.values()))
            for label, model in models.items():
                query = model.objects.using(using).filter(pk__in=selected[label])
                for field in model._meta.concrete_fields:
                    if not field.is_relation:
                        continue
                    target = field.related_model._meta.label_lower
                    if target not in models:
                        _reject("unreviewed_model_dependency")
                    selected[target].update(
                        value
                        for value in query.values_list(field.attname, flat=True)
                        if value is not None
                    )
            for label, field, target in REVERSE:
                target = user_label if target == "user" else target
                add(
                    label,
                    models[label]
                    .objects.using(using)
                    .filter(**{f"{field}_id__in": selected[target]}),
                )
            if before == sum(map(len, selected.values())):
                break
        records = []
        for label, model in models.items():
            data = serializers.serialize(
                "json",
                model.objects.using(using).filter(pk__in=selected[label]).order_by("pk"),
                fields=_fields(model),
            )
            records.extend(_canonical_record(row, models) for row in json.loads(data))
    snapshot = seal(
        {
            "version": VERSION,
            "schema": _schema_hash(),
            "source_id": source_id,
            "captured_at": captured_at,
            "records": sorted(records, key=lambda r: (r["model"], str(r["pk"]))),
        }
    )
    validate(snapshot)
    return snapshot


def seal(snapshot):
    """Canonical integrity seal; not a security signature or encrypted archive."""
    body = {key: value for key, value in snapshot.items() if key != "digest"}
    return {**body, "digest": _hash(body)}


def _money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result != result.quantize(Decimal("0.01")):
            _reject("invalid_points_precision")
        return result
    except (InvalidOperation, ValueError, TypeError):
        _reject("invalid_points_value")


def validate(snapshot):
    """Verify closure, tree, identity, uniqueness and both running balances."""
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "version",
        "schema",
        "source_id",
        "captured_at",
        "records",
        "digest",
    }:
        _reject("invalid_snapshot_format")
    legacy_payment_schema = any(
        snapshot["schema"]
        in {
            _schema_hash(payment_links=False, guest_events=events),
            _schema_hash(payment_links=False, referral_revisions=False, guest_events=events),
        }
        for events in (True, False)
    )
    old_referral_schema = any(
        snapshot["schema"]
        in {
            _schema_hash(referral_revisions=False, guest_events=events),
            _schema_hash(legacy_link_expiry=True, referral_revisions=False, guest_events=events),
            _schema_hash(payment_links=False, referral_revisions=False, guest_events=events),
        }
        for events in (True, False)
    )
    old_event_schema = snapshot["schema"] in {
        _schema_hash(guest_events=False),
        _schema_hash(legacy_link_expiry=True, guest_events=False),
        _schema_hash(referral_revisions=False, guest_events=False),
        _schema_hash(legacy_link_expiry=True, referral_revisions=False, guest_events=False),
        _schema_hash(payment_links=False, guest_events=False),
        _schema_hash(payment_links=False, referral_revisions=False, guest_events=False),
    }
    if snapshot["version"] != VERSION or (
        snapshot["schema"] not in {_schema_hash(), _schema_hash(legacy_link_expiry=True)}
        and not legacy_payment_schema
        and not old_referral_schema
        and not old_event_schema
    ):
        _reject("incompatible_snapshot_schema")
    if seal(snapshot)["digest"] != snapshot["digest"]:
        _reject("snapshot_integrity_failed")
    if len(_json(snapshot).encode()) > MAX_BYTES or len(snapshot["records"]) > MAX_ROWS:
        _reject("snapshot_size_limit")
    try:
        at = datetime.fromisoformat(snapshot["captured_at"])
        if timezone.is_naive(at):
            _reject("invalid_snapshot_time")
    except (ValueError, TypeError):
        _reject("invalid_snapshot_time")
    if not isinstance(snapshot["source_id"], str) or not 3 <= len(snapshot["source_id"]) <= 100:
        _reject("source_identity_required")
    models = _models()
    indexed = {}
    for row in snapshot["records"]:
        if not isinstance(row, dict) or set(row) != {"model", "pk", "fields"}:
            _reject("invalid_record_format")
        model = models.get(row["model"])
        if old_event_schema and row["model"] == "loyalty.guestevent":
            _reject("unreviewed_record_fields")
        if old_referral_schema and row["model"] == "loyalty.referralrevision":
            _reject("unreviewed_record_fields")
        if model is None:
            _reject("unreviewed_record_fields")
        expected_fields = set(_fields(model))
        if legacy_payment_schema:
            if row["model"] in {
                "payments.reservationpaymentrequest",
                "payments.paymentrequestdelivery",
                "payments.offlinepaymentreceipt",
                "payments.manualreceiptreference",
            }:
                _reject("unreviewed_record_fields")
            if row["model"] == "payments.paymentattempt":
                expected_fields.remove("payment_request")
        if set(row["fields"]) != expected_fields:
            _reject("unreviewed_record_fields")
        key = (row["model"], str(row["pk"]))
        if key in indexed:
            _reject("duplicate_record")
        indexed[key] = row["fields"]
    for (label, _), fields in indexed.items():
        for field in models[label]._meta.concrete_fields:
            if field.is_relation:
                if (
                    legacy_payment_schema
                    and label == "payments.paymentattempt"
                    and field.name == "payment_request"
                ):
                    # Exactly the known nullable field absent before this release.
                    # Keep the original payload and seal intact; do not rewrite history.
                    continue
                value = fields[field.name]
                if (
                    value is not None
                    and (field.related_model._meta.label_lower, str(value)) not in indexed
                ):
                    _reject("missing_related_record")
    members = {pk: f for (label, pk), f in indexed.items() if label == "loyalty.member"}
    event_references = set()
    for (label, _), event in indexed.items():
        if label != "loyalty.guestevent":
            continue
        member_id = str(event["member"]) if event["member"] is not None else None
        ledger_row = indexed.get(("loyalty.ledger", str(event["ledger"])))
        if (
            event["reference"] in event_references
            or event["kind"] not in models[label].Kind.values
            or event["source"] not in {"guest", "admin", "system", "history"}
            or (member_id and members[member_id]["customer"] != event["customer"])
            or (ledger_row and str(ledger_row["member"]) != member_id)
        ):
            _reject("invalid_guest_event")
        event_references.add(event["reference"])
    referrals = {
        str(f["member"]): f for (label, _), f in indexed.items() if label == "loyalty.referral"
    }
    if len(referrals) != sum(row["model"] == "loyalty.referral" for row in snapshot["records"]):
        _reject("duplicate_member_relation")
    if set(members) != set(referrals):
        _reject("incomplete_membership_tree")
    if len({m["number"] for m in members.values()}) != len(members) or len(
        {str(m["customer"]) for m in members.values()}
    ) != len(members):
        _reject("duplicate_membership_identity")
    if len({r["tree_number"] for r in referrals.values()}) != len(referrals):
        _reject("duplicate_tree_number")
    histories = defaultdict(list)
    for member_id, referral in referrals.items():
        histories[member_id].append(referral)
    revisions = sorted(
        (r for r in snapshot["records"] if r["model"] == "loyalty.referralrevision"),
        key=lambda r: int(r["pk"]),
    )
    for row in revisions:
        histories[str(row["fields"]["member"])].append(row["fields"])
    current_referrals = {key: rows[-1] for key, rows in histories.items()}
    for member_id, referral in ((key, r) for key, rows in histories.items() for r in rows):
        inviter_id = str(referral["inviter"]) if referral["inviter"] is not None else None
        if inviter_id is None:
            valid = (
                str(referral["root"]) == member_id
                and referral["depth"] == 0
                and referral["tree_number"] == f"{referral['branch_order']:04}"
            )
        else:
            valid = any(
                inviter_id != member_id
                and referral["root"] == parent["root"]
                and referral["depth"] == parent["depth"] + 1
                and referral["tree_number"]
                == f"{parent['tree_number']}-{referral['branch_order']:04}"
                for parent in histories[inviter_id]
            )
        if not valid:
            _reject("invalid_membership_tree")
    if len({r["tree_number"] for r in current_referrals.values()}) != len(members):
        _reject("duplicate_tree_number")
    for member_id, row in current_referrals.items():
        seen = {member_id}
        while row["inviter"] is not None:
            parent_id = str(row["inviter"])
            if parent_id in seen:
                _reject("invalid_membership_tree")
            seen.add(parent_id)
            parent = current_referrals[parent_id]
            if (
                row["root"] != parent["root"]
                or row["depth"] != parent["depth"] + 1
                or row["tree_number"] != f"{parent['tree_number']}-{row['branch_order']:04}"
            ):
                _reject("invalid_membership_tree")
            row = parent
    ledger = sorted(
        [f for (label, _), f in indexed.items() if label == "loyalty.ledger"],
        key=lambda f: f["sequence"],
    )
    program = Decimal("0.00")
    balances = defaultdict(lambda: Decimal("0.00"))
    transfers = defaultdict(list)
    uniqueness = [set(), set(), set()]
    for sequence, row in enumerate(ledger, 1):
        member_id = str(row["member"])
        member = members[member_id]
        if row["sequence"] != sequence:
            _reject("invalid_ledger_sequence")
        for seen, key in zip(
            uniqueness,
            (
                row["operation_number"],
                (row["event_reference"], member_id, row["component"]),
                (row["operation_group"], row["branch"]),
            ),
            strict=True,
        ):
            if key in seen:
                _reject("duplicate_ledger_event")
            seen.add(key)
        if (
            row["customer"] != member["customer"]
            or row["membership_number"] != member["number"]
            or row["tree_number"] not in {r["tree_number"] for r in histories[member_id]}
        ):
            _reject("ledger_identity_mismatch")
        credit, debit = _money(row["credit"]), _money(row["debit"])
        if credit < 0 or debit < 0 or (credit > 0) == (debit > 0):
            _reject("invalid_ledger_sides")
        program += credit - debit
        balances[member_id] += credit - debit
        if (
            _money(row["program_balance_after"]) != program
            or _money(row["member_balance_after"]) != balances[member_id]
        ):
            _reject("ledger_balance_mismatch")
        if balances[member_id] < 0 and not (
            credit > 0
            or (row["event_type"] == "adjust" and row.get("original"))
            or row["event_type"] == "redeem"
        ):
            _reject("invalid_negative_posting")
        if row["event_type"] == "transfer":
            transfers[row["operation_group"]].append(row)
        # Financial evidence also contains JSON references, not just foreign keys.
        for payment in row["evidence"].get("payments", []):
            if ("payments.paymentattempt", str(payment["id"])) not in indexed:
                _reject("missing_payment_evidence")
        for refund in row["evidence"].get("refunds", []):
            if ("reservations.refundobligation", str(refund)) not in indexed:
                _reject("missing_refund_evidence")
        modification = row["evidence"].get("modification")
        if (
            modification
            and ("reservations.bookingmodificationrequest", str(modification)) not in indexed
        ):
            _reject("missing_modification_evidence")
    for pair in transfers.values():
        if len(pair) != 2:
            _reject("incomplete_transfer_pair")
        a, b = pair
        if not (
            a["member"] == b["counterpart"]
            and b["member"] == a["counterpart"]
            and _money(a["credit"]) == _money(b["debit"])
            and _money(a["debit"]) == _money(b["credit"])
        ):
            _reject("invalid_transfer_pair")
    return {
        "counts": dict(sorted(Counter(row["model"] for row in snapshot["records"]).items())),
        "program_balance": str(program),
        "member_balances": {pk: str(balances[pk]) for pk in members},
        "ledger_sequence": len(ledger),
    }


def compare(previous, incoming):
    """Never propagate a disappeared or rewritten financial history."""
    validate(incoming)
    if previous is None:
        return
    validate(previous)
    if previous["source_id"] != incoming["source_id"]:
        _reject("different_source")
    if datetime.fromisoformat(incoming["captured_at"]) < datetime.fromisoformat(
        previous["captured_at"]
    ):
        _reject("stale_snapshot")
    models = _models()
    old = {(r["model"], str(r["pk"])): _canonical_record(r, models) for r in previous["records"]}
    new = {(r["model"], str(r["pk"])): _canonical_record(r, models) for r in incoming["records"]}
    for key, row in old.items():
        if key not in new:
            _reject("financial_history_missing" if key[0] in CORE else "related_history_missing")
        if (
            key[0]
            in {"payments.offlinepaymentreceipt", "loyalty.referralrevision", "loyalty.guestevent"}
            and new[key] != row
        ):
            _reject(
                "offline_receipt_history_changed"
                if key[0] == "payments.offlinepaymentreceipt"
                else "financial_history_changed"
            )
        if key[0] not in CORE:
            continue
        if key[0] != "loyalty.member" and new[key] != row:
            _reject("financial_history_changed")
        if key[0] == "loyalty.member" and any(
            new[key]["fields"][f] != row["fields"][f] for f in ("customer", "number", "joined_at")
        ):
            _reject("membership_identity_changed")


class LocalMirror:
    """One fixed SQLite copy, transactions and one bounded run-status row.

    This is an offline rehearsal adapter, NOT an active Supabase integration.
    Contains private account/booking data; only fictional SQLite sources allowed.
    """

    TABLES = {
        "loyalty.member": "members",
        "loyalty.referral": "referrals",
        "loyalty.ledger": "ledger",
    }

    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists() and self.path.is_symlink():
            _reject("unsafe_mirror_path")

    @contextmanager
    def _open(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            allowed = {*self.TABLES.values(), "context", "manifest", "status"}
            existing = {
                r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if existing - allowed:
                _reject("not_a_mirror_database")
            for table in (*self.TABLES.values(), "context"):
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} "
                    "(key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS manifest "
                "(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS status "
                "(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
            )
            db.commit()
            yield db
        finally:
            db.close()

    def _read(self, db):
        result = db.execute("SELECT payload FROM manifest WHERE id=1").fetchone()
        if result is None:
            if any(
                db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                for t in (*self.TABLES.values(), "context")
            ):
                _reject("missing_mirror_manifest")
            return None
        manifest = json.loads(result[0])
        records = []
        for table in (*self.TABLES.values(), "context"):
            records.extend(
                json.loads(row[0])
                for row in db.execute(f"SELECT payload FROM {table} ORDER BY key")
            )
        records.sort(key=lambda r: (r["model"], str(r["pk"])))
        snapshot = {**manifest, "records": records}
        validate(snapshot)
        return snapshot

    def read(self):
        with self._open() as db:
            db.execute("BEGIN")
            return self._read(db)

    def update(self, snapshot):
        _local_source_only()
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                # Verify the supplied seal BEFORE canonicalizing its ordering.
                validate(snapshot)
                _fictional_only(snapshot)
                snapshot = dict(snapshot)
                snapshot["records"] = sorted(
                    snapshot["records"], key=lambda r: (r["model"], str(r["pk"]))
                )
                snapshot = seal(snapshot)
                compare(self._read(db), snapshot)
                for table in (*self.TABLES.values(), "context"):
                    db.execute(f"DELETE FROM {table}")
                for row in snapshot["records"]:
                    table = self.TABLES.get(row["model"], "context")
                    key = f"{row['model']}:{row['pk']}"
                    db.execute(f"INSERT INTO {table}(key,payload) VALUES (?,?)", (key, _json(row)))
                manifest = {k: v for k, v in snapshot.items() if k != "records"}
                db.execute(
                    "INSERT OR REPLACE INTO manifest(id,payload) VALUES (1,?)", (_json(manifest),)
                )
                # Final readback remains in the same transaction, before any commit.
                result = validate(self._read(db))
                status = {
                    "status": "succeeded",
                    "last_attempt": timezone.now().isoformat(),
                    "last_success": timezone.now().isoformat(),
                    "error_code": "",
                }
                db.execute(
                    "INSERT OR REPLACE INTO status(id,payload) VALUES (1,?)", (_json(status),)
                )
                db.commit()
                return result
            except Exception as exc:
                db.rollback()
                old = db.execute("SELECT payload FROM status WHERE id=1").fetchone()
                status = json.loads(old[0]) if old else {"last_success": None}
                status.update(
                    status="failed",
                    last_attempt=timezone.now().isoformat(),
                    error_code=getattr(exc, "code", None) or type(exc).__name__,
                )
                db.execute(
                    "INSERT OR REPLACE INTO status(id,payload) VALUES (1,?)", (_json(status),)
                )
                db.commit()
                raise

    def status(self):
        with self._open() as db:
            row = db.execute("SELECT payload FROM status WHERE id=1").fetchone()
            return json.loads(row[0]) if row else {"status": "never_run", "last_success": None}

    def run(self, *, source_id):
        """Also record capture failure, which occurs before the update transaction."""
        _local_source_only()
        try:
            return self.update(capture(source_id=source_id))
        except Exception as exc:
            with self._open() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT payload FROM status WHERE id=1").fetchone()
                state = json.loads(old[0]) if old else {"last_success": None}
                state.update(
                    status="failed",
                    last_attempt=timezone.now().isoformat(),
                    error_code=getattr(exc, "code", None) or type(exc).__name__,
                )
                db.execute(
                    "INSERT OR REPLACE INTO status(id,payload) VALUES (1,?)", (_json(state),)
                )
                db.commit()
            raise


def _local_source_only(using="default"):
    if connections[using].vendor != "sqlite":
        _reject("local_rehearsal_only")


def _fictional_only(snapshot):
    for row in snapshot["records"]:
        if row["model"] == settings.AUTH_USER_MODEL.lower() and not row["fields"]["email"].endswith(
            ".invalid"
        ):
            _reject("fictional_accounts_only")
        if row["model"] == "reservations.reservation" and not row["fields"]["is_test"]:
            _reject("fictional_bookings_only")


def restore_local(snapshot, *, using):
    """Insert into an explicitly separate EMPTY SQLite database. No overwrites.

    Raw fixture saves do not call booking/payment services. Original references,
    verification timestamps, settlement revisions and request receipts survive.
    """
    if using == "default":
        _reject("restore_requires_separate_database")
    _local_source_only(using)
    source = connections["default"]
    target = connections[using]
    if target.settings_dict["NAME"] == source.settings_dict["NAME"]:
        _reject("restore_requires_separate_database")
    validate(snapshot)
    _fictional_only(snapshot)
    models = _models()
    if any(m.objects.using(using).exists() for m in models.values()):
        _reject("restore_target_not_empty")
    # Use referral depth / ledger sequence order for database history constraints.
    order = {label: n for n, label in enumerate(models)}
    records = sorted(
        snapshot["records"],
        key=lambda row: (
            order[row["model"]],
            row["fields"].get("sequence", row["fields"].get("depth", 0)),
            str(row["pk"]),
        ),
    )
    with target.constraint_checks_disabled(), transaction.atomic(using=using):
        for obj in serializers.deserialize("json", _json(records), using=using):
            obj.save(using=using)
        target.check_constraints()
        recovered = capture(source_id=snapshot["source_id"], using=using)
        if {k: v for k, v in validate(recovered).items()} != validate(snapshot):
            _reject("restore_totals_mismatch")
        original = {
            (r["model"], str(r["pk"])): _canonical_record(r, models) for r in snapshot["records"]
        }
        restored = {
            (r["model"], str(r["pk"])): _canonical_record(r, models) for r in recovered["records"]
        }
        if original != restored:
            _reject("restore_content_mismatch")
    return validate(recovered)
