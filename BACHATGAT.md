# Mahila Bachat Gat (महिला बचत गट) module

Women's Self-Help Group (SHG) management for the "Mahila Bachat Gat" business type:
groups, members, savings, internal loans, meetings & resolutions, double-entry
accounts, livelihood business, bank/scheme linkage, notifications and reports.

## Where things are

| Part | Files |
|---|---|
| Schema (31 `shg_*` tables, triggers, business type row) | `bachatgat_service.py`, `migrations/2026_10_17_bachatgat.sql` |
| Financial maths (no DB): schedules, allocation, penalties, eligibility, journal lines | `bachatgat_finance.py` |
| Auth, transactions, idempotency, posting engine, resolution checks | `bachatgat_common.py` |
| Groups, officers, bank accounts, policies, members, documents, savings | `bachatgat_api.py` |
| Loans, repayments, installments, meetings, attendance, resolutions | `bachatgat_loans_api.py` |
| Accounts, business, schemes, notifications, dashboard, reports | `bachatgat_accounts_api.py` |
| Mobile module | `../src/screens/bachatgat/` (mounted from `App.tsx` as the "Mahila Bachat Gat" tab) |
| Admin feature catalogue | `../admin-dashboard/src/App.tsx` (`shg_*` keys) |

All endpoints are under `/api/bachatgat` and require the bearer token from `/api/login`.

## Migration

The schema is provisioned at API startup by `ensure_bachatgat_tables()` in `app.py`,
in its own transaction so a failure cannot roll back other business types' schema.
It is additive and idempotent: it creates `shg_*` tables, indexes, two trigger
functions and inserts the `Mahila Bachat Gat` row into `business_types`. It never
alters existing tables.

**Starting the API applies it to whatever `DATABASE_URL` points to.** To review or
apply it by hand first:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/2026_10_17_bachatgat.sql
```

Rollback (deletes all SHG data — back up first):

```powershell
pg_dump "$env:DATABASE_URL" -t 'shg_*' -f shg_backup.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/2026_10_17_bachatgat_down.sql
```

Tested: applied twice (idempotent) and rolled back on a fresh PostgreSQL 18 database
loaded with `schema.sql`; existing rows were untouched.

## Tests

```powershell
# Pure financial calculations (no database)
.venv\Scripts\python.exe -m unittest test_bachatgat_finance -v

# End-to-end API tests. They CREATE TABLES and WRITE DATA, so they only run against
# a database you name explicitly (they refuse the DATABASE_URL in .env):
$env:BACHATGAT_TEST_DATABASE_URL = "postgresql://user@127.0.0.1:5432/empty_test_db"
.venv\Scripts\python.exe -m unittest test_bachatgat_api -v
```

The end-to-end suite covers tenant isolation, feature switches, the group/member/loan
workflows, idempotent and concurrent duplicate submissions, reversals, period
closing, ledger immutability, balanced trial balance and financial position, business
stock and P&L, schemes, bank reconciliation, member exit and every report.

## Accounting model

- Every posting is one `shg_transactions` header plus balanced `shg_ledger_lines`
  (double entry). A deferred constraint trigger rejects any unbalanced transaction
  at commit; another trigger blocks edits to posted lines.
- Classifications are kept apart: member savings (liability), loans to members
  (asset), interest/penalty income, business income/expense, group expenses, grants
  and bank loans (external funds).
- Interest is recognised when received (cash basis), as SHG books are kept.
- Corrections are reversals (mirror entry dated today); posted rows are never
  edited or deleted. Write-offs are final.
- Postings lock the group row, so balance checks and number sequences cannot race;
  cash or a bank account can never go negative.
- Financial writes need an `Idempotency-Key` header; a retried request returns the
  original response instead of posting again.
- Closing a period blocks postings dated on or before it.

## Roles and approvals

The platform has one login per customer account. That login acts as the Group
Manager for all of the account's groups; the platform admin (admin dashboard) is the
Super Admin and switches `shg_*` modules on or off per customer, which the API
enforces. President, Secretary and Treasurer are member records, and the workflow
requires them where SHG practice does:

- group approval: a registered Group Manager / Field Officer as reviewer, a document on file
- loan approval, reschedule, waiver, write-off, large expenses, savings withdrawals:
  a passed resolution of a held meeting with quorum (each resolution authorizes one decision;
  the borrower may not vote on her own loan; above the loan approval limit two-thirds must agree)
- disbursement: authorized by the President or Treasurer, never the borrower
- minutes: confirmed by both the President and the Secretary, who must have attended

Separate logins for members, treasurers, field officers or auditors are not part of
this module (the platform has no per-person accounts inside a customer).

## Autopilot (AI agent) - `bachatgat_agent.py`

Runs each active group by itself (background thread every `SHG_AUTOPILOT_INTERVAL_MIN`, a group at most every
`SHG_AUTOPILOT_EVERY_HOURS` = 6; also `POST /api/bachatgat/agent/cron` with header `X-Cron-Secret: $SHG_AGENT_CRON_SECRET`
for an external cron, since a sleeping free Render instance runs no thread). Steps: approve pending members with ID
recorded, raise dues, sync Razorpay, penalties/overdue, loans (submit -> review -> approve within the Autopilot limit or
a passed resolution -> disburse when cash/bank allows, authorized by Treasurer/President), SMS payment links for overdue
savings and installments (loan links post as repayments), schedule next meeting, reminders, close last month from the 7th.
`POST /agent/scan` reads a meeting-register photo with Gemini and posts attendance, savings, repayments, loan requests and
new members. Every action is in `shg_agent_actions`; money actions undo by reversal; undone records are left to people.
Disabling: per group in the app, or `SHG_AUTOPILOT=off` for the thread. Tests: `test_bachatgat_agent.py`.
