# Medallion rules

Five non-negotiable principles. Violating any one of these causes silent data corruption or
unbounded recompute.

## 1. Watermarks

Every incremental job must read and advance a watermark. The watermark is the exclusive lower
bound for the next run.

- Read watermark before scanning source.
- Advance watermark to `max(ingested_at)` of the batch written, not to "now".
- If no rows were written, do not advance the watermark.
- Store watermarks in `lakestore/watermarks.py` — do not embed them in job state.

## 2. Single source of truth

Each table has exactly one writer. No two jobs write to the same Iceberg table.

- Bronze tables: ingest jobs only.
- Silver tables: the designated transform job for that table only.
- Gold tables: the designated aggregate job only.

## 3. Keyed-delta aggregates

Gold aggregates must be keyed deltas, not full rewrites. A job that rewrites the whole gold table
on every tick is a bug, not a feature.

- Identify the aggregate key (e.g. `(date, user_id)`).
- Write only the changed keys.
- Use overwrite-with-predicate, not full overwrite.

## 4. Schema fences

Each layer owns its schema. Silver cannot assume bronze schema; gold cannot assume silver schema.

- Add explicit schema validation at layer boundaries (`lakestore/schemas/`).
- Schema changes require a migration entry in `lakestore/migrations/`.
- Never pass a `**kwargs` dict across layer boundaries as a schema substitute.

## 5. Lane isolation

Bronze, silver, and gold jobs run independently. No job spans multiple layers in one transaction.

- Each job reads from exactly one layer and writes to exactly one layer.
- If a job reads silver and writes both silver and gold, split it.
- Failure in one lane must not corrupt another lane.
