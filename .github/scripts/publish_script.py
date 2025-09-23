import os
import json
import subprocess

from typing import Any
import boto3
from botocore.exceptions import ClientError

# --- Configuration ---
ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
PROD_BUCKET = os.environ["R2_PRODUCTION_BUCKET"]
STAGING_BUCKET = os.environ["R2_STAGING_BUCKET"]
INTERNAL_BUCKET = os.environ["R2_INTERNAL_BUCKET"]
ENDPOINT_URL = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"
MANIFEST_FILE = "manifest.json"
DATASETS_DOC_PATH = "docs/source/datasets.md"
BASE_DOWNLOAD_URL = "https://data.openenergyoutlook.org/"

# --- Boto3 S3 Client ---
client = boto3.client(
    "s3",
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=ACCESS_KEY_ID,
    aws_secret_access_key=SECRET_ACCESS_KEY,
)


def get_commit_details() -> dict[str, str]:
    """Gets the hash and subject of the latest commit affecting the manifest."""
    commit_hash = (
        subprocess.check_output(
            ["git", "log", "-1", "--pretty=%h", "--", MANIFEST_FILE]
        )
        .decode()
        .strip()
    )
    commit_subject = (
        subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s", "--", MANIFEST_FILE]
        )
        .decode()
        .strip()
    )
    return {"hash": commit_hash, "subject": commit_subject}


def finalize_dataset_docs(manifest_data: list[dict[str, Any]]) -> None:
    """
    Reads updated manifest and generates a Markdown table of datasets and their
    versions including download lionks, and writes it to docs
    """

    markdown_content = "# Available Datasets\n\n"
    markdown_content += (
        "This page lists all versioned datasets managed by the OEO Data Management tool,"
        "\n\n"
    )

    markdown_content += (
        "| Dataset Name | Version | Timestamp (UTC) | Description | Download |\n"
        "|--------------|---------|-----------------|-------------|----------|\n"
    )

    manifest_data.sort(key=lambda x: x["fileName"])

    for dataset in manifest_data:
        file_name = dataset["fileName"]
        history_sorted = sorted(
            dataset.get("history", []),
            key=lambda x: x.get("timestamp", ""),
            reverse=True,
        )
        for version_entry in history_sorted:
            version = version_entry.get("version", "N/A")
            # Remove 'Z' from timestamp for cleaner display in docs
            timestamp = version_entry.get("timestamp", "N/A").replace("Z", "")
            # Ensure description is single-line for table markdown
            description = version_entry.get("description", "").replace("\n", " ")
            r2_object_key = version_entry.get("r2_object_key")

            download_link = "N/A"
            if r2_object_key:
                # Construct the full download URL
                full_url = f"{BASE_DOWNLOAD_URL}{r2_object_key}"
                download_link = f"[Download]({full_url})"

            markdown_content += (
                f"| {file_name} "
                f"| {version} "
                f"| {timestamp} "
                f"| {description} "
                f"| {download_link} |\n"
            )

    with open(DATASETS_DOC_PATH, "w") as f:
        f.write(markdown_content)

    subprocess.run(["git", "add", DATASETS_DOC_PATH])
    subprocess.run(
        ["git", "commit", "-m", "Updating dataset docs to reflect manifest file"]
    )


def finalize_manifest(updated_data: list[dict[str, Any]], commit_message: str) -> None:
    """Writes the updated manifest, commits, and pushes the changes."""
    print("\nFinalizing manifest file...")
    with open(MANIFEST_FILE, "w") as f:
        json.dump(updated_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("Committing and pushing finalized manifest...")
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"])
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"]
    )
    subprocess.run(["git", "add", MANIFEST_FILE])
    subprocess.run(["git", "commit", "-m", commit_message])

    finalize_dataset_docs(updated_data)

    subprocess.run(["git", "push"])
    print("✅ Manifest finalized.")


