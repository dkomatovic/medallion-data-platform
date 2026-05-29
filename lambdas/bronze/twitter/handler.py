import os
from datetime import datetime, timezone
import boto3
import pandas as pd
from datasets import load_dataset
from dotenv import load_dotenv
load_dotenv()

data_sites = [
    {
        "name": "twitter_icoar",                     # Name used in S3 path
        "source": "huggingface",
        "repo_id": "majinwakeup/ICOAR-DATA",        
        "split": "train",                           
        "format": "csv"                             
    },
    {
        "name": "chatgpt_tweets",
        "source": "huggingface",
        "repo_id": "MouezYazidi/ChatGPT_tweets",         
        "split": "train",
        "format": "csv",
    }
]

S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")
OUTPUT_FOLDER = "output"


def save(data, dataset_name, date_str, file_format="csv"):
    """
    Saves data to S3 (if S3_BUCKET_NAME is set) or locally.
    data can be pandas DataFrame or string.
    """
    s3_key = f"bronze/x/{dataset_name}/{date_str}/{dataset_name}.{file_format}"

    if isinstance(data, pd.DataFrame):
        if file_format == "csv":
            body = data.to_csv(index=False).encode("utf-8")
            content_type = "text/csv"
        elif file_format == "json":
            body = data.to_json(orient="records", indent=2).encode("utf-8")
            content_type = "application/json"
        else:
            raise ValueError(f"Unsupported format: {file_format}")
    else:
        # If string 
        body = data.encode("utf-8")
        content_type = "text/plain"

    if S3_BUCKET_NAME:
        # Saving to S3
        s3_client = boto3.client("s3")
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=body,
            ContentType=content_type,
        )
        print(f"Saved to S3: s3://{S3_BUCKET_NAME}/{s3_key}")
    else:
        # Saving locally
        folder = os.path.join(OUTPUT_FOLDER, "x", dataset_name, date_str)
        os.makedirs(folder, exist_ok=True)
        putanja = os.path.join(folder, f"{dataset_name}.{file_format}")
        with open(putanja, "wb") as f:
            f.write(body)
        print(f"Saved locally: {putanja}")


def download_from_huggingface(repo_id, split, rename_map=None):
    """
    Download dataset from Hugging Face Hub-a and returnes pandas DataFrame.
    """
    print(f"Downloading {repo_id} (split={split})...")
    dataset = load_dataset(repo_id, split=split)
    df = dataset.to_pandas()
    print(f"Downloaded {len(df)} rows, {len(df.columns)} columns.")

    return df


def handler(event=None, context=None):
    """Main function — Lambda handler on AWS"""
    time_start = datetime.now(timezone.utc)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"Pokretanje bronze pipeline-a za Twitter/X datasetove u {time_start} UTC")
    print(f"S3 bucket: {S3_BUCKET_NAME if S3_BUCKET_NAME else 'NIJE POSTAVLJEN (čuvam lokalno)'}")
    print("-" * 50)

    for site in data_sites:
        name = site["name"]
        print(f"\nDownloading: {name}")

        try:
            if site["source"] == "huggingface":
                df = download_from_huggingface(
                    repo_id=site["repo_id"],
                    split=site.get("split", "train")
                )
                save(df, name, date_str, file_format=site.get("format", "csv"))
            else:
                print(f"Unknown source: {site['source']}")
                continue

            print(f"Done with: {name}")

        except Exception as e:
            print(f"Error for {name}: {str(e)}")

    duration = (datetime.now(timezone.utc) - time_start).total_seconds()
    print(f"Finished! Lasted: {duration:.1f} seconds ")


if __name__ == "__main__":
    handler()