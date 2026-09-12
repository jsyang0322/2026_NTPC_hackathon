"""
Build a SageMaker XGBoost-ready training CSV from the Iris dataset,
create an S3 bucket in us-west-2, upload the file, and print the S3 URI.

Requirements: boto3, scikit-learn (installed into the .venv).
Credentials/region come from the [default] profile in ~/.aws.
"""

import csv
import sys

import boto3
from botocore.exceptions import ClientError
from sklearn.datasets import load_iris

REGION = "us-west-2"
# Simple, recognizable bucket name. Bucket names must be globally unique,
# lowercase, 3-63 chars. We append the account ID to reduce collision risk.
BUCKET_BASENAME = "kiro-workshop-iris"
TRAIN_FILE = "train.csv"
S3_KEY = "iris/train.csv"


def build_training_csv(path: str) -> int:
    """Write Iris as SageMaker XGBoost CSV: no header, first column = numeric label."""
    data = load_iris()
    X = data.data          # 150 rows, 4 features
    y = data.target        # integer labels 0/1/2

    # ~120 rows: take the first 40 of each of the 3 classes (stratified, balanced).
    rows = []
    for cls in (0, 1, 2):
        idx = [i for i, label in enumerate(y) if label == cls][:40]
        for i in idx:
            rows.append([int(y[i])] + [float(v) for v in X[i]])

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)  # label first, no header
    return len(rows)


def ensure_bucket(s3, bucket: str) -> None:
    """Create the bucket in us-west-2 (requires LocationConstraint), idempotently."""
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"Bucket already exists and is accessible: {bucket}")
        return
    except ClientError:
        pass  # fall through to create

    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    s3.get_waiter("bucket_exists").wait(Bucket=bucket)
    print(f"Created bucket: {bucket}")


def main() -> int:
    session = boto3.Session(region_name=REGION)

    account_id = session.client("sts").get_caller_identity()["Account"]
    bucket = f"{BUCKET_BASENAME}-{account_id}"

    n = build_training_csv(TRAIN_FILE)
    print(f"Wrote {TRAIN_FILE} with {n} rows (no header, first column = label).")

    s3 = session.client("s3")
    ensure_bucket(s3, bucket)

    s3.upload_file(TRAIN_FILE, bucket, S3_KEY)
    uri = f"s3://{bucket}/{S3_KEY}"
    print("Uploaded training data.")
    print(f"S3 path: {uri}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
