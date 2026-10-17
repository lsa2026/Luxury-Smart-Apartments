"""Read-only, bounded tree presentation from current membership/referral records."""

from collections import Counter, defaultdict, deque

from apps.accounts.models import VerifiedPhone
from apps.reservations.phone_search import phone_variants

PAGE_SIZE = 20
DIAGRAM_NODES = 120
DIAGRAM_DEPTH = 4


def diagram(links, children, *, focus=None):
    """Five visible generations at most; unrendered relatives keep explicit controls."""
    adjacency = defaultdict(list)
    for row in links.values():
        adjacency[row.inviter_id].append(row)
    for siblings in adjacency.values():
        siblings.sort(key=lambda row: (row.branch_order, row.member_id))
    result = [{"link": row, "children": [], "loaded": False} for row in children]
    queue = deque((node, 1 if focus else 0) for node in result)
    seen = {row.member_id for row in children}
    if focus:
        seen.add(focus.member_id)
    while queue:
        node, depth = queue.popleft()
        row = node["link"]
        if depth >= DIAGRAM_DEPTH or len(seen) >= DIAGRAM_NODES:
            continue
        siblings = adjacency[row.member_id]
        selected = [child for child in siblings if child.member_id not in seen][
            : min(PAGE_SIZE, DIAGRAM_NODES - len(seen))
        ]
        node["children"] = [{"link": child, "children": [], "loaded": False} for child in selected]
        node["loaded"] = bool(selected)
        node["cursor"] = (
            selected[-1].branch_order if selected and len(selected) < len(siblings) else None
        )
        seen.update(child.member_id for child in selected)
        queue.extend((child, depth + 1) for child in node["children"])
    return result


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
        inviter = links.get(row.inviter_id)
        row.inviter_name = (
            inviter.member.customer.get_full_name() or inviter.member.number
            if inviter
            else "بدون داعٍ"
        )
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
        "tree_diagram_children": diagram(links, children[:PAGE_SIZE], focus=focus),
        "tree_cursor": children[PAGE_SIZE - 1].branch_order if len(children) > PAGE_SIZE else None,
        "tree_ancestors": list(reversed(ancestors)),
        "tree_path_shortened": parent is not None,
        "tree_total": len(links),
        "tree_roots": counts[None],
        "tree_linked": len(links) - counts[None],
    }
