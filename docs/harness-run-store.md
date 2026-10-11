# Persistent run store

October 7, 2026 · Harness Task 4 implemented

`runs/store.py` saves the harness's private audit data in local SQLite. Record
changes and their typed events share one durable transaction. This is the
storage foundation used by the [budget ledger](harness-budget-ledger.md),
with the decision loop and full game recovery still upcoming.
It imports neither a provider SDK nor a native library and makes no paid calls.

## Records and scope

| Table | Saved data | Protection |
| --- | --- | --- |
| `campaigns` | Shared resource caps and creation time | Stable campaign identity; caps cannot be replaced by a retry |
| `runs` | Campaign link, complete execution manifest, creation time, status | Foreign key to campaign; immutable manifest identity |
| `decisions` | Public observation, frozen pool and handles, issued evidence, final outcome | Run scope; one decision per game/revision within a run; append-only evidence |
| `attempts` | Complete bounded request, its SHA-256, dispatch state, raw typed reply, usage, opaque continuation | Unique attempt identity; reply must match attempt/provider/decision/observation |
| `reservations` | Integer microusd, model call slots, native work, tool steps, state, settlement facts | Scoped decision/attempt foreign keys; at most one reservation per model attempt |
| `commits` | Selected physical move, game, expected revision, source checkpoint identity, optional receipt | Unique game/expected revision and game/new revision within the database |
| `events` | Typed record snapshot, event ID, UTC timestamp, per-run sequence | Record/event atomicity; ordered unique sequences; SQL blocks event updates/deletes |

Records use the existing frozen, strict Pydantic models and canonical JSON.
Each body has a SHA-256; reads validate the model, canonical representation,
hash, and relational columns. These checks detect accidental inconsistencies;
they do not authenticate data supplied by a trusted caller. Manifest dependency
order is normalized to match the manifest's existing identity semantics.
The rate card and model settings remain part of the saved execution manifest.

IDs are supplied by the caller and retained across retries. Retrying an
identical creation or completed transition returns the durable record and adds
no event. Reusing an identity with different inputs, reply, allocation, outcome,
or commit fails with `StoreConflictError`. Retry methods can return an already
advanced record: **inspect its state before any external dispatch**.
Use new run/decision/attempt identities for a new trial. Repeated position
trials may reuse an observation across different runs; full-game trials need
separate game identities when they record commits in the same database.

## Use

Run from the project root. `RunStore` creates its dedicated parent with mode
0700 and its database/lock with mode 0600. `private/` is already ignored.
An existing parent must be owner-only; do not use a shared or synced directory.
The directory contains raw provider data and is unsuitable as a viewer export.

```python
from pathlib import Path
from scrabble_harness.runs import RunStore

# manifest and context come from the existing preparation APIs.
with RunStore(Path("private/runs/campaign.sqlite3")) as store:
    store.create_campaign("campaign-001", manifest.run_config.limits)
    store.create_run("run-001", "campaign-001", manifest)
    store.prepare_decision("run-001", context)
    recorded = store.get_decision(context.decision_id)
    timeline = store.events("run-001", after_sequence=0, limit=1000)
    store.check_integrity()
```

For preparation failures, first call
`start_decision(run_id, decision_id, observation)`. After generation, call
`prepare_decision(run_id, context)` to freeze the snapshot. If preparation
fails, `complete_decision` accepts a scoped aborted `DecisionOutcome` with no
pool, evidence, or selection. Preparation failure therefore remains auditable
without constructing a fake candidate context.

After comparison, `record_evidence(updated_context)` accepts only an unchanged
frozen context with appended issued evidence. Before another model request,
that request's public view and evidence must match the saved context.
`complete_decision(outcome)` checks its pool, bindings, evidence, and selected
workflow against the saved snapshot.

`prepare_attempt(run_id, decision_id, request)` stores the exact request and
hash. `mark_attempt_dispatched(attempt_id)` records the external-side-effect
boundary; `record_reply(reply)` saves even malformed tool argument text and
unknown usage. `mark_attempt_unresolved(attempt_id)` retains uncertainty and
allows a subsequently recovered reply. A dispatched or unresolved attempt
cannot transition back to prepared/dispatched for automatic replay.

Group related updates with the same connection:

```python
with store.transaction():
    store.mark_attempt_dispatched(attempt_id)
    store.transition_reservation(reservation_id, "dispatched")
# The provider call belongs after this transaction commits.
```

The outer block uses `BEGIN IMMEDIATE`. Nested store operations use savepoints:
a failed method rolls back its own record/event changes even if the caller
catches its exception inside the outer block. An uncaught exception rolls back
the entire outer block. Do not await, call a provider, run native analysis, or
perform other external side effects inside this synchronous transaction.

## Lifecycle rules and deliberate limits

- Run: `created → running → completed/failed/cancelled`; a created run may also
  fail/cancel. Terminal runs cannot be restarted or accept new decisions.
- Decision: `preparing → prepared → completed`, or preparation directly aborts
  to completed. Direct `prepare_decision` remains available.
