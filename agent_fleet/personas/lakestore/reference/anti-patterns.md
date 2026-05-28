# Anti-patterns

Known footguns in the lakestore codebase. Recognize these and fix them when asked.

## Global recompute disguised as incremental

A job that reads all rows from silver and rewrites all gold rows on every tick. The job may be
called "incremental" but it is a full recompute. Symptoms: gold job runtime grows with table size;
watermark is always set to "now" regardless of rows written.

Fix: scope the scan to rows after the watermark; write only changed keys.

## Schema drift across layers

Silver job reads bronze and writes silver columns without explicit schema validation. When bronze
adds a column, silver silently passes it through — or drops it, depending on select ordering.

Fix: add `lakestore/schemas/` validators at layer boundaries. Fail loudly on unexpected columns.

## Missing watermark update

Job completes successfully but does not advance the watermark. Next run re-processes the same
batch. Symptoms: duplicate rows in silver/gold; row counts growing faster than source.

Fix: always update the watermark inside the same transaction (or immediately after commit) as the
write. Never update the watermark before the write.

## Fan-out without isolation

One job writes to two tables — e.g. silver.events and silver.users — in a loop. If the second
write fails, the first write is committed but the watermark for the second table is not advanced.
Next run re-processes the first table again.

Fix: split into two jobs; each owns one table.

## Overwriting bronze

Bronze tables must be append-only. Overwriting bronze destroys the raw audit trail.

Fix: use `tbl.append()`, never `tbl.overwrite()`, for bronze.

## Using `drop_nulls` in transforms

`drop_nulls` silently removes rows that have null values in any column. In transforms, this
reduces row counts and breaks downstream joins without any error.

Fix: use `fill_null` with a sentinel or explicitly filter only the columns that must be non-null.
