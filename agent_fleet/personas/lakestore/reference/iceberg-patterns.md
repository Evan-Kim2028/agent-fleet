# Iceberg patterns

Patterns for PyIceberg operations in the lakestore package.

## Append (bronze ingest)

Prefer `append` for raw ingest — never overwrite bronze. Overwrite destroys lineage.

```python
tbl = catalog.load_table("bronze.events")
tbl.append(df.to_arrow())
```

## Overwrite with predicate (silver correction)

Use overwrite with an explicit `delete_where` predicate when correcting silver rows by key.
Never overwrite the full table — it silently discards unrelated partitions.

```python
tbl = catalog.load_table("silver.events")
tbl.overwrite(new_rows.to_arrow(), overwrite_filter=("date", "=", target_date))
```

## Schema evolution

Add columns via `UpdateSchema`, never by dropping and recreating the table.

```python
with tbl.update_schema() as upd:
    upd.add_column("new_col", IntegerType(), "Optional description")
```

After adding a column, update the silver migration record in `lakestore/migrations/`.

## Compaction

Run compaction after large appends to avoid small-file proliferation:

```python
from pyiceberg.table import Table
tbl.rewrite_data_files()
```

Schedule: compaction is handled by `lakestore/maintenance.py`. Do not inline it into ingest jobs.

## Expire snapshots

Keep 30 days of snapshot history by default:

```python
tbl.expire_snapshots().expire_older_than(max_snapshot_age_ms=30 * 24 * 60 * 60 * 1000).commit()
```

## Orphan file cleanup

Run after failed writes or aborted jobs:

```python
tbl.delete_orphan_files().older_than(max_file_age_ms=3 * 24 * 60 * 60 * 1000).execute()
```

## Reading snapshots for watermark inspection

```python
import duckdb
con = duckdb.connect()
con.execute("INSTALL iceberg; LOAD iceberg;")
result = con.execute("SELECT * FROM iceberg_snapshots('s3://bucket/warehouse/ns/table/')").fetchdf()
```
