# tests/test_core.py
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock
from pytest_mock import MockerFixture
import pytest

from botocore.exceptions import ClientError
from typing import Dict, Any


from datamanager import core
from datamanager.config import settings


from typing import TypedDict, cast


class _ErrorBody(TypedDict):
    Code: str
    Message: str


class _ClientErrResp(TypedDict):
    Error: _ErrorBody


def make_client_error(code: str, message: str, operation_name: str) -> ClientError:
    """
    Build a ClientError with a response shape that satisfies mypy
    without relying on private botocore types.
    """
    resp: _ClientErrResp = {"Error": {"Code": code, "Message": message}}
    # ClientError expects a dict with "Error" key but the stubs require
    # a specific TypedDict. We cast once here to keep call sites clean.
    return ClientError(cast(Any, resp), operation_name)


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

    mock_client.put_object.side_effect = make_client_error(
        "403", "Access Denied", "PutObject"
    )

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
    mock_client.head_bucket.side_effect = make_client_error(
        "404", "Not Found", "HeadBucket"
    )

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
    mock_client.head_object.side_effect = make_client_error(
        "404", "Not Found", "HeadObject"
    )

    output_file = tmp_path / "test.sqlite"

    # Assert that the function raises the expected exception,
    # which our CLI logic is designed to catch.
    with pytest.raises(ClientError):
        core.download_from_r2(mock_client, "non-existent-key", output_file)


def test_resolve_bucket_alias_production(mocker: MockerFixture) -> None:
    """Test that resolve_bucket_alias correctly resolves 'production' alias."""
    # Mock the _resolve_bucket function to avoid actual settings dependency
    mocker.patch(
        "datamanager.core._resolve_bucket", return_value="test-production-bucket"
    )

    result = core.resolve_bucket_alias("production")
    assert result == "test-production-bucket"


def test_resolve_bucket_alias_internal(mocker: MockerFixture) -> None:
    """Test that resolve_bucket_alias correctly resolves 'internal' alias."""
    mocker.patch(
        "datamanager.core._resolve_bucket", return_value="test-internal-bucket"
    )

    result = core.resolve_bucket_alias("internal")
    assert result == "test-internal-bucket"


def test_resolve_bucket_none(mocker: MockerFixture) -> None:
    """Test _resolve_bucket with None bucket parameter."""
    # Mock the entire settings object since it's a frozen dataclass
    mock_settings = mocker.MagicMock()
    mock_settings.bucket = "test-production-bucket"
    mock_settings.internal_bucket = "test-internal-bucket"
    mocker.patch("datamanager.core.settings", mock_settings)

    result = core._resolve_bucket(None)
    assert result == "test-production-bucket"


def test_resolve_bucket_production(mocker: MockerFixture) -> None:
    """Test _resolve_bucket with 'production' bucket parameter."""
    mock_settings = mocker.MagicMock()
    mock_settings.bucket = "test-production-bucket"
    mock_settings.internal_bucket = "test-internal-bucket"
    mocker.patch("datamanager.core.settings", mock_settings)

    result = core._resolve_bucket("production")
    assert result == "test-production-bucket"


def test_resolve_bucket_internal(mocker: MockerFixture) -> None:
    """Test _resolve_bucket with 'internal' bucket parameter."""
    mock_settings = mocker.MagicMock()
    mock_settings.bucket = "test-production-bucket"
    mock_settings.internal_bucket = "test-internal-bucket"
    mocker.patch("datamanager.core.settings", mock_settings)

    result = core._resolve_bucket("internal")
    assert result == "test-internal-bucket"


def test_resolve_bucket_custom(mocker: MockerFixture) -> None:
    """Test _resolve_bucket with custom bucket name."""
    result = core._resolve_bucket("custom-bucket-name")
    assert result == "custom-bucket-name"


def test_upload_to_staging(mocker: MockerFixture, tmp_path: Path) -> None:
    """Test upload_to_staging function."""
    mock_client = mocker.MagicMock()
    test_file = tmp_path / "test.sqlite"
    test_file.touch()

    # Mock the entire settings object since it's a frozen dataclass
    mock_settings = mocker.MagicMock()
    mock_settings.staging_bucket = "test-staging-bucket"
    mocker.patch("datamanager.core.settings", mock_settings)

    core.upload_to_staging(mock_client, test_file, "test-key")

    # Verify upload_file was called with correct staging bucket
    mock_client.upload_file.assert_called_once_with(
        str(test_file),
        "test-staging-bucket",
        "test-key",
        Callback=mocker.ANY,  # We don't need to test the exact callback
    )


def test_upload_to_internal(mocker: MockerFixture, tmp_path: Path) -> None:
    """Test upload_to_internal function."""
    mock_client = mocker.MagicMock()
    test_file = tmp_path / "test.sqlite"
    test_file.touch()

    # Mock the upload_to_r2 function
    mock_upload_r2 = mocker.patch("datamanager.core.upload_to_r2")

    core.upload_to_internal(mock_client, test_file, "test-key")

    # Verify upload_to_r2 was called with internal bucket
    mock_upload_r2.assert_called_once_with(
        mock_client, test_file, "test-key", bucket="internal"
    )


