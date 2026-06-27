import os
import re
import json
import html
import uuid
import shutil
import boto3
import requests
import pandas as pd
import awswrangler as wr
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ako je postavljena env varijabla, cuvamo na S3, inace lokalno
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")

# Putanje racunamo u odnosu na sam fajl (ne na cwd odakle se pokrece skript),
# tako da rade bez obzira odakle pozivas "python handler.py"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))

# Bronze HN handler pise lokalno u <project_root>/output/<datum>/<tip>.json
LOCAL_BRONZE_PATH = os.path.join(PROJECT_ROOT, "output")
LOCAL_OUTPUT = os.path.join(PROJECT_ROOT, "output", "silver")

TIPOVI = ["story", "ask_hn", "comment", "job", "poll"]

# Mapiranje HN tag-a u nas post_type
POST_TYPE_MAP = {
    "story": "story",
    "ask_hn": "ask",
    "comment": "comment",
    "job": "job",
    "poll": "poll",
}

PLATFORM = "Hacker News"
# fiksni namespace za deterministicke ID-eve
NAMESPACE_UUID = uuid.UUID("a6edc906-2f9f-5993-bf3a-ef74fa10c10b")


# CITANJE SIROVIH PODATAKA

def load_bronze_data(date_str):
    """Cita sirove JSON fajlove (5 tipova) — sa S3 ili lokalno."""
    raw_by_type = {}

    for tip in TIPOVI:
        if S3_BUCKET_NAME:
            s3_key = f"bronze/hacker-news/{date_str}/{tip}.json"
            try:
                odgovor = boto3.client("s3").get_object(
                    Bucket=S3_BUCKET_NAME, Key=s3_key)
                raw_by_type[tip] = json.loads(odgovor["Body"].read())
            except boto3.client("s3").exceptions.NoSuchKey:
                print(f"  Nema bronze fajla za '{tip}', preskacemo")
                raw_by_type[tip] = []
        else:
            putanja = os.path.join(LOCAL_BRONZE_PATH, date_str, f"{tip}.json")
            if os.path.exists(putanja):
                with open(putanja, "r", encoding="utf-8") as f:
                    raw_by_type[tip] = json.load(f)
            else:
                print(f"  Nema lokalnog fajla za '{tip}', preskacem")
                raw_by_type[tip] = []

    return raw_by_type


# NORMALIZACIJA


def clean_html(text):
    """Uklanja HTML tagove i HTML entitete"""
    if not text:
        return ""
    bez_tagova = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(bez_tagova).strip()


def unix_to_iso(timestamp):
    """Konvertuje Unix timestamp u UTC ISO string"""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_content_text(item):
    """HN objave imaju sadrzaj u razlicitim poljima zavisno od tipa"""
    sadrzaj = item.get("comment_text") or item.get(
        "story_text") or item.get("title") or ""
    return clean_html(sadrzaj)


def get_user_id(username):
    """Deterministicki UUID — isti username uvek dobija isti ID"""
    return str(uuid.uuid5(NAMESPACE_UUID, f"{PLATFORM}:{username}"))


# IZGRADNJA POSTS TABELE

def build_posts(raw_by_type, date_str):
    """Pretvara sirove HN objave u normalizovanu posts tabelu"""
    posts = []
    seen_ids = set()

    for tip, objave in raw_by_type.items():
        for item in objave:
            post_id = item.get("objectID")
            author = item.get("author")

            # Bez ID-ja ili autora ne moze
            if not post_id or not author:
                continue

            # Uklanjanje duplikata
            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            created_at_i = item.get("created_at_i")

            posts.append({
                "post_id": post_id,
                "author_username": author,
                "platform": PLATFORM,
                "content_text": get_content_text(item),
                "created_at": unix_to_iso(created_at_i),
                "post_type": POST_TYPE_MAP.get(tip, tip),
                "score": item.get("points"),
                "year": int(date_str[0:4]),
                "month": int(date_str[5:7]),
                "day": int(date_str[8:10]),
            })

    return pd.DataFrame(posts)


