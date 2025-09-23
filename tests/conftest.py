# tests/conftest.py
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Generator

import pytest

# Import the hashing function from the application itself
from datamanager.core import hash_file


@pytest.fixture
def test_repo(tmp_path: Path) -> Generator[Path, Any, None]:
    """
    Creates a temporary directory initialized as a Git repository,
    with a pre-populated and committed manifest.json and dummy sqlite files.
    The manifest has two versions to allow for rollback testing.
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    subprocess.run(["git", "init"], cwd=repo_path, check=True)

    # Create a valid v1 database file
    v1_db_path = repo_path / "old_data.sqlite"
    con = sqlite3.connect(v1_db_path)
    con.execute("CREATE TABLE data (id INT)")
    con.commit()
    con.close()
    v1_hash = hash_file(v1_db_path)

    # Create a different v2 database file
    v2_db_path = repo_path / "new_data.sqlite"
    con = sqlite3.connect(v2_db_path)
    con.execute("CREATE TABLE data (id INT, value TEXT)")
    con.commit()
    con.close()
    v2_hash = hash_file(v2_db_path)

    manifest_data = [
        {
            "fileName": "core-dataset.sqlite",
            "bucket": "production",
            "latestVersion": "v2",
            "history": [
                {
                    "version": "v2",
                    "timestamp": "2025-07-01T12:00:00Z",
                    "sha256": v2_hash,
                    "r2_object_key": f"core-dataset/v2-{v2_hash}.sqlite",
                    "diffFromPrevious": None,
                    "commit": "abcdef1",
                },
                {
                    "version": "v1",
                    "timestamp": "2025-06-30T10:00:00Z",
                    "sha256": v1_hash,
                    "r2_object_key": f"core-dataset/v1-{v1_hash}.sqlite",
                    "diffFromPrevious": None,
                    "commit": "f4b8e7a",
                },
            ],
        }
    ]

    manifest_path = repo_path / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump(manifest_data, f, indent=2)

    subprocess.run(["git", "add", "manifest.json"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit with manifest"],
        cwd=repo_path,
        check=True,
    )

    yield repo_path

    shutil.rmtree(repo_path)
