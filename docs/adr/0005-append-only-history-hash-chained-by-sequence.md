# ADR-0005 — History is append-only and hash-chained over a monotonic sequence

- **Status:** accepted
- **Implemented by:** `marketlab.storage.database`, `marketlab.storage.events`, `marketlab.audit.roots`
- **Checked by:** `tests/unit/test_event_chain.py`, `tests/unit/test_daily_roots.py`, `tests/unit/test_interventions.py`

## Context

A prospective study's central claim is about *ordering*: the forecast was made
before the outcome was known, the pre-registration was written before the data
existed. That claim is only as good as the impossibility of editing the record
afterwards — including, and especially, by the study's own authors, who are the
people with both the access and the motive.

"We did not edit it" is not a checkable statement. Neither is "the code does
not contain an UPDATE".

## Options considered

**Discipline plus code review.** Rejected for the reason above.

**An ORM-level guard: no `UPDATE` statements in the codebase.** Rejected. It
constrains this codebase and not the `sqlite3` shell, which is the tool anyone
would actually reach for at 2am when a run has gone wrong.

**Write-once files instead of a database.** Genuinely appealing — a directory
of immutable JSON lines cannot be updated in place. Rejected on query cost: the
resolution pass, the analysis and the replay all need indexed lookups across
21 relations, and rebuilding an index over flat files on every command would
have made the platform unusable long before it made it trustworthy.

**Database triggers that refuse the operation, plus a hash chain that makes
tampering detectable even if the triggers are bypassed.** Chosen.

## Decision

### Refusal

Every scientific table — all 21 of them, listed in
`marketlab.storage.database.APPEND_ONLY_TABLES` — carries `BEFORE UPDATE` and
`BEFORE DELETE` triggers that raise. `Database.create_schema` verifies that
every name in that set corresponds to a table that actually exists, so a typo
in the list is a failure rather than a silently unprotected table.

There is exactly **one** escape hatch. `Database.migration_mode()` drops the
triggers, requires a reason and an author, records the intervention, and
restores them in a `finally` — so they come back even if the migration raises.
Any code that reaches for it is security-relevant by definition.

### Detection

The event log is hash-chained: each row carries the hash of the previous one,
so altering any historical event invalidates every hash after it. Daily root
hashes summarise each day's segment, so a verifier can check a range without
replaying the whole chain.

**The chain is ordered by a monotonic `seq`, not by a timestamp.** This is the
non-obvious part and it is load-bearing: the six arms of one cycle share a
decision instant, so timestamp order among them is undefined. A chain ordered
by timestamp would have no single valid linearisation, and "verification
failed" would depend on which order a reader happened to reconstruct.

The payload is hashed as **stored bytes**, never re-serialised for
verification. Re-serialising would mask a payload edited into a form that
happens to round-trip to the same object — which is precisely the edit a
motivated author would make.

## Consequences

**Nothing can be corrected in place.** A wrong row is superseded by a new row,
never fixed. This is felt most in the instrument repository: §7.1 asks for an
explicit validity period on each instrument version, and storing an
`effective_to` column would mean mutating the *previous* version's row the
moment a new one is written. So there is no such column — "current as of a
cutoff" is computed as the latest version whose `effective_from` does not
exceed it. Positions get the same treatment: immutable open/close events with
lots folded on read, rather than a `quantity_remaining` decremented in place.

**Storage grows monotonically.** No compaction, no retention policy. For a
120-session six-arm study this is megabytes and does not matter. For a
long-running platform it eventually would.

**Schema evolution is genuinely hard.** There is no Alembic migration and
`create_schema` builds the schema directly, which is fine while no study is
live. Once one is, `migration_mode()` is the audited window a migration runs
in, and a migration that rewrites scientific rows is a scientific act requiring
its own record.

**The chain proves tampering, not authorship.** Nothing is signed. Someone with
write access could rewrite history *and* recompute every hash, and the result
would verify. What the chain defends against is accidental corruption and
casual editing — not a determined author with the database file. Publishing
daily root hashes somewhere outside the repository (a commit, a timestamping
service) is what would close that, and it is not done.

## What would make us revisit this

- **The first real study going live.** At that point the daily root hash should
  be committed to git — or published somewhere the study's authors do not
  control — so the chain's head is witnessed externally at a known time.
  Without that, the append-only property is a local one.
- **A schema change during a live study.** The first use of `migration_mode()`
  in anger will show whether the escape hatch is usable or whether it is so
  awkward that someone reaches for `sqlite3` instead, which would be worse than
  not having it.
- **Storage becoming a real cost.** Retention would require deciding what may
  be dropped without weakening the record — a scientific decision, not an
  operational one, and one that would need its own record here.