# IZGRADNJA USERS TABELE (sa karma_score preko HN API-ja)

def fetch_user_info(username):
    """Poziva HN Firebase API da dobije karma i datum kreiranja naloga.

    Napomena: Algolia /users/ endpoint NEMA created_at polje (samo about,
    karma, username), pa za datum kreiranja naloga koristimo zvanicni
    Firebase API koji vraca 'created' kao Unix timestamp.
    """
    try:
        odgovor = requests.get(
            f"https://hacker-news.firebaseio.com/v0/user/{username}.json",
            timeout=5,
        )
        if odgovor.status_code != 200:
            return username, None, None
        podaci = odgovor.json()
        if podaci is None:
            return username, None, None
        karma = podaci.get("karma")
        created_at = unix_to_iso(podaci.get("created"))
        return username, karma, created_at
    except requests.RequestException:
        return username, None, None


def build_users(posts_df):
    """Pravi users tabelu — jedinstveni korisnici + karma preko API-ja (paralelno)."""
    if posts_df.empty or "author_username" not in posts_df.columns:
        return pd.DataFrame(columns=["user_id", "username", "platform", "karma_score", "is_verified", "created_at"])

    jedinstveni_useri = posts_df["author_username"].dropna().unique().tolist()

    karma_po_korisniku = {}

    # Paralelni pozivi (20 odjednom) da ne traje predugo
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(fetch_user_info, u)
                   for u in jedinstveni_useri]
        for future in as_completed(futures):
            username, karma, created_at = future.result()
            karma_po_korisniku[username] = (karma, created_at)

    users = []
    for username in jedinstveni_useri:
        karma, created_at = karma_po_korisniku.get(username, (None, None))
        users.append({
            "user_id": get_user_id(username),
            "username": username,
            "platform": PLATFORM,
            "karma_score": karma,
            "is_verified": None,  # HN nema verifikaciju, samo X
            "created_at": created_at,
        })

    return pd.DataFrame(users)


# UPIS U PARQUET

def write_table(df, table_name, partition_cols):
    """Cuva DataFrame u parquet — na S3 ili lokalno"""
    if df.empty:
        print(f"  '{table_name}' je prazna, preskocen upis")
        return

    if S3_BUCKET_NAME:
        putanja = f"s3://{S3_BUCKET_NAME}/silver/{table_name}/"
        wr.s3.to_parquet(
            df=df,
            path=putanja,
            dataset=True,
            partition_cols=partition_cols,
            mode="overwrite_partitions",
        )
        print(f"  Sacuvano: {putanja} ({len(df)} redova)")
    else:
        putanja = os.path.join(LOCAL_OUTPUT, table_name)
        if os.path.exists(putanja):
            shutil.rmtree(putanja)
        os.makedirs(putanja)
        df.to_parquet(putanja, partition_cols=partition_cols, engine="pyarrow")
        print(f"  Sacuvano: {putanja} ({len(df)} redova)")


# GLAVNA FUNKCIJA

def handler(event=None, context=None):  # event i context su obavezni Lambda parametri
    vreme_start = datetime.now(timezone.utc)

    juce = datetime.now(timezone.utc) - timedelta(days=1)
    date_str = juce.strftime("%Y-%m-%d")

    print(f"Normalizujem HN podatke za: {date_str}")
    print("-" * 40)

    raw_by_type = load_bronze_data(date_str)

    posts_df = build_posts(raw_by_type, date_str)
    print(f"  Ukupno posts: {len(posts_df)}")

    users_df = build_users(posts_df)
    print(f"  Ukupno users: {len(users_df)}")

    write_table(posts_df, "posts", partition_cols=["year", "month", "day"])
    write_table(users_df, "users", partition_cols=["platform"])

    trajanje = (datetime.now(timezone.utc) - vreme_start).total_seconds()
    print(f"Gotovo! Trajalo: {trajanje:.1f} sekundi")


if __name__ == "__main__":
    handler()
