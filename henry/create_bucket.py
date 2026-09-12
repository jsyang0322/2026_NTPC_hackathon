"""Create an S3 bucket in us-west-2 using boto3 (requires LocationConstraint).

Block Public Access is enabled explicitly after creation, as required by the
hackathon rules (no publicly accessible buckets).
"""
import sys
import boto3
from botocore.exceptions import ClientError

REGION = "us-west-2"
BASE_NAME = "kiro-workshop-henry"


def main() -> int:
    session = boto3.Session(region_name=REGION)

    # Confirm which identity/account we're operating as.
    ident = session.client("sts").get_caller_identity()
    account_id = ident["Account"]
    print("Account:", account_id)
    print("Caller ARN:", ident["Arn"])

    # Bucket names are globally unique; append account id to the recognizable base.
    bucket = f"{BASE_NAME}-{account_id}"

    s3 = session.client("s3")
    try:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        s3.get_waiter("bucket_exists").wait(Bucket=bucket)
        print("Created bucket:", bucket)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        print("Bucket already owned by you:", bucket)
    except ClientError as e:
        print("ERROR creating bucket:", e.response["Error"].get("Code"),
              "-", e.response["Error"].get("Message"))
        return 1

    # 競賽規範第 1 點：不得建立公開對外的 S3 Bucket。
    # 不依賴 AWS 帳戶層級預設，於程式中明確開啟 Block Public Access（四項全開）。
    # 對既有 bucket 重跑亦為幂等操作。
    try:
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        print("Block Public Access: enabled (all four settings)")
    except ClientError as e:
        print("ERROR enabling Block Public Access:", e.response["Error"].get("Code"),
              "-", e.response["Error"].get("Message"))
        return 1

    print("Bucket ARN: arn:aws:s3:::" + bucket)
    print("Region:", REGION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
