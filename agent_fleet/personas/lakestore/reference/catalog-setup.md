# Catalog setup

How lakestore resolves and connects to the Iceberg catalog.

## Config location

Active catalog type is set in `lakestore/config.py` via `LAKESTORE_CATALOG` env var.
Supported values: `rest`, `glue`, `lakekeeper`, `sqlite` (local dev only).

## REST catalog

```python
from pyiceberg.catalog import load_catalog

catalog = load_catalog(
    "default",
    **{
        "type": "rest",
        "uri": "https://your-catalog-host/api/catalog",
        "credential": "...",
        "warehouse": "s3://your-bucket/warehouse",
    },
)
```

## Glue catalog

```python
catalog = load_catalog(
    "glue",
    **{
        "type": "glue",
        "warehouse": "s3://your-bucket/warehouse",
    },
)
```

AWS credentials come from the environment (IAM role / `AWS_*` env vars).

## Local dev (SQLite catalog)

```python
catalog = load_catalog(
    "local",
    **{
        "type": "sql",
        "uri": "sqlite:///lakestore_dev.db",
        "warehouse": "/tmp/lakestore_warehouse",
    },
)
```

The SQLite catalog is used by tests and local development. Never commit `lakestore_dev.db`.

## Lakekeeper

Lakekeeper is a REST-compatible catalog with branching (WAP) support. Connect with the REST
adapter but set `lakekeeper.branch` for WAP workflows.

## Namespace conventions

| Namespace | Purpose |
|-----------|---------|
| `bronze`  | Raw ingest tables |
| `silver`  | Cleaned and keyed tables |
| `gold`    | Aggregated/reporting tables |
