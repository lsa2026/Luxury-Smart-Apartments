"""Read-only, bounded tree presentation from current membership/referral records."""

from collections import Counter

from apps.accounts.models import VerifiedPhone
from apps.reservations.phone_search import phone_variants

PAGE_SIZE = 20


def search(links, value):
    """Never infer relationships from ledger paths frozen at the time of posting."""
    exact = [
        row
        for row in links.values()
        if value.casefold() in {row.member.number.casefold(), row.tree_number.casefold()}
    ]
    if exact:
        return exact
    variants = phone_variants(value)
    if variants:
        customers = set(
            VerifiedPhone.objects.filter(phone__in=variants).values_list("user_id", flat=True)
        )
        return [row for row in links.values() if row.member.customer_id in customers]
    return [
        row
        for row in links.values()
        if value.casefold() in row.member.customer.get_full_name().casefold()
    ][: PAGE_SIZE + 1]


def presentation(links, focus, after):
    counts = Counter(row.inviter_id for row in links.values())
    for row in links.values():
        row.child_count = counts[row.member_id]
    children = sorted(
        (
            row
            for row in links.values()
            if row.inviter_id == (focus.member_id if focus else None) and row.branch_order > after
        ),
        key=lambda row: (row.branch_order, row.member_id),
    )[: PAGE_SIZE + 1]
    # A corrupt path must not cause recursion or an unbounded breadcrumb render.
    ancestors, seen = [], set()
    parent = focus.inviter_id if focus else None
    while parent in links and parent not in seen and len(ancestors) < 12:
        seen.add(parent)
        row = links[parent]
        ancestors.append(row)
        parent = row.inviter_id
    return {
        "tree_children": children[:PAGE_SIZE],
        "tree_cursor": children[PAGE_SIZE - 1].branch_order if len(children) > PAGE_SIZE else None,
        "tree_ancestors": list(reversed(ancestors)),
        "tree_path_shortened": parent is not None,
        "tree_total": len(links),
        "tree_roots": counts[None],
        "tree_linked": len(links) - counts[None],
    }
