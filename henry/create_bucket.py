"""Create an S3 bucket in us-west-2 using boto3 (requires LocationConstraint)."""
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

    print("Bucket ARN: arn:aws:s3:::" + bucket)
    print("Region:", REGION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
