import os
from datetime import UTC, datetime, timedelta

import boto3

DELETION_THRESHOLD_DAYS = 7
# Load config from environment
ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
STAGING_BUCKET = os.environ["R2_STAGING_BUCKET"]
ENDPOINT_URL = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"

print("--- Starting Staging Bucket Cleanup ---")
print(f"Bucket: {STAGING_BUCKET}")
print(f"Deletion Threshold: {DELETION_THRESHOLD_DAYS} days")

client = boto3.client(
    "s3",
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=ACCESS_KEY_ID,
    aws_secret_access_key=SECRET_ACCESS_KEY,
)

paginator = client.get_paginator("list_objects_v2")
pages = paginator.paginate(Bucket=STAGING_BUCKET)

objects_to_delete = []
now = datetime.now(UTC)
threshold = now - timedelta(days=DELETION_THRESHOLD_DAYS)

for page in pages:
    if "Contents" not in page:
        continue
    for obj in page["Contents"]:
        if obj["LastModified"] < threshold:
            objects_to_delete.append({"Key": obj["Key"]})
            print(
                f"  - Marking for deletion: {obj['Key']} (last modified: {obj['LastModified']})"
            )

if not objects_to_delete:
    print("\nNo old objects found to delete. Exiting.")
    exit(0)

print(f"\nFound {len(objects_to_delete)} objects to delete. Proceeding...")

# Boto3 can delete up to 1000 objects in a single request
for i in range(0, len(objects_to_delete), 1000):
    chunk = objects_to_delete[i : i + 1000]
    response = client.delete_objects(
        Bucket=STAGING_BUCKET, Delete={"Objects": chunk, "Quiet": True}
    )
    if response.get("Errors"):
        print("  ❌ ERROR during batch deletion:")
        for error in response["Errors"]:
            print(
                f"    - Key: {error['Key']}, Code: {error['Code']}, Message: {error['Message']}"
            )
        exit(1)

print(f"✅ Successfully deleted {len(objects_to_delete)} objects.")
print("--- Cleanup Complete ---")