- Attempt: `prepared → dispatched → replied/unresolved`; unresolved may accept
  a late reply. Persisted dispatch alone does not mean a provider call succeeded.
- Reservation: `reserved → dispatched → settled/unresolved`; unresolved may
  settle later. Release is allowed only from reserved, and a model reservation
  cannot release once its attempt has been dispatched. A released model
  allowance cannot subsequently dispatch its associated attempt.
- Commit: intent first, then one consecutive revision/checkpoint confirmation.
  The move must equal the selected canonical action, including exchanged tiles.

`create_reservation` **does not enforce caps**. Runtime admission must use
Task 5's `BudgetLedger`, which checks campaign/run/decision limits and accounts
for uncertain cost/work in one transaction. Actual usage beyond its saved bound
remains recordable and blocks further admission. The ledger derives current
totals from reservations and finish holds in a coherent transaction; no mutable
aggregate counter can drift from those facts.

Commit methods record intent and a caller-supplied receipt. They do not invoke
the arbiter, validate checkpoint contents, publish checkpoint files, or resume a
game. SQLite and checkpoint publication cannot share a transaction. The game
runner and Task 15 reconciliation must verify the checkpoint and actual
transition before treating a stored receipt as proof of a committed turn.
Opening the database neither replays calls nor automatically resolves pending
attempts/reservations. Task 5 adds explicit conservative accounting recovery;
checkpoint reconciliation and game resume remain Task 15.

## Durability and concurrency

The store enables and verifies foreign keys, WAL mode, and `synchronous=FULL`.
On macOS it also requests full filesystem sync for writes and checkpoints.
Creation syncs the database directory. WAL durability and local filesystem
constraints follow [SQLite's WAL documentation](https://www.sqlite.org/wal.html)
and its [synchronous pragma](https://www.sqlite.org/pragma.html#pragma_synchronous).
Do not copy a live `.sqlite3` file by itself: committed data may still be in its
WAL. A backup/export API is a later feature.

Each `RunStore` belongs to its creating thread and process. Open a separate
instance for another thread/process. Managed connections serialize opening,
migrations, transactions, explicit checkpoints, and closing using a private
`fcntl.flock` file; independent reads remain possible. Lock waits are bounded
by `busy_timeout_seconds` (default 5), and raise `StoreBusyError` on expiry.
The persistent `.lock` file is never unlinked, to keep one lock inode.
This database lock is separate from the future game-runner lock.

The tested Python runtime embeds SQLite 3.45.3, which falls within the versions
listed for SQLite's [WAL-reset bug](https://www.sqlite.org/wal.html#walreset).
Serializing every managed write/checkpoint is our mitigation of its concurrent
write/checkpoint trigger. All live connections must use `RunStore`; unmanaged
writers/checkpointers bypass this protection. A patched SQLite runtime remains
the preferred eventual upgrade. Concurrent tests verify our serialization and
data integrity; they do not attempt to reproduce SQLite's internal race.

The current implementation targets macOS/Linux local filesystems (`fcntl` and
`O_NOFOLLOW`). It rejects symlinks, hard-linked files, unowned files, and public
parent directories. Network filesystem placement is unsupported. There is no
Windows lock implementation.

## Migrations and errors

`runs/migrations/001_initial.sql` and `002_accounting.sql` are packaged in the wheel. The store checks an
application ID, schema version, and SHA-256 migration history before enabling
WAL on an existing database. Foreign databases, future versions, and altered
migration histories fail with `StoreCorruptionError`. Migration DDL/history/
version updates share one transaction; `executescript` is avoided because its
implicit commit would break this boundary. Add new numbered migrations rather
than changing a published migration. Task 4 is the initial schema release. Task 5 adds finish holds, persisted budget
clocks, and native receipt identities. Migration 002 preserves existing record
bytes and audit events; migration 001 is unchanged.

`StoreNotFoundError` reports missing identities, `StoreConflictError` reports
conflicts/transitions, `StoreBusyError` reports contention, `StorePathError`
reports unsafe locations, and `StoreCorruptionError` reports invalid stored
content. Validation errors report malformed incoming typed data. Public store
errors do not include raw replies or provider continuation content.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/runs -q -p no:cacheprovider
```

The Task 4 storage tests cover complete reopening, conservative unresolved states,
late replies, idempotency/conflicts, preparation aborts, frozen evidence,
foreign keys, revision uniqueness, private modes, migration rollback/history,
content/index corruption, atomic events, nested rollback, bounded contention,
thread ownership, four concurrent thread writers, three process writers, and
hard process exits before/after commit. A full NWL23 test generates a real
pool, records a static fallback, commits through the arbiter, and reopens the
stored pool and receipt. Test manifest rates are synthetic, with no real API
request or performance claim. The complete suite passes 497 tests: 468 offline
and 29 native. Task 5 also checks a real native comparison through the ledger
and verifies that saved work/results and final-selection holds survive reopening. Wheel validation checks the installed migration resource and a
fresh schema without SDK/native import side effects.
