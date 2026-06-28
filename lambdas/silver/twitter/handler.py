import os
import re
import hashlib
import uuid
import shutil
import boto3
import pandas as pd
import awswrangler as wr
from datetime import datetime, timezone


S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))

LOCAL_BRONZE_PATH = os.path.join(PROJECT_ROOT, "output")       
LOCAL_OUTPUT = os.path.join(PROJECT_ROOT, "output", "silver")     

PLATFORM = "X"

# same namespace like hacker news
NAMESPACE_UUID = uuid.UUID("a6edc906-2f9f-5993-bf3a-ef74fa10c10b")


def get_user_id(username):
    """Deterministički UUID za X korisnika."""
    return str(uuid.uuid5(NAMESPACE_UUID, f"{PLATFORM}:{username}"))


def clean_text(text):
    """Čisti tekst X objave – uklanja URL-ove i višestruke razmake."""
    if not text:
        return ""
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def convert_x_date(date_str):
    """
    Converts different time strings UTC ISO-8601.
    Supports: %m/%d/%Y %H:%M  (npr. 7/25/2020 12:27)
              %m/%d/%Y %H:%M:%S
              %Y-%m-%d %H:%M:%S
    """
    if date_str is None:
        return None
    # If not string convert to string
    if not isinstance(date_str, str):
        # Check for Nan (pandas)
        try:
            if pd.isna(date_str):
                return None
        except:
            pass
        date_str = str(date_str)
    if date_str.strip() == "":
        return None
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


# loading bronze
def load_all_bronze_x():
    """
    Reading datasets from bronze it will take just the latest instance of the dataset since they are the same
    """
    all_dfs = []

    if S3_BUCKET_NAME:
        s3_client = boto3.client("s3")
        prefix = "bronze/x/"
        paginator = s3_client.get_paginator("list_objects_v2")

        # Getting all CSV keys
        csv_keys = []
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".csv"):
                    csv_keys.append(key)

        # Format: bronze/x/{dataset_name}/{date}/{dataset_name}.csv
        # Group by dataset_name
        datasets = {}
        for key in csv_keys:
            parts = key.split('/')
            if len(parts) >= 3:
                dataset_name = parts[2]  # bronze/x/{dataset_name}/...
                if dataset_name not in datasets:
                    datasets[dataset_name] = []
                datasets[dataset_name].append(key)

        # Getting first csv for every dataset
        for dataset_name, keys in datasets.items():
            if keys:
                # Get first key
                key = keys[0]
                try:
                    response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                    df = pd.read_csv(response["Body"])
                    all_dfs.append(df)
                    print(f"  Loaded: s3://{S3_BUCKET_NAME}/{key}, {len(df)} rows")
                except Exception as e:
                    print(f"  Error loading {key}: {e}")

    else:
        base_path = os.path.join(LOCAL_BRONZE_PATH, "x")
        if not os.path.exists(base_path):
            print(f"  Folder {base_path} doesn't exist.")
            return pd.DataFrame()

        # Iterating through all datasets
        for dataset_name in os.listdir(base_path):
            dataset_path = os.path.join(base_path, dataset_name)
            if not os.path.isdir(dataset_path):
                continue
            # Taking first subfolder
            date_folders = [d for d in os.listdir(dataset_path) if os.path.isdir(os.path.join(dataset_path, d))]
            if not date_folders:
                continue
            first_date = sorted(date_folders)[0]
            date_path = os.path.join(dataset_path, first_date)
            for fname in os.listdir(date_path):
                if fname.endswith(".csv"):
                    full_path = os.path.join(date_path, fname)
                    try:
                        df = pd.read_csv(full_path)
                        all_dfs.append(df)
                        print(f"  Loaded: {full_path}, {len(df)} rows")
                    except Exception as e:
                        print(f"  Error loading {full_path}: {e}")

    if all_dfs:
        return pd.concat(all_dfs, ignore_index=True)
    else:
        return pd.DataFrame()


