# Address path audit — 2026-11-10

The owner requested consistent short-address entry and clear approved field
conditions across website, guest and management journeys. This extends the
existing merchant-approved contract; it is not a newly verified provider spec.
The cited upstream review in `morning-hyperpay-validation.md` remains dated
2026-11-07. No real guest booking, payment, message or paid address lookup is
needed to test this change.

| Path | Entry/search and validation |
| --- | --- |
| Public quote / guest details POST | Shared required billing fields, persistent help and browser/server rules. Invalid input returns to the billing step. |
| Manual management finalization | Existing authenticated search, Arabic shared fields and strict server validation. No administration consent on the guest’s behalf. |
| Existing reservation payment, legacy and revised links | Same required billing contract and search, with service validation before provider IO. Explicitly collected manual addresses are reviewed or corrected; placeholders from legacy bookings are not presented as verified addresses. |
| Morning management create, detail and failed billing save | Shared authenticated search and field rules; existing payment/booking guards retained. |
| Day-use management create | Adds the same authenticated search and client validation to the existing server rules. Availability/price confirmation remains required. |
| Day-use management detail | Adds local-only correction, with optimistic timestamp and property/stay locks shared with checkout. Disabled, ended, paid, processing, unresolved provider/checkout or financial-event states refuse edits. No provider write or communication replay. |
| Morning/day-use guest payment | Existing required fallback entry uses the same explanatory note and fields. Saved valid billing stays review-only; invalid saved billing requires management correction. Guest accepts current legal documents separately. |
| Guest account residence details | Existing optional search/manual entry, optionality and storage limits are visible. No payment-specific region/postcode restrictions imposed on residence data. |

Property location displays, invoices, reservation summaries and read-only
administration billing displays are not address-entry/payment forms. Account
residence is not automatically certified as the card billing address.

The short address code is a search input, not a substitute for the street.
Selecting a suggestion fills only available components, preserves manual edits
and exposes incomplete/invalid fields for explicit correction. Provider spaces
or hyphens are not silently deleted from suggestions. Search unavailable,
disabled or JavaScript unavailable leaves manual fields usable. All billing
fields are required: street 1–100 (not only ASCII digits), city 1–80 Unicode,
region 1–50 ASCII letters/digits/dots, postcode 1–16 ASCII letters/digits,
country an actual uppercase ISO alpha-2 choice. Edge whitespace is trimmed;
no internal space/hyphen stripping or invented address values.

Verification uses disposable databases and mocked integrations, the actual shared
browser validator, search/stale-ticket tests, and ordinary/morning/day-use/payment
regressions. The public checkout wizard reveals an invalid field’s step before
reporting errors; collapsed correction fields open before validation reporting.
No production booking/payment/lookup is used. Deploy through the existing PR/main
pipeline after checks pass; verify exact deployment commits and public health.