def test_pull_and_verify_download_error(mocker: MockerFixture, tmp_path: Path) -> None:
    """Test pull_and_verify when download fails."""
    mock_client = mocker.MagicMock()
    mocker.patch("datamanager.core.get_r2_client", return_value=mock_client)
    mock_download = mocker.patch("datamanager.core.download_from_r2")
    mock_remove = mocker.patch("os.remove")

    # Simulate download failure - note: os.remove should NOT be called
    # when download fails, only when download succeeds but hash verification fails
    error_response: Any = {"Error": {"Code": "404", "Message": "Not Found"}}
    mock_download.side_effect = ClientError(
        error_response,
        "Download",
    )

    output_file = tmp_path / "test.sqlite"
    output_file.touch()

    result = core.pull_and_verify("test-key", "expected-hash", output_file)

    assert result is False
    mock_download.assert_called_once()
    # os.remove should NOT be called when download fails
    mock_remove.assert_not_called()


def test_pull_and_verify_success(mocker: MockerFixture, tmp_path: Path) -> None:
    """Test pull_and_verify when everything succeeds."""
    mock_client = mocker.MagicMock()
    mocker.patch("datamanager.core.get_r2_client", return_value=mock_client)
    mock_download = mocker.patch("datamanager.core.download_from_r2")
    mock_remove = mocker.patch("os.remove")

    output_file = tmp_path / "test.sqlite"
    output_file.touch()

    # Mock hash_file to return correct hash
    mocker.patch("datamanager.core.hash_file", return_value="expected-hash")

    result = core.pull_and_verify("test-key", "expected-hash", output_file)

    assert result is True
    mock_download.assert_called_once()
    mock_remove.assert_not_called()


def test_delete_from_r2_exception(mocker: MockerFixture) -> None:
    """Test delete_from_r2 when delete operation fails."""
    mock_client = mocker.MagicMock()
    mock_client.delete_object.side_effect = Exception("Delete failed")

    # Should not raise exception, just print warning
    core.delete_from_r2(mock_client, "test-key")


def test_verify_r2_access_connection_error(mocker: MockerFixture) -> None:
    """Test verify_r2_access when client creation fails."""
    mocker.patch(
        "datamanager.core.get_r2_client", side_effect=Exception("Connection failed")
    )

    results = core.verify_r2_access()

    assert len(results) == 1
    assert results[0]["bucket_name"] == "Connection"
    assert results[0]["exists"] is False
    assert "Failed to create R2 client" in results[0]["message"]


def test_check_bucket_permissions_access_denied(mocker: MockerFixture) -> None:
    """Test _check_bucket_permissions when bucket access is denied."""
    mock_client = mocker.MagicMock()
    error_response: Dict[str, Any] = {
        "Error": {"Code": "403", "Message": "Access Denied"}
    }
    mock_client.head_bucket.side_effect = ClientError(error_response, "HeadBucket")  # type: ignore

    result = core._check_bucket_permissions(mock_client, "test-bucket")

    assert result["bucket_name"] == "test-bucket"
    assert result["exists"] is False
    assert result["permissions"] == {"read": False, "write": False, "delete": False}
    assert "Access Denied" in result["message"]


def test_check_bucket_permissions_connection_error(mocker: MockerFixture) -> None:
    """Test _check_bucket_permissions when there's a connection error."""
    mock_client = mocker.MagicMock()
    error_response: Dict[str, Any] = {
        "Error": {"Code": "500", "Message": "Internal Server Error"}
    }
    mock_client.head_bucket.side_effect = ClientError(error_response, "HeadBucket")  # type: ignore

    result = core._check_bucket_permissions(mock_client, "test-bucket")

    assert result["bucket_name"] == "test-bucket"
    assert result["exists"] is False
    assert "Connection error" in result["message"]


def test_check_bucket_permissions_read_only(mocker: MockerFixture) -> None:
    """Test _check_bucket_permissions when only read permissions are available."""
    mock_client = mocker.MagicMock()
    mock_client.head_bucket.return_value = True
    mock_client.list_objects_v2.return_value = True
    # Simulate write/delete failing
    error_response: Dict[str, Any] = {
        "Error": {"Code": "403", "Message": "Access Denied"}
    }
    mock_client.put_object.side_effect = ClientError(error_response, "PutObject")  # type: ignore

    result = core._check_bucket_permissions(mock_client, "test-bucket")

    assert result["bucket_name"] == "test-bucket"
    assert result["exists"] is True
    assert result["permissions"]["read"] is True
    assert result["permissions"]["write"] is False
    assert result["permissions"]["delete"] is False
    assert "Partial access: [read]" in result["message"]


def test_check_bucket_permissions_no_permissions(mocker: MockerFixture) -> None:
    """Test _check_bucket_permissions when no permissions are available."""
    mock_client = mocker.MagicMock()
    mock_client.head_bucket.return_value = True
    # Simulate all operations failing
    error_response: Any = {"Error": {"Code": "403", "Message": "Access Denied"}}
    mock_client.list_objects_v2.side_effect = ClientError(
        error_response, "ListObjectsV2"
    )
    mock_client.put_object.side_effect = ClientError(error_response, "PutObject")

    result = core._check_bucket_permissions(mock_client, "test-bucket")

    assert result["bucket_name"] == "test-bucket"
    assert result["exists"] is True
    assert result["permissions"] == {"read": False, "write": False, "delete": False}
    assert "No object permissions" in result["message"]
