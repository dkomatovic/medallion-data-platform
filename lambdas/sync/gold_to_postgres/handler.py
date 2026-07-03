import json
import os

import awswrangler as wr
import boto3
import pg8000

S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")
POSTGRES_HOST_PARAM = os.environ.get("POSTGRES_HOST_PARAM", "/medallion/postgres/host")
POSTGRES_PASSWORD_PARAM = os.environ.get("POSTGRES_PASSWORD_PARAM", "/medallion/postgres/password")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "medallion")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "medallion")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "gold")

GOLD_TABLES = [
    "hn_daily_post_counts",
    "hn_daily_user_count",
    "hn_top10_karma_high",
    "hn_top10_karma_low",
    "hn_top10_jobs",
    "hn_top10_stories",
    "hn_data_quality_score",
    "x_daily_user_count",
    "x_top10_followers",
    "x_data_quality_score",
]


def _get_ssm_param(name, decrypt=False):
    return boto3.client("ssm").get_parameter(
        Name=name,
        WithDecryption=decrypt,
    )["Parameter"]["Value"]


def _postgres_connection():
    host = _get_ssm_param(POSTGRES_HOST_PARAM)
    password = _get_ssm_param(POSTGRES_PASSWORD_PARAM, decrypt=True)

    if not host or host == "UNSET":
        raise ValueError(
            f"PostgreSQL host nije konfigurisan ({POSTGRES_HOST_PARAM}). "
            "Proverite da li je EC2 instanca pokrenuta."
        )
    if not password or password == "UNSET":
        raise ValueError(
            f"PostgreSQL password nije konfigurisan ({POSTGRES_PASSWORD_PARAM})."
        )

    return pg8000.connect(
        host=host,
        port=POSTGRES_PORT,
        user=POSTGRES_USER,
        password=password,
        database=POSTGRES_DB,
    )


def _sync_table(con, table_name):
    s3_path = f"s3://{S3_BUCKET_NAME}/gold/{table_name}/"
    try:
        df = wr.s3.read_parquet(path=s3_path, dataset=True)
    except Exception as exc:
        print(f"  Preskacem '{table_name}': nema podataka ({exc})")
        return {"table": table_name, "rows": 0, "status": "skipped"}

    if df.empty:
        print(f"  Preskacem '{table_name}': prazna tabela")
        return {"table": table_name, "rows": 0, "status": "empty"}

    wr.postgresql.to_sql(
        df=df,
        con=con,
        table=table_name,
        schema=POSTGRES_SCHEMA,
        mode="overwrite",
        index=False,
    )
    print(f"  Sinhronizovano '{table_name}': {len(df)} redova")
    return {"table": table_name, "rows": len(df), "status": "synced"}


def handler(event=None, context=None):
    print(f"Sync gold -> PostgreSQL (bucket={S3_BUCKET_NAME})")

    con = _postgres_connection()
    try:
        cur = con.cursor()
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {POSTGRES_SCHEMA}")
        con.commit()
        cur.close()

        results = []
        for table in GOLD_TABLES:
            results.append(_sync_table(con, table))
    finally:
        con.close()

    synced = sum(1 for r in results if r["status"] == "synced")
    total_rows = sum(r["rows"] for r in results)
    summary = {
        "status": "ok",
        "tables_synced": synced,
        "total_rows": total_rows,
        "details": results,
    }
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    handler()