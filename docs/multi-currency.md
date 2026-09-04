# Multi-currency display and SAR payment architecture

The booking platform has three deliberately separate monetary concepts:

- **Source price:** the current amount and explicit currency returned by Hostaway
  `priceDetails` v2. The application never derives this currency from a city,
  country, property, or cached listing currency.
- **Payment price:** the server-calculated amount in SAR. HyperPay receives this
  amount and `currency=SAR` only.
- **Display price:** a presentation-only conversion in SAR, MAD, USD, or EUR.
  The selected display currency is stored as a non-sensitive session/cookie
  preference and is never accepted as a payment instruction.

## Conversion and rounding

The configured provider uses SAR as its base. If `rates[MAD] = 2.5`, one SAR is
2.5 MAD:

```text
source amount to SAR = source amount / rates[source currency]
SAR to display amount = SAR amount * rates[display currency]
```

SAR-to-SAR is an identity operation. All parsing and arithmetic uses
`decimal.Decimal`. `apps.payments.currency.normalize_amount()` is the single
rounding boundary and currently applies two minor units with `ROUND_HALF_UP` to
SAR, MAD, USD, and EUR. No FX spread or markup is applied.

## Provider validation and resilience

`apps.payments.currency.ExchangeRateProvider` accepts only an HTTPS provider
response with `result=success`, `base_code=SAR`, every supported positive
numeric rate, `rates[SAR]=1`, and valid timestamp metadata when supplied.
Connection and read timeouts are independent settings.

One rate table serves every amount on a page. A fresh table is cached for
`FX_RATE_CACHE_TTL_SECONDS` (one hour by default) and the same validated table
is retained as Last Known Good. A provider timeout, HTTP error, invalid JSON, or
malformed table falls back to LKG. Without a valid rate, foreign conversion
fails safely; no code substitutes a rate of one. A pure SAR quote remains
available through the mathematically exact identity path.

Production should set `CACHE_URL` to the shared Redis cache already used by the
project so web workers share fresh and LKG rates.

## Immutable checkout snapshot

Every new `BookingQuote` stores the untouched Hostaway source amount/currency,
the rounded SAR payment amount, the selected display currency, provider/base,
rate timestamp, relevant rates, display amount, and quote timestamps. These
fields are included in the existing HMAC quote fingerprint.

When the guest accepts a still-current quote, `BookingIntent` receives its own
copy of the snapshot with the checkout expiry. HyperPay validates that the
persisted snapshot still reproduces the persisted SAR amount before creating a
checkout. Browser amounts, cookies, query strings, hidden fields, and display
values are never used to build the gateway payload.

Historical rows remain nullable and are not backfilled with invented FX data.
They cannot start a new HyperPay checkout without a valid payment snapshot.

An expired public quote is marked expired, revalidated through the existing
live Hostaway path, and replaced with a new source price and FX snapshot for the
guest to review. A current checkout keeps its locked SAR amount; Hostaway is
still revalidated immediately before payment and again before reservation
creation. If the source amount or explicit source currency changes, payment is
stopped for renewed customer approval.

Hostaway reservation and modification payloads continue using the original
Hostaway currency and total. Only the matching verified payment gate uses the
SAR payment amount.

## Configuration

Safe defaults are documented in `.env.example`:

```text
FX_PROVIDER_URL=https://open.er-api.com/v6/latest/SAR
FX_PROVIDER_NAME=open.er-api.com
FX_BASE_CURRENCY=SAR
FX_SUPPORTED_CURRENCIES=SAR,MAD,EUR,USD
FX_RATE_CACHE_TTL_SECONDS=3600
FX_REQUEST_CONNECT_TIMEOUT=3
FX_REQUEST_READ_TIMEOUT=5
```

The application refuses a non-HTTPS provider, non-SAR base, or a supported set
other than SAR/MAD/EUR/USD. `HYPERPAY_CURRENCY` remains independently pinned to
SAR by the existing production configuration guard and a database constraint.

To add another display currency, add its ISO code and minor units to the
settings validation, require and validate its provider rate, add its label to
the selector/presentation map, update `.env.example`, and add exact Decimal,
failure-mode, template, RTL, and browser tests. Do not enable the code in the
environment until all workers can read the same cache.

## Verification commands

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_currency.py
.\.venv\Scripts\python.exe -m pytest tests\test_multi_currency_integration.py
.\.venv\Scripts\python.exe -m pytest tests\test_hyperpay.py
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py migrate --check
```

There is no repository-owned Playwright suite at present. For browser QA, run
the local Django server with non-production credentials and inspect the search,
property, quote, request, checkout, and result pages at desktop and mobile
widths. Exercise SAR/MAD/USD/EUR, Arabic RTL, selector persistence, and the
explicit SAR payment notice. Never use a live production payment.
