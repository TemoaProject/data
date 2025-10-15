# tests/test_manifest.py
import os
from pathlib import Path
import json
import pytest

from datamanager import manifest
from datamanager.__main__ import _validate_temoa_hash


def test_read_manifest(test_repo: Path) -> None:
    """Test reading a valid manifest file."""
    os.chdir(test_repo)  # Change directory to the test repo
    data = manifest.read_manifest()
    assert len(data) == 1
    assert data[0]["fileName"] == "core-dataset.sqlite"


def test_read_manifest_corrupted_json(test_repo: Path) -> None:
    """Test that read_manifest raises an error for a corrupted JSON file."""
    os.chdir(test_repo)
    # Overwrite the manifest with invalid JSON
    with open(manifest.MANIFEST_PATH, "w") as f:
        f.write("{ not_valid_json: ")

    # Assert that the expected error is raised
    with pytest.raises(json.JSONDecodeError):
        manifest.read_manifest()


def test_get_dataset(test_repo: Path) -> None:
    """Test finding an existing and non-existing dataset."""
    os.chdir(test_repo)
    dataset = manifest.get_dataset("core-dataset.sqlite")
    assert dataset is not None
    assert dataset["latestVersion"] == "v2"

    non_existent = manifest.get_dataset("non-existent.sqlite")
    assert non_existent is None


def test_update_latest_history_entry(test_repo: Path) -> None:
    """Test that the latest history entry can be correctly replaced."""
    os.chdir(test_repo)
    new_entry = {"version": "v2", "commit": "abcdef"}
    manifest.update_latest_history_entry("core-dataset.sqlite", new_entry)

    data = manifest.read_manifest()
    assert data[0]["history"][0]["version"] == "v2"
    assert data[0]["history"][0]["commit"] == "abcdef"
    assert data[0]["latestVersion"] == "v2"


def test_validate_temoa_hash() -> None:
    """Test the temoa hash validation function."""
    # Valid short hash
    assert _validate_temoa_hash("abc123")
    assert _validate_temoa_hash("ABCDEF")

    # Valid long hash
    assert _validate_temoa_hash("a" * 40)
    assert _validate_temoa_hash("1234567890abcdef" * 2)

    # Invalid formats
    assert not _validate_temoa_hash("gggggg")  # 'g' is not hex
    assert not _validate_temoa_hash("abc123g")  # contains 'g'
    assert not _validate_temoa_hash("abc12345-")  # contains dash
    assert _validate_temoa_hash("")  # Empty is allowed (optional)
    assert _validate_temoa_hash("   ")  # Whitespace only is allowed (optional)

    # Too short
    assert not _validate_temoa_hash("abc")  # Less than 4 chars

    # Too long
    assert not _validate_temoa_hash("a" * 41)  # More than 40 chars
