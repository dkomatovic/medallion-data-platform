import os
import shutil
import pandas as pd
import awswrangler as wr
from datetime import datetime, timedelta, timezone


S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))

LOCAL_SILVER_PATH = os.path.join(PROJECT_ROOT, "output", "silver")
LOCAL_OUTPUT = os.path.join(PROJECT_ROOT, "output", "gold")

PLATFORM = "X"


def load_silver(table_name):
    """Reads silver table or local folder"""
    if S3_BUCKET_NAME:
        return wr.s3.read_parquet(
            path=f"s3://{S3_BUCKET_NAME}/silver/{table_name}/",
            dataset=True,
        )
    return pd.read_parquet(os.path.join(LOCAL_SILVER_PATH, table_name))


def write_gold(df, table_name, partition_cols):
    """Saves gold table to S3 or locally"""
    if df.empty:
        print(f"  '{table_name}' empty, skipping")
        return
    if S3_BUCKET_NAME:
        path = f"s3://{S3_BUCKET_NAME}/gold/{table_name}/"
        wr.s3.to_parquet(
            df=df,
            path=path,
            dataset=True,
            partition_cols=partition_cols,
            mode="overwrite_partitions",
        )
        print(f"  Saved: {path} ({len(df)} rows)")
    else:
        path = os.path.join(LOCAL_OUTPUT, table_name)
        if os.path.exists(path):
            shutil.rmtree(path)
        os.makedirs(path)
        df.to_parquet(path, partition_cols=partition_cols, engine="pyarrow")
        print(f"  Saved: {path} ({len(df)} rows)")


# metrics
def daily_user_count(users_df, date_str):
    """
    Total number of users and number of new users (registered that day).
    """
    new_users = users_df[
        users_df["created_at"].notna() &
        users_df["created_at"].str.startswith(date_str)
    ].shape[0]
    return pd.DataFrame([{
        "date": date_str,
        "platform": PLATFORM,
        "total_users": len(users_df),
        "new_users": new_users,
    }])


def top10_followers(users_df, date_str):
    """
    First 10 users with largest follower count.
    """
    valid = users_df[users_df["followers_count"].notna()].copy()
    valid["followers_count"] = pd.to_numeric(valid["followers_count"], errors="coerce")
    valid = valid.dropna(subset=["followers_count"])
    top = (
        valid.nlargest(10, "followers_count")
        .reset_index(drop=True)
    )
    top["rank"] = range(1, len(top) + 1)
    top["date"] = date_str
    # Keep just what we need
    return top[["date", "rank", "username", "user_id", "followers_count"]]



def data_quality_score(posts_df, users_df, date_str):
    """
    Data Quality Score: % not-null values.
    """
    rows = []
    for table_name, df in [("posts", posts_df), ("users", users_df)]:
        total = df.size
        non_null = int(df.notna().sum().sum())
        rows.append({
            "date": date_str,
            "platform": PLATFORM,
            "table_name": table_name,
            "total_rows": len(df),
            "total_values": int(total),
            "non_null_values": non_null,
            "dqs_percent": round(non_null / total * 100, 2) if total > 0 else 0.0,
        })
    return pd.DataFrame(rows)


def handler(event=None, context=None):
    start_time = datetime.now(timezone.utc)

    print("Calculating X (Twitter) gold metrics for the latest date")

    # Load silver x data
    all_posts = load_silver("posts")
    posts_df = all_posts[all_posts["platform"] == PLATFORM].copy()

    all_users = load_silver("users")
    users_df = all_users[all_users["platform"] == PLATFORM].copy()

    # If no data, stop
    if posts_df.empty or users_df.empty:
        print("  No data.")
        return {"status": "no_data"}

    # Find max date in data
    max_created = posts_df["created_at"].max()
    if pd.isna(max_created):
        
        max_created = users_df["created_at"].max()
        if pd.isna(max_created):
            print("  No valid dates in data.")
            return {"status": "no_date"}

    # Convert to YYYY-MM-DD
    date_str = pd.to_datetime(max_created).strftime("%Y-%m-%d")

    print(f"  Latest date in data: {date_str}")
    print(f"  Loaded: {len(posts_df)} posts, {len(users_df)} users")
    print()

    # 1. Dayly user num
    write_gold(
        daily_user_count(users_df, date_str),
        "x_daily_user_count",
        ["date"]
    )

    # 2. Top 10 by follower count
    write_gold(
        top10_followers(users_df, date_str),
        "x_top10_followers",
        ["date"]
    )

    # 3. Data Quality Score
    write_gold(
        data_quality_score(posts_df, users_df, date_str),
        "x_data_quality_score",
        ["date"]
    )

    duration = (datetime.now(timezone.utc) - start_time).total_seconds()
    print(f"\Done! Lasted: {duration:.1f} seconds")
    return {"status": "ok", "date": date_str}

def check():
    import pandas as pd
    df = pd.read_parquet("output/gold/x_daily_user_count/")
    print(df)
    df = pd.read_parquet("output/gold/x_top10_followers/")
    print(df)
    df = pd.read_parquet("output/gold/x_data_quality_score/")
    print(df)

if __name__ == "__main__":
    handler()
    check()