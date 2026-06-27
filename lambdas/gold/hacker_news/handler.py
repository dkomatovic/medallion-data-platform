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

PLATFORM = "Hacker News"


def load_silver(table_name):
    """Cita silver tabelu — sa S3 ili lokalno."""
    if S3_BUCKET_NAME:
        return wr.s3.read_parquet(
            path=f"s3://{S3_BUCKET_NAME}/silver/{table_name}/",
            dataset=True,
        )
    return pd.read_parquet(os.path.join(LOCAL_SILVER_PATH, table_name))


def write_gold(df, table_name, partition_cols):
    """Cuva gold tabelu — na S3 ili lokalno."""
    if df.empty:
        print(f"  '{table_name}' je prazna, preskacemo")
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
        print(f"  Sacuvano: {path} ({len(df)} redova)")
    else:
        path = os.path.join(LOCAL_OUTPUT, table_name)
        if os.path.exists(path):
            shutil.rmtree(path)
        os.makedirs(path)
        df.to_parquet(path, partition_cols=partition_cols, engine="pyarrow")
        print(f"  Sacuvano: {path} ({len(df)} redova)")


# METRIKE

def daily_post_counts(posts_df, date_str):
    """Dnevni broj objava po tipu (story, ask, comment, job, poll)."""
    counts = posts_df.groupby("post_type").size().reset_index(name="count")
    counts["date"] = date_str
    counts["platform"] = PLATFORM
    return counts[["date", "platform", "post_type", "count"]]


def daily_user_count(users_df, date_str):
    """Ukupan broj HN korisnika za taj dan."""
    return pd.DataFrame([{
        "date": date_str,
        "platform": PLATFORM,
        "total_users": len(users_df),
    }])


def top10_karma(users_df, date_str, ascending):
    """Top 10 korisnika po karma score-u (najveci ili najmanji)."""
    valid = users_df[users_df["karma_score"].notna()].copy()
    valid["karma_score"] = pd.to_numeric(valid["karma_score"], errors="coerce")
    valid = valid.dropna(subset=["karma_score"])
    top = (
        valid.nsmallest(10, "karma_score") if ascending
        else valid.nlargest(10, "karma_score")
    ).reset_index(drop=True)
    top["rank"] = range(1, len(top) + 1)
    top["date"] = date_str
    return top[["date", "rank", "username", "user_id", "karma_score"]]


def top10_jobs(posts_df, date_str):
    """Top 10 job ponuda po score-u. HN job postovi cesto nemaju score,
    pa sortiramo po score-u (null ide na kraj) i uzimamo top 10."""
    jobs = posts_df[posts_df["post_type"] == "job"].copy()
    jobs["score"] = pd.to_numeric(jobs["score"], errors="coerce")
    top = (jobs
           .sort_values("score", ascending=False, na_position="last")
           .head(10)
           .reset_index(drop=True))
    top["rank"] = range(1, len(top) + 1)
    top["date"] = date_str
    return top[["date", "rank", "post_id", "author_username", "content_text", "score"]]


def top10_stories(posts_df, date_str):
    """Top 10 objava (story/ask/poll) po score-u — komentari i job-ovi iskljuceni."""
    stories = posts_df[~posts_df["post_type"].isin(["comment", "job"])].copy()
    stories["score"] = pd.to_numeric(stories["score"], errors="coerce")
    top = stories.dropna(subset=["score"]).nlargest(
        10, "score").reset_index(drop=True)
    top["rank"] = range(1, len(top) + 1)
    top["date"] = date_str
    return top[["date", "rank", "post_id", "author_username", "content_text", "post_type", "score"]]


# KPI

def data_quality_score(posts_df, users_df, date_str):
    """Data Quality Score: % ne-null vrednosti po tabeli."""
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


# GLAVNA FUNKCIJA

def handler(event=None, context=None):
    vreme_start = datetime.now(timezone.utc)

    juce = datetime.now(timezone.utc) - timedelta(days=1)
    date_str = juce.strftime("%Y-%m-%d")

    print(f"Racunam HN gold metrike za: {date_str}")

    all_posts = load_silver("posts")
    posts_df = all_posts[all_posts["platform"] == PLATFORM].copy()

    all_users = load_silver("users")
    users_df = all_users[all_users["platform"] == PLATFORM].copy()

    print(f"  Ucitano: {len(posts_df)} posts, {len(users_df)} users")
    print()

    write_gold(daily_post_counts(posts_df, date_str),
               "hn_daily_post_counts", ["date"])
    write_gold(daily_user_count(users_df, date_str),
               "hn_daily_user_count", ["date"])
    write_gold(top10_karma(users_df, date_str, False),
               "hn_top10_karma_high", ["date"])
    write_gold(top10_karma(users_df, date_str, True),
               "hn_top10_karma_low", ["date"])
    write_gold(top10_jobs(posts_df, date_str), "hn_top10_jobs", ["date"])
    write_gold(top10_stories(posts_df, date_str), "hn_top10_stories", ["date"])
    write_gold(data_quality_score(posts_df, users_df, date_str),
               "hn_data_quality_score", ["date"])

    trajanje = (datetime.now(timezone.utc) - vreme_start).total_seconds()
    print(f"\nGotovo! Trajalo: {trajanje:.1f} sekundi")


if __name__ == "__main__":
    handler()
