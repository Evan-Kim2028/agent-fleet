# Polars patterns

Patterns for Polars-based transforms in the lakestore package.

## Lazy scan from Iceberg via PyArrow

```python
import polars as pl
from pyiceberg.catalog import load_catalog

catalog = load_catalog("default")
tbl = catalog.load_table("silver.events")
arrow = tbl.scan().to_arrow()
df = pl.from_arrow(arrow).lazy()
```

## Watermark-gated incremental scan

Always scope scans to records after the last watermark — never scan the whole table.

```python
from lakestore.watermarks import get_watermark

wm = get_watermark("silver.events")
arrow = tbl.scan(row_filter=f"ingested_at > '{wm}'").to_arrow()
df = pl.from_arrow(arrow).lazy()
```

## Dedup by key within a batch

```python
df = df.sort("updated_at", descending=True).unique(subset=["event_id"], keep="first")
```

## Join patterns

Prefer left joins when enriching silver with dimension tables. Log unmatched keys; do not drop
them silently.

```python
result = events.join(dim_users, on="user_id", how="left")
```

## Writing back to Iceberg

Collect the lazy frame before writing — PyIceberg expects Arrow tables, not Polars lazy frames.

```python
out = df.collect().to_arrow()
tbl.append(out)
```

## Null handling

Use `fill_null` with a sentinel, not `drop_nulls`, unless the schema contract explicitly forbids
nulls. Dropping nulls silently reduces row counts and breaks downstream joins.
