import requests
import json
import os
import boto3
from datetime import datetime, timedelta, timezone


# Tipovi objava koje prikupljamo sa Hacker News
TIPOVI = ["story", "ask_hn", "comment", "job", "poll"]

# Ako je postavljena env varijabla, cuvamo na S3, inace lokalno
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")
OUTPUT_FOLDER = "output"


def get_yesterday_range():
    """Vraca Unix timestamp za pocetak i kraj jucerasnjeg dana (UTC)."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    start = yesterday.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    end = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(start.timestamp()), int(end.timestamp())


def get_hourly(type, ys_start, ys_end):
    """Prikuplja sve objave za jedan vremenski prozor (max 1000 po upitu)."""
    all_posts = []
    pages = 0

    while True:
        response = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "tags": type,
                "numericFilters": f"created_at_i>{ys_start},created_at_i<{ys_end}",
                "hitsPerPage": 1000,
                "page": pages,
            }
        )

        if response.status_code != 200:
            print(f"  GRESKA: API vratio status {response.status_code}")
            break

        response.encoding = "utf-8"
        data = response.json()
        posts = data.get("hits", [])
        all_posts.extend(posts)

        # Ako nema vise stranica, stajemo
        if pages >= data.get("nbPages", 1) - 1:
            break

        pages += 1

    return all_posts


def get_by_type(type, start, end):
    """Prikuplja sve objave zadatog tipa — sat po sat da ne hitujemo limit od 1000."""
    all_posts = []
    seen_ids = set()  # za uklanjanje duplikata

    print(f"  Prikupljam '{type}'...")

    # Delimo dan na prozore od po 1 sat (24 upita)
    SAT = 3600
    current = start

    while current < end:
        next = min(current + SAT, end)

        posts_in_hour = get_hourly(type, current, next)

        # Dodajemo samo objave koje jos nismo videli
        nove = 0
        for objava in posts_in_hour:
            oid = objava.get("objectID")
            if oid not in seen_ids:
                seen_ids.add(oid)
                all_posts.append(objava)
                nove += 1

        if posts_in_hour:
            print(f"    {current} → {next}: {nove} objava")

        current = next

    print(f"  Ukupno '{type}': {len(all_posts)} objava")
    return all_posts


def save(type, posts, date_str):
    """Cuva JSON — na S3 ako smo na AWS-u, lokalno ako testiramo."""
    data = json.dumps(posts, ensure_ascii=False, indent=2)

    if S3_BUCKET_NAME:
        # === AWS: cuvamo u S3 ===
        s3_key = f"bronze/hacker-news/{date_str}/{type}.json"
        boto3.client("s3").put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=data.encode("utf-8"),
            ContentType="application/json",
        )
        print(f"  Sacuvano: s3://{S3_BUCKET_NAME}/{s3_key}")
    else:
        # === Lokalno: cuvamo na disk ===
        folder = os.path.join(OUTPUT_FOLDER, date_str)
        os.makedirs(folder, exist_ok=True)
        putanja = os.path.join(folder, f"{type}.json")
        with open(putanja, "w", encoding="utf-8") as f:
            f.write(data)
        print(f"  Sacuvano: {putanja}")


def handler(event=None, context=None):
    """Glavna funkcija — Lambda handler na AWS-u."""
    vreme_start = datetime.now(timezone.utc)

    start, end = get_yesterday_range()
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")

    print(f"Prikupljam podatke za: {date_str}")
    print(f"Vremenski opseg: {start} → {end}")
    print("-" * 40)

    for type in TIPOVI:
        posts = get_by_type(type, start, end)
        save(type, posts, date_str)
        print()

    duration = (datetime.now(timezone.utc) - vreme_start).total_seconds()
    print(f"Gotovo! Trajalo: {duration:.1f} sekundi")


# Pokretanje lokalno (python handler.py)
if __name__ == "__main__":
    handler()