def handle_deletions(manifest_data: list[dict[str, Any]]) -> bool:
    """
    Scans for and processes all pending deletions.
    Returns True if any deletions were processed.
    """
    print("--- Phase 1: Checking for pending deletions ---")
    datasets_to_keep = []
    objects_to_delete_from_r2 = []
    processed_deletion = False

    for dataset in manifest_data:
        # Get the bucket type for this dataset
        bucket_type = dataset.get("bucket", "production")
        target_bucket = INTERNAL_BUCKET if bucket_type == "internal" else PROD_BUCKET
        bucket_name = "internal" if bucket_type == "internal" else "production"

        if dataset.get("status") == "pending-deletion":
            processed_deletion = True
            print(
                f"Found dataset marked for full deletion: {dataset['fileName']} from {bucket_name} bucket"
            )
            for entry in dataset.get("history", []):
                if "r2_object_key" in entry:
                    objects_to_delete_from_r2.append(
                        {"Key": entry["r2_object_key"], "Bucket": target_bucket}
                    )
        else:
            versions_to_keep = []
            for entry in dataset.get("history", []):
                if entry.get("status") == "pending-deletion":
                    processed_deletion = True
                    print(
                        f"Found version marked for deletion: {dataset['fileName']} v{entry['version']} from {bucket_name} bucket"
                    )
                    if "r2_object_key" in entry:
                        objects_to_delete_from_r2.append(
                            {"Key": entry["r2_object_key"], "Bucket": target_bucket}
                        )
                else:
                    versions_to_keep.append(entry)
            dataset["history"] = versions_to_keep
            datasets_to_keep.append(dataset)

    if not processed_deletion:
        print("No pending deletions found.")
        return False

    if objects_to_delete_from_r2:
        # Group objects by bucket for deletion
        prod_objects = [
            obj for obj in objects_to_delete_from_r2 if obj["Bucket"] == PROD_BUCKET
        ]
        internal_objects = [
            obj for obj in objects_to_delete_from_r2 if obj["Bucket"] == INTERNAL_BUCKET
        ]

        if prod_objects:
            print(
                f"\nDeleting {len(prod_objects)} objects from production R2 bucket..."
            )
            for i in range(0, len(prod_objects), 1000):
                chunk: Any = prod_objects[i : i + 1000]
                objects_only = [{"Key": obj["Key"]} for obj in chunk]
                response = client.delete_objects(
                    Bucket=PROD_BUCKET, Delete={"Objects": objects_only, "Quiet": True}
                )
                if response.get("Errors"):
                    print("  ❌ ERROR during batch deletion:", response["Errors"])
                    exit(1)
            print("✅ Successfully deleted objects from production R2 bucket.")

        if internal_objects:
            print(
                f"\nDeleting {len(internal_objects)} objects from internal R2 bucket..."
            )
            for i in range(0, len(internal_objects), 1000):
                chunk: Any = internal_objects[i : i + 1000]
                objects_only = [{"Key": obj["Key"]} for obj in chunk]
                response = client.delete_objects(
                    Bucket=INTERNAL_BUCKET,
                    Delete={"Objects": objects_only, "Quiet": True},
                )
                if response.get("Errors"):
                    print("  ❌ ERROR during batch deletion:", response["Errors"])
                    exit(1)
            print("✅ Successfully deleted objects from internal R2 bucket.")

    finalize_manifest(datasets_to_keep, "ci: Finalize manifest after data deletion")
    return True


def handle_publications(manifest_data: list[dict[str, Any]]) -> bool:
    """
    Scans for and processes one pending publication or rollback.
    Returns True if a publication was processed.
    """
    print("\n--- Phase 2: Checking for pending publications ---")
    for dataset in manifest_data:
        # Get the bucket type for this dataset
        bucket_type = dataset.get("bucket", "production")
        target_bucket = INTERNAL_BUCKET if bucket_type == "internal" else PROD_BUCKET
        bucket_name = "internal" if bucket_type == "internal" else "production"

        for i, entry in enumerate(dataset["history"]):
            if entry.get("commit") == "pending-merge":
                commit_details = get_commit_details()
                entry["commit"] = commit_details["hash"]
                # Only overwrite description if it's the placeholder
                if entry.get("description") == "pending-merge":
                    entry["description"] = commit_details["subject"]

                if "staging_key" in entry and entry["staging_key"]:
                    staging_key = entry.pop("staging_key")
                    final_key = entry["r2_object_key"]
                    print(
                        f"Publishing: {dataset['fileName']} v{entry['version']} to {bucket_name} bucket"
                    )
                    print(f"  Description: {entry['description']}")
                    try:
                        copy_source: Any = {
                            "Bucket": STAGING_BUCKET,
                            "Key": staging_key,
                        }
                        client.copy_object(
                            CopySource=copy_source, Bucket=target_bucket, Key=final_key
                        )
                        print(
                            f"  ✅ Server-side copy to {bucket_name} bucket successful."
                        )
                        client.delete_object(Bucket=STAGING_BUCKET, Key=staging_key)
                        print("  ✅ Staging object deleted.")
                    except ClientError as e:
                        print(f"  ❌ ERROR: Could not process object. Reason: {e}")
                        exit(1)
                else:
                    print(
                        f"Finalizing rollback: {dataset['fileName']} v{entry['version']} in {bucket_name} bucket"
                    )
                    print(f"  Description: {entry['description']}")

                dataset["history"][i] = entry
                finalize_manifest(
                    manifest_data,
                    f"ci: Publish {dataset['fileName']} {entry['version']} to {bucket_name}",
                )
                return True  # Process only one publication per run

    print("No pending publications found.")
    return False


def main() -> None:
    """Main execution block."""
    print("Starting dataset publish/cleanup process...")
    with open(MANIFEST_FILE) as f:
        manifest_data = json.load(f)

    # Prioritize deletions. If any are found, the script will exit after handling them.
    deletions_processed = handle_deletions(manifest_data)
    if deletions_processed:
        print("\nDeletions were processed. Exiting to allow for a clean next run.")
        return

    # If no deletions, check for publications.
    handle_publications(manifest_data)
    print("\nProcess complete.")


if __name__ == "__main__":
    main()
