# Manual invitation history and deliberate repeat requests

The reservation administration page now shows the existing WhatsApp/email
receipts, including requested time, recorded outcome time and status, in
Asia/Riyadh. Existing records are read without backfilling invented timestamps.
The history remains visible when a later eligibility or membership check blocks
the invitation. Provider acceptance is distinguished from delivery/read evidence.

A previous receipt does not permanently prevent a manager's manual repeat.
Repeating requires a fresh signed preview of the current history/recipient/quote
and an explicit repeat checkbox. Each new approval has its own request ID,
provider reference and stored receipt. Duplicate task execution, replaying the
same approval, a pending send, stale history, revoked authorization and a guest
joining before dispatch remain protected.

An existing shared invitation receipt and its provider ID/outcomes are not reset
to make a manual repeat. Automatic n8n claims retain their original suppression
rules; this owner-only behavior is not an integration API input. A legacy manual
receipt without a registry entry receives a new suppression claim for the new
explicit attempt, without rewriting the legacy receipt.

An accepted or ambiguous original past-stay reward offer remains the financial
authority; repeat delivery cannot replace its recipient or quoted financial
snapshot, remove its acceptance evidence or create a second 3% award. A failed
repeat does not invalidate the original invitation. No guest messages or live
financial actions are executed as part of release verification.
