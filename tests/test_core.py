# tests/test_core.py
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from pytest_mock import MockerFixture
import pytest

from botocore.exceptions import ClientError

from datamanager import core
from datamanager.config import settings


def test_hash_file(tmp_path: Path) -> None:
    """Test SHA256 hash calculation."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")
    # Known SHA256 hash for "hello world"
    expected_hash = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert core.hash_file(test_file) == expected_hash


def test_generate_sql_diff(tmp_path: Path) -> None:
    """Test creating a diff between two sqlite files."""
    old_db_path = tmp_path / "old.sqlite"
    new_db_path = tmp_path / "new.sqlite"

    # Create old DB
    con = sqlite3.connect(old_db_path)
    con.execute("CREATE TABLE users (id INT, name TEXT)")
    con.execute("INSERT INTO users VALUES (1, 'Alice')")
    con.commit()
    con.close()

    # Create new DB with a change
    con = sqlite3.connect(new_db_path)
    con.execute("CREATE TABLE users (id INT, name TEXT)")
    con.execute("INSERT INTO users VALUES (2, 'Bob')")  # Changed data
    con.commit()
    con.close()

    full_diff, summary = core.generate_sql_diff(old_db_path, new_db_path)

    # We should see evidence of the change in full_diff:
    #   - either an UPDATE statement from sqldiff,
    #   - or a "+INSERT ..." / "-INSERT ..." style from the fallback.
    assert "Bob" in full_diff, f"full_diff did not mention Bob:\n{full_diff}"
    assert (
        "UPDATE users SET" in full_diff
        or "+INSERT INTO" in full_diff
        or "-INSERT INTO" in full_diff
    ), f"unexpected diff format:\n{full_diff}"

    # And summary should be a non‐empty string
    assert isinstance(summary, str)
    assert summary.strip(), "Summary should not be empty"


def test_r2_interactions(mocker: MockerFixture, tmp_path: Path) -> None:
    """Test that our code calls the boto3 client correctly."""
    mock_client = MagicMock()
    mocker.patch("datamanager.core.get_r2_client", return_value=mock_client)

    # Create a dummy file so that `file_path.stat()` doesn't fail.
    dummy_file = tmp_path / "dummy.txt"
    dummy_file.touch()

    # Test upload
    core.upload_to_r2(mock_client, dummy_file, "my/object/key")
    mock_client.upload_file.assert_called_once()

    # Test delete
    core.delete_from_r2(mock_client, "my/object/key")
    mock_client.delete_object.assert_called_once_with(
        Bucket=settings.bucket, Key="my/object/key"
    )


def test_verify_r2_access_full_permissions(mocker: MockerFixture) -> None:
    """Test verification when credentials have full permissions."""
    mock_client = mocker.patch("datamanager.core.get_r2_client").return_value
    # All boto3 calls succeed
    mock_client.head_bucket.return_value = True
    mock_client.list_objects_v2.return_value = True
    mock_client.put_object.return_value = True
    mock_client.delete_object.return_value = True

    results = core.verify_r2_access()

    # Should return results for production, staging, and internal buckets
    assert len(results) == 3
    prod_result = results[0]
    staging_result = results[1]
    internal_result = results[2]
    assert prod_result["exists"] is True
    assert staging_result["exists"] is True
    assert internal_result["exists"] is True
    assert all(prod_result["permissions"].values())
    assert all(staging_result["permissions"].values())
    assert all(internal_result["permissions"].values())
    assert "Full access" in prod_result["message"]
    assert "Full access" in staging_result["message"]
    assert "Full access" in internal_result["message"]


def test_verify_r2_access_read_only(mocker: MockerFixture) -> None:
    """Test verification when credentials only have read permissions."""
    mock_client = mocker.patch("datamanager.core.get_r2_client").return_value
    mock_client.head_bucket.return_value = True
    mock_client.list_objects_v2.return_value = True
    # Simulate write/delete failing with a generic ClientError
    error_response: Any = {"Error": {"Code": "403", "Message": "Access Denied"}}
    mock_client.put_object.side_effect = ClientError(error_response, "PutObject")

    results = core.verify_r2_access()
    prod_result = results[0]

    assert prod_result["exists"] is True
    assert prod_result["permissions"]["read"] is True
    assert prod_result["permissions"]["write"] is False
    assert prod_result["permissions"]["delete"] is False
    assert "Partial access: [read]" in prod_result["message"]


def test_verify_r2_access_bucket_not_found(mocker: MockerFixture) -> None:
    """Test verification when a bucket does not exist."""
    mock_client = mocker.patch("datamanager.core.get_r2_client").return_value
    error_response: Any = {"Error": {"Code": "404", "Message": "Not Found"}}
    mock_client.head_bucket.side_effect = ClientError(error_response, "HeadBucket")

    results = core.verify_r2_access()
    prod_result = results[0]

    assert prod_result["exists"] is False
    assert not any(prod_result["permissions"].values())
    assert "Bucket not found" in prod_result["message"]


def test_pull_and_verify_hash_mismatch(mocker: MockerFixture, tmp_path: Path) -> None:
    """Test that a corrupted download is deleted after a failed integrity check."""
    # Mock the download to succeed
    mocker.patch("datamanager.core.get_r2_client")
    mock_download = mocker.patch("datamanager.core.download_from_r2")
    # Mock os.remove to verify it gets called
    mock_remove = mocker.patch("os.remove")

    output_file = tmp_path / "corrupted.sqlite"
    output_file.touch()  # Create a dummy file to be "removed"

    # Run the function with mismatching hashes
    success = core.pull_and_verify(
        object_key="some/key",
        expected_hash="hash_A",
        output_path=output_file,
    )

    assert success is False
    mock_download.assert_called_once()
    # Verify that the cleanup logic was triggered
    mock_remove.assert_called_once_with(output_file)


def test_download_from_r2_failure(mocker: MockerFixture, tmp_path: Path) -> None:
    """Test that download_from_r2 handles a ClientError gracefully."""
    mock_client = mocker.MagicMock()
    mocker.patch("datamanager.core.get_r2_client", return_value=mock_client)

    # Simulate boto3 raising an error during download
    error_response: Any = {"Error": {"Code": "404", "Message": "Not Found"}}
    mock_client.head_object.side_effect = ClientError(error_response, "HeadObject")

    output_file = tmp_path / "test.sqlite"

    # Assert that the function raises the expected exception,
    # which our CLI logic is designed to catch.
    with pytest.raises(ClientError):
        core.download_from_r2(mock_client, "non-existent-key", output_file)
