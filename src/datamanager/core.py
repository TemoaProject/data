# datamanager/core.py
import difflib
import hashlib
import io
import shutil
import sqlite3
import subprocess
from pathlib import Path, PurePath
import os
import uuid

from botocore.exceptions import ClientError

import boto3
from rich.progress import Progress
from rich.console import Console

from types_boto3_s3.client import S3Client

from typing import Any, TypedDict

from datamanager.config import settings


console = Console()


class PermissionDict(TypedDict):
    read: bool
    write: bool
    delete: bool


class VerificationResult(TypedDict):
    bucket_name: str
    exists: bool
    permissions: PermissionDict
    message: str


def get_r2_client() -> S3Client:
    """Initializes and returns a boto3 S3 client for R2."""
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        region_name="auto",
    )


def hash_file(file_path: Path) -> str:
    """Calculates and returns the SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def upload_to_r2(
    client: S3Client, file_path: Path, object_key: str, bucket: str | None = None
) -> None:
    """Uploads a file to R2 with a progress bar."""
    target_bucket = bucket or settings.bucket
    file_size = file_path.stat().st_size
    with Progress() as progress:
        task = progress.add_task(
            f"[cyan]Uploading {file_path.name}...", total=file_size
        )
        client.upload_file(
            str(file_path),
            target_bucket,
            object_key,
            Callback=lambda bytes_transferred: progress.update(
                task, advance=bytes_transferred
            ),
        )


def download_from_r2(
    client: S3Client, object_key: str, download_path: Path, bucket: str | None = None
) -> None:
    """Downloads a file from R2 with a progress bar."""
    target_bucket = bucket or settings.bucket
    try:
        file_size = client.head_object(Bucket=target_bucket, Key=object_key)[
            "ContentLength"
        ]
        with Progress() as progress:
            task = progress.add_task(
                f"[cyan]Downloading {download_path.name}...", total=file_size
            )
            client.download_file(
                target_bucket,
                object_key,
                str(download_path),
                Callback=lambda bytes_transferred: progress.update(
                    task, advance=bytes_transferred
                ),
            )
    except ClientError as e:
        # Handle cases where the object might not exist
        console.print(f"[bold red]Error downloading from R2: {e}[/]")
        raise


def pull_and_verify(
    object_key: str, expected_hash: str, output_path: Path, bucket: str | None = None
) -> bool:
    """
    Downloads a file from R2, verifies its hash, and cleans up on failure.

    Returns:
        True if download and verification succeed, False otherwise.
    """
    client = get_r2_client()
    try:
        download_from_r2(client, object_key, output_path, bucket)
    except Exception:
        return False  # Error message is printed inside download_from_r2

    console.print("Verifying file integrity...")
    downloaded_hash = hash_file(output_path)

    if downloaded_hash == expected_hash:
        return True
    else:
        console.print("[bold red]Integrity check FAILED![/]")
        console.print(f"  Expected SHA256: {expected_hash}")
        console.print(f"  Actual SHA256:   {downloaded_hash}")
        console.print(f"Deleting corrupted file: [yellow]{output_path}[/]")
        os.remove(output_path)
        return False


def generate_sql_diff(old_file: Path, new_file: Path) -> tuple[str, str]:
    """
    Return (full_diff, summary) between two SQLite files.

    - If `sqldiff` CLI is available, use that for both full and summary.
    - Otherwise fall back to sqlite3 + difflib, and synthesize a summary.
    """

    # Try sqldiff CLI first
    if shutil.which("sqldiff"):
        # Full diff
        proc_full = subprocess.run(
            ["sqldiff", str(old_file), str(new_file)],
            text=True,
            capture_output=True,
            check=True,
        )
        full_diff = proc_full.stdout

        # Summary (sqldiff --summary)
        proc_sum = subprocess.run(
            ["sqldiff", "--summary", str(old_file), str(new_file)],
            text=True,
            capture_output=True,
            check=True,
        )
        summary = proc_sum.stdout

        return full_diff, summary

    # Pure-Python fallback: dump both DBs and diff
    def _dump(db: Path) -> str:
        buf = io.StringIO()
        con = sqlite3.connect(db)
        for line in con.iterdump():
            buf.write(f"{line}\n")
        con.close()
        return buf.getvalue()

    old_sql = _dump(old_file)
    new_sql = _dump(new_file)

    diff_iter = difflib.unified_diff(
        old_sql.splitlines(keepends=True),
        new_sql.splitlines(keepends=True),
        fromfile=str(PurePath(old_file).name),
        tofile=str(PurePath(new_file).name),
    )
    full_diff = "".join(diff_iter)

    # Synthesize a summary: count ADDs and DELs
    adds = sum(
        1
        for ln in full_diff.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    )
    dels = sum(
        1
        for ln in full_diff.splitlines()
        if ln.startswith("-") and not ln.startswith("---")
    )
    summary = f"# summary: {adds} additions, {dels} deletions\n"

    return full_diff, summary


def delete_from_r2(
    client: S3Client, object_key: str, bucket: str | None = None
) -> None:
    """Deletes an object from the R2 bucket."""
    target_bucket = bucket or settings.bucket
    console.print(f"Attempting to delete [yellow]{object_key}[/] from R2...")
    try:
        client.delete_object(Bucket=target_bucket, Key=object_key)
        console.print("✅ Rollback deletion successful.")
    except Exception as e:
        # This is a best-effort cleanup. We notify the user if it fails.
        console.print(
            f"[bold red]Warning:[/] Failed to delete R2 object during rollback: {e}"
        )
        console.print("You may need to manually delete the object from your R2 bucket.")


def _check_bucket_permissions(client: Any, bucket_name: str) -> VerificationResult:
    """Performs granular permission checks on a single bucket."""
    results: VerificationResult = {
        "bucket_name": bucket_name,
        "exists": False,
        "permissions": {"read": False, "write": False, "delete": False},
        "message": "",
    }

    # 1. Check for bucket existence and basic access
    try:
        client.head_bucket(Bucket=bucket_name)
        results["exists"] = True
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code")
        if error_code == "404":
            results["message"] = "Bucket not found."
        elif error_code == "403":
            results["message"] = "Access Denied. Cannot view bucket."
        else:
            results["message"] = f"Connection error: {e}"
        return results

    # 2. Check for Read permission (by listing objects)
    try:
        client.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
        results["permissions"]["read"] = True
    except ClientError:
        # Fails if ListBucket permission is denied
        pass

    # 3. Check for Write and Delete permissions (atomically)
    test_key = f"datamanager-verify-test-{uuid.uuid4()}.tmp"
    try:
        # Test Write
        client.put_object(Bucket=bucket_name, Key=test_key, Body=b"verify")
        results["permissions"]["write"] = True
        # Test Delete
        client.delete_object(Bucket=bucket_name, Key=test_key)
        results["permissions"]["delete"] = True
    except ClientError:
        # Fails if PutObject or DeleteObject is denied
        pass
    finally:
        # Ensure the test object is always cleaned up if it was created
        try:
            client.delete_object(Bucket=bucket_name, Key=test_key)
        except ClientError:
            pass

    # 4. Construct final message
    if all(results["permissions"].values()):
        results["message"] = "Full access verified."
    else:
        perms = ", ".join([k for k, v in results["permissions"].items() if v])
        results["message"] = (
            f"Partial access: [{perms}]" if perms else "No object permissions."
        )

    return results


def verify_r2_access() -> list[VerificationResult]:
    """
    Verifies granular permissions for production, staging, and internal buckets.

    Returns:
        A list of result dictionaries, one for each bucket check.
    """
    results = []
    try:
        client = get_r2_client()
        # Check Production Bucket
        results.append(_check_bucket_permissions(client, settings.bucket))
        # Check Staging Bucket
        results.append(_check_bucket_permissions(client, settings.staging_bucket))
        # Check Internal Bucket
        results.append(_check_bucket_permissions(client, settings.internal_bucket))
    except Exception as e:
        # Catches errors during client creation (e.g., bad endpoint)
        connection_error: VerificationResult = {
            "bucket_name": "Connection",
            "exists": False,
            "permissions": {"read": False, "write": False, "delete": False},
            "message": f"Failed to create R2 client: {e}",
        }
        results.append(connection_error)
    return results


def upload_to_staging(client: S3Client, file_path: Path, object_key: str) -> None:
    """Uploads a file to the STAGING R2 bucket with a progress bar."""
    file_size = file_path.stat().st_size
    with Progress() as progress:
        task = progress.add_task(
            f"[yellow]Uploading to staging: {file_path.name}...", total=file_size
        )
        client.upload_file(
            str(file_path),
            settings.staging_bucket,
            object_key,
            Callback=lambda bytes_transferred: progress.update(
                task, advance=bytes_transferred
            ),
        )


def upload_to_internal(client: S3Client, file_path: Path, object_key: str) -> None:
    """Uploads a file to the INTERNAL R2 bucket with a progress bar."""
    file_size = file_path.stat().st_size
    with Progress() as progress:
        task = progress.add_task(
            f"[blue]Uploading to internal: {file_path.name}...", total=file_size
        )
        client.upload_file(
            str(file_path),
            settings.internal_bucket,
            object_key,
            Callback=lambda bytes_transferred: progress.update(
                task, advance=bytes_transferred
            ),
        )
