import os

# Ensure we rely on the [default] profile in ~/.aws, not any leftover env vars.
for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "AWS_DEFAULT_REGION", "AWS_REGION", "AWS_PROFILE"):
    os.environ.pop(var, None)

import boto3

session = boto3.Session()  # no profile name specified -> uses [default]
sts = session.client("sts")
identity = sts.get_caller_identity()

print("Region in use:", session.region_name)
print("Account ID:", identity["Account"])
print("ARN:", identity["Arn"])
print("UserId:", identity["UserId"])