def build_posts(original_df):
    """
    Turning raw x data to a normalized posts table
    Extract year/month/day from created_at for partitioning
    """
    if original_df.empty:
        return pd.DataFrame()

    posts = []
    seen_ids = set()

    for idx, row in original_df.iterrows():
        author = row.get("author_username", "")
        content = row.get("text", "")
        created_raw = row.get("date", "")
        is_retweet = row.get("is_retweet", False)

        raw_id = f"{author}|{created_raw}|{content}"
        post_id = hashlib.md5(raw_id.encode("utf-8")).hexdigest()

        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)

        created_at = convert_x_date(created_raw)
        if not created_at:
            continue

        try:
            dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
        except:
            continue
        year, month, day = dt.year, dt.month, dt.day

        post_type = "retweet" if is_retweet else "tweet"
        content_clean = clean_text(content)

        posts.append({
            "post_id": post_id,
            "author_username": author,
            "platform": PLATFORM,
            "content_text": content_clean,
            "created_at": created_at,
            "post_type": post_type,
            "score": None,
            "year": year,
            "month": month,
            "day": day,
        })

    return pd.DataFrame(posts)


def build_users(original_df, posts_df):
    """
    Kreira users tabelu iz originalnog X DataFrame-a.
    """
    if original_df.empty or posts_df.empty:
        return pd.DataFrame(columns=[
            "user_id", "username", "platform", "karma_score",
            "is_verified", "created_at", "followers_count"
        ])

    authors_in_posts = set(posts_df["author_username"])
    user_df = original_df[original_df["author_username"].isin(authors_in_posts)].copy()
    user_df = user_df.drop_duplicates(subset=["author_username"], keep="first")

    users = []
    for _, row in user_df.iterrows():
        username = row.get("author_username")
        if not username:
            continue

        user_created = convert_x_date(row.get("user_created", ""))

        is_verified = row.get("user_verified", False)
        if isinstance(is_verified, str):
            is_verified = is_verified.upper() == "TRUE"

        followers = row.get("user_followers")
        if followers is not None:
            try:
                followers = int(followers)
            except (ValueError, TypeError):
                followers = None

        users.append({
            "user_id": get_user_id(username),
            "username": username,
            "platform": PLATFORM,
            "karma_score": None,
            "is_verified": is_verified,
            "created_at": user_created,
            "followers_count": followers,
        })

    return pd.DataFrame(users)



def write_table(df, table_name, partition_cols):
    """Saves dataframe to parquet to S3 or locally"""

    if df.empty:
        print(f"  '{table_name}' empty, writing skipped")
        return

    if S3_BUCKET_NAME:
        path = f"s3://{S3_BUCKET_NAME}/silver/{table_name}/"
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


def handler(event=None, context=None):
    time_start = datetime.now(timezone.utc)

    print("Normalizing X (Twitter) data from bronze layer")
    print(f"PROJECT_ROOT = {PROJECT_ROOT}")
    print(f"LOCAL_BRONZE_PATH = {LOCAL_BRONZE_PATH}")
    print(f"LOCAL_OUTPUT = {LOCAL_OUTPUT}")
    print("-" * 40)

    # 1) Load CSVs
    original_df = load_all_bronze_x()
    print(f"  loaded total rows: {len(original_df)}")

    if original_df.empty:
        print("  No data to process.")
        return {"status": "no_data"}

    # 2) Build posts table
    posts_df = build_posts(original_df)
    print(f"  Total posts: {len(posts_df)}")

    # 3) Build users table
    users_df = build_users(original_df, posts_df)
    print(f"  Total users: {len(users_df)}")

    # 4) Save to Parquet
    write_table(posts_df, "posts", partition_cols=["year", "month", "day"])
    write_table(users_df, "users", partition_cols=["platform"])

    duration = (datetime.now(timezone.utc) - time_start).total_seconds()
    print(f"Done! Lasted: {duration:.1f} seconds")
    return {"status": "ok"}


if __name__ == "__main__":
    handler()