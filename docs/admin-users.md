# Operations managers and Google account selection

The operations owner remains the configured `OPERATIONS_OWNER_EMAIL`, with the existing active staff/superuser checks. Only that owner can add or revoke manager access at `/admin/operations/users/`.

## Manager access

- Grants accept explicitly confirmed `@gmail.com` addresses, normalized to lowercase.
- A grant does not create an account or send an invitation. The manager signs in normally with the approved Google identity.
- Manager authorization requires a verified Google identity and an authenticated server-side session proof. Email-code, password, and other-provider logins cannot establish that proof.
- Managers can operate bookings, manual bookings, cancellation/refund requests, and guest invoices. They cannot manage users, use unrestricted Django administration, change system settings, access operational reports, or administer loyalty.
- Manager accounts are not promoted to Django staff or superuser.
- Revocation is checked against the database on every subsequent request. It does not retroactively cancel an already executing request or delete accounts, bookings, invoices, or audit evidence.
- Re-enabling a grant changes its version: an old session does not regain access; a fresh Google sign-in is required.
- Grant and revoke actions require owner authorization and CSRF-protected confirmation and are recorded in the audit log.

## Account chooser

The administration sign-in link sends `prompt=select_account` to Google. Guest sign-in behavior is unchanged. An existing authenticated owner session may still redirect directly from the login URL; the chooser applies when using the Google sign-in button, not on every page visit. Google may reuse a valid Google session without requesting the password again.

## Deployment and verification

Migration `accounts.0004_operationsmanager` is additive. No existing owner, guest account, booking, or payment is modified by the migration. Synthetic tests cover owner-only grants, Google proof, revocation, stale sessions, CSRF, route restrictions, and the scoped OAuth chooser; PostgreSQL CI also runs these tests. Offline previews have no real bookings or external API calls.

Rollback must retain the grants table and audit records. Reverting to owner-only application code denies all managers while preserving data. Never delete production account or reservation tables to undo this feature.
