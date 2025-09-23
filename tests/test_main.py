import os
from pathlib import Path

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from datamanager import __main__ as main_app
from datamanager.__main__ import app
from datamanager import manifest
from datamanager.config import settings

runner = CliRunner()


def test_prepare_for_create_success(test_repo: Path, mocker: MockerFixture) -> None:
    """Test the 'prepare' command for a brand-new dataset (no diff)."""
    os.chdir(test_repo)
    new_file = test_repo / "new_dataset.sqlite"
    new_file.touch()

    mocker.patch("datamanager.core.get_r2_client")
    mock_upload = mocker.patch("datamanager.core.upload_to_staging")

    result = runner.invoke(app, ["prepare", "new-dataset.sqlite", str(new_file)])

    assert result.exit_code == 0, result.stdout
    assert "Preparation complete!" in result.stdout
    mock_upload.assert_called_once()

    # Verify the manifest entry was created correctly with no diff
    dataset = manifest.get_dataset("new-dataset.sqlite")
    assert dataset is not None
    assert dataset["bucket"] == "production"  # Should default to production
    assert dataset["history"][0]["diffFromPrevious"] is None
    assert dataset["history"][0]["description"] == "pending-merge"


def test_prepare_for_update_with_small_diff(
    test_repo: Path, mocker: MockerFixture
) -> None:
    """Test an update that generates and stores both summary+full diff."""
    os.chdir(test_repo)
    mock_r2_client = mocker.patch("datamanager.core.get_r2_client").return_value
    mock_r2_client.head_object.return_value = {"ContentLength": 1024}
    mocker.patch("datamanager.core.upload_to_staging")
    mocker.patch("datamanager.core.download_from_r2")

    # Prepare a fake summary and full diff
    fake_summary = "# summary: 1 add, 1 del\n"
    fake_full = "--- a\n+++ b\n-foo\n+bar\n"
    mocker.patch(
        "datamanager.core.generate_sql_diff",
        return_value=(fake_full, fake_summary),
    )

    v3_file = test_repo / "v3_data.sqlite"
    v3_file.write_text("this is v3")

    result = runner.invoke(app, ["prepare", "core-dataset.sqlite", str(v3_file)])
    assert result.exit_code == 0, result.stdout
    assert "Full diff stored in Git" in result.stdout

    expected = Path("diffs/core-dataset.sqlite/diff-v2-to-v3.diff")
    assert expected.exists()

    # Now verify the file contains summary first, then full diff
    content = expected.read_text()
    assert content == fake_summary + fake_full

    ds = manifest.get_dataset("core-dataset.sqlite")
    assert ds is not None
    assert ds["history"][0]["diffFromPrevious"] == str(expected)


def test_prepare_for_update_with_large_diff(
    test_repo: Path, mocker: MockerFixture
) -> None:
    """Test an update where the full diff is too large and only summary is stored."""
    os.chdir(test_repo)
    mock_r2_client = mocker.patch("datamanager.core.get_r2_client").return_value
    mock_r2_client.head_object.return_value = {"ContentLength": 1024}
    mocker.patch("datamanager.core.upload_to_staging")
    mocker.patch("datamanager.core.download_from_r2")
    # Make the full diff larger than the default limit, but still provide a summary
    large_full = "line\n" * (settings.max_diff_lines + 1)
    small_summary = "# summary: huge diff, see details in PR\n"
    mocker.patch(
        "datamanager.core.generate_sql_diff",
        return_value=(large_full, small_summary),
    )

    v3_file = test_repo / "v3_data.sqlite"
    v3_file.write_text("this is v3")

    result = runner.invoke(app, ["prepare", "core-dataset.sqlite", str(v3_file)])
    assert result.exit_code == 0, result.stdout

    # We should see that the full diff was too large but a summary was stored
    assert "too large" in result.stdout.lower()

    expected_diff_path = Path("diffs/core-dataset.sqlite/diff-v2-to-v3.diff")
    assert expected_diff_path.exists(), "Summary file should have been written"

    # The manifest should record the path
    dataset = manifest.get_dataset("core-dataset.sqlite")
    assert dataset is not None
    assert dataset["history"][0]["diffFromPrevious"] == str(expected_diff_path)

    # And the contents of that file should be exactly our summary
    content = expected_diff_path.read_text()
    assert content == small_summary


def test_prepare_no_changes(test_repo: Path, mocker: MockerFixture) -> None:
    """Test 'prepare' when the file hash is identical to the latest version."""
    os.chdir(test_repo)
    mock_upload = mocker.patch("datamanager.core.upload_to_staging")

    result = runner.invoke(app, ["prepare", "core-dataset.sqlite", "new_data.sqlite"])

    assert result.exit_code == 0, result.stdout
    assert "No changes detected" in result.stdout
    mock_upload.assert_not_called()


def test_pull_command_latest_success(test_repo: Path, mocker: MockerFixture) -> None:
    """Test the 'pull' command for the latest version."""
    os.chdir(test_repo)
    mock_pull = mocker.patch("datamanager.core.pull_and_verify", return_value=True)

    result = runner.invoke(app, ["pull", "core-dataset.sqlite"])

    assert result.exit_code == 0
    assert "✅ Success!" in result.stdout
    mock_pull.assert_called_once()
    call_args = mock_pull.call_args[0]
    # The key in the fixture has a different hash now, check for the version
    assert "core-dataset/v2-" in call_args[0]
    assert call_args[2] == Path("core-dataset.sqlite")


def test_pull_command_version_not_found(test_repo: Path, mocker: MockerFixture) -> None:
    """Test pulling a version that does not exist."""
    os.chdir(test_repo)
    result = runner.invoke(app, ["pull", "core-dataset.sqlite", "--version", "v99"])
    assert result.exit_code == 1
    assert "Could not find version 'v99'" in result.stdout


def test_verify_command_success(mocker: MockerFixture) -> None:
    """Test the 'verify' command with successful credentials."""
    # Mock the return value to simulate a successful check for both buckets
    mock_return_value = [
        {
            "bucket_name": "production",
            "exists": True,
            "message": "Full access",
            "permissions": {"read": True, "write": True, "delete": True},
        },
        {
            "bucket_name": "staging",
            "exists": True,
            "message": "Full access",
            "permissions": {"read": True, "write": True, "delete": True},
        },
    ]
    mocker.patch("datamanager.core.verify_r2_access", return_value=mock_return_value)

    result = runner.invoke(app, ["verify"])

    assert result.exit_code == 0, result.stdout
    assert "All checks passed" in result.stdout
    assert "production" in result.stdout
    assert "staging" in result.stdout


def test_verify_command_failure(mocker: MockerFixture) -> None:
    """Test the 'verify' command when a bucket is not found."""
    # Mock the return value to simulate a failure
    mock_return_value = [
        {
            "bucket_name": "production",
            "exists": False,
            "message": "Bucket not found.",
            "permissions": {"read": False, "write": False, "delete": False},
        },
        {
            "bucket_name": "staging",
            "exists": True,
            "message": "Full access",
            "permissions": {"read": True, "write": True, "delete": True},
        },
    ]
    mocker.patch("datamanager.core.verify_r2_access", return_value=mock_return_value)

    result = runner.invoke(app, ["verify"])

    assert result.exit_code == 1, result.stdout
    assert "One or more critical checks failed" in result.stdout
    assert "Bucket not found" in result.stdout


def test_prepare_interactive_success(test_repo: Path, mocker: MockerFixture) -> None:
    """Test the full interactive 'prepare' flow."""
    os.chdir(test_repo)
    new_file = test_repo / "new_dataset.sqlite"
    new_file.touch()

    mock_run_logic = mocker.patch("datamanager.__main__._run_prepare_logic")

    mocker.patch(
        "questionary.path",
        return_value=mocker.Mock(ask=mocker.Mock(return_value=str(new_file))),
    )
    mocker.patch(
        "questionary.text",
        return_value=mocker.Mock(ask=mocker.Mock(return_value="new-dataset.sqlite")),
    )
    mocker.patch(
        "questionary.confirm",
        return_value=mocker.Mock(ask=mocker.Mock(return_value=True)),
    )

    # Create a simple mock context instead of a real one.
    # We only need it to have an `obj` attribute.
    mock_ctx = mocker.MagicMock()
    mock_ctx.obj = {}  # Simulate an empty context object

    # Call the private method directly from the imported module
    main_app._prepare_interactive(mock_ctx)

    mock_run_logic.assert_called_once_with(
        mock_ctx, name="new-dataset.sqlite", file=new_file, bucket="production"
    )


def test_prepare_interactive_cancel(test_repo: Path, mocker: MockerFixture) -> None:
    """Test cancelling the interactive 'prepare' flow."""
    os.chdir(test_repo)
    mock_run_logic = mocker.patch("datamanager.__main__._run_prepare_logic")

    mocker.patch(
        "questionary.path",
        return_value=mocker.Mock(ask=mocker.Mock(return_value=None)),
    )

    mock_ctx = mocker.MagicMock()
    mock_ctx.obj = {}

    main_app._prepare_interactive(mock_ctx)

    mock_run_logic.assert_not_called()


def test_pull_command_with_output_dir(test_repo: Path, mocker: MockerFixture) -> None:
    """Test the 'pull' command with the -o flag pointing to a directory."""
    os.chdir(test_repo)
    mock_pull = mocker.patch("datamanager.core.pull_and_verify", return_value=True)

    output_dir = test_repo / "downloads"
    output_dir.mkdir()

    result = runner.invoke(app, ["pull", "core-dataset.sqlite", "-o", str(output_dir)])

    assert result.exit_code == 0
    mock_pull.assert_called_once()
    # Verify it constructed the correct final path inside the directory
    final_path = mock_pull.call_args[0][2]
    assert final_path == output_dir / "core-dataset.sqlite"


def test_pull_interactive_success(test_repo: Path, mocker: MockerFixture) -> None:
    """Test the full interactive 'pull' flow."""
    os.chdir(test_repo)
    mock_run_logic = mocker.patch("datamanager.__main__._run_pull_logic")

    # Simulate a user answering all prompts successfully
    mocker.patch(
        "questionary.select",
        side_effect=[
            mocker.Mock(ask=mocker.Mock(return_value="core-dataset.sqlite")),
            mocker.Mock(ask=mocker.Mock(return_value="v1 (commit: f4b8e7a, ...)")),
        ],
    )
    mocker.patch(
        "questionary.path",
        return_value=mocker.Mock(ask=mocker.Mock(return_value="./pulled-file.sqlite")),
    )

    mock_ctx = mocker.MagicMock()
    mock_ctx.obj = {}
    main_app._pull_interactive(mock_ctx)

    # Verify the core logic was called with the user's answers
    mock_run_logic.assert_called_once_with(
        name="core-dataset.sqlite",
        version="v1",
        output=Path("./pulled-file.sqlite"),
        bucket="production",
    )


def test_pull_interactive_no_datasets(test_repo: Path, mocker: MockerFixture) -> None:
    """Test the interactive pull flow when the manifest is empty."""
    os.chdir(test_repo)
    # Mock the manifest read to return an empty list
    mocker.patch("datamanager.manifest.read_manifest", return_value=[])
    mock_select = mocker.patch("questionary.select")

    mock_ctx = mocker.MagicMock()
    mock_ctx.obj = {}
    main_app._pull_interactive(mock_ctx)

    # Verify that no prompts were shown because there were no datasets
    mock_select.assert_not_called()


def test_rollback_success(test_repo: Path, mocker: MockerFixture) -> None:
    """Test a successful rollback to a previous version."""
    os.chdir(test_repo)
    # Mock the confirmation prompt to return 'yes'
    mocker.patch("datamanager.__main__._ask_confirm", return_value=True)

    # Act: Roll back from v2 (latest) to v1
    result = runner.invoke(
        app, ["rollback", "core-dataset.sqlite", "--to-version", "v1"]
    )

    assert result.exit_code == 0, result.stdout
    assert "✅ Rollback prepared!" in result.stdout

    # Assert: Check the new state of the manifest
    dataset = manifest.get_dataset("core-dataset.sqlite")
    assert dataset is not None
    assert dataset["latestVersion"] == "v3"
    assert len(dataset["history"]) == 3  # v3, v2, v1

    new_v3_entry = dataset["history"][0]
    original_v1_entry = dataset["history"][2]

    assert new_v3_entry["version"] == "v3"
    # The new v3 should have the same content hash and R2 key as the original v1
    assert new_v3_entry["sha256"] == original_v1_entry["sha256"]
    assert new_v3_entry["r2_object_key"] == original_v1_entry["r2_object_key"]
    assert new_v3_entry["commit"] == "pending-merge"
    assert new_v3_entry["description"] == "Rollback to version v1"


def test_rollback_no_op(test_repo: Path, mocker: MockerFixture) -> None:
    """Test rolling back to the version that is already the latest."""
    os.chdir(test_repo)
    # Act: Try to roll back from v2 to v2
    result = runner.invoke(
        app, ["rollback", "core-dataset.sqlite", "--to-version", "v2"]
    )

    assert result.exit_code == 0, result.stdout
    assert "No action needed" in result.stdout


def test_rollback_version_not_found(test_repo: Path, mocker: MockerFixture) -> None:
    """Test rolling back to a version that does not exist."""
    os.chdir(test_repo)
    result = runner.invoke(
        app, ["rollback", "core-dataset.sqlite", "--to-version", "v99"]
    )

    assert result.exit_code == 1
    assert "Version 'v99' not found" in result.stdout


def test_rollback_dataset_not_found(test_repo: Path, mocker: MockerFixture) -> None:
    """Test rolling back a dataset that does not exist."""
    os.chdir(test_repo)
    result = runner.invoke(
        app, ["rollback", "non-existent.sqlite", "--to-version", "v1"]
    )

    assert result.exit_code == 1
    assert "Dataset 'non-existent.sqlite' not found" in result.stdout


def test_rollback_user_cancel(test_repo: Path, mocker: MockerFixture) -> None:
    """Test that the operation is cancelled if the user says no."""
    os.chdir(test_repo)
    # Mock the confirmation prompt to return 'no'
    mocker.patch("datamanager.__main__._ask_confirm", return_value=False)
    # Get the original state of the manifest
    original_manifest = manifest.read_manifest()

    result = runner.invoke(
        app, ["rollback", "core-dataset.sqlite", "--to-version", "v1"]
    )

    assert result.exit_code == 0, result.stdout
    assert "Rollback cancelled" in result.stdout

    # Verify the manifest file was not changed
    final_manifest = manifest.read_manifest()
    assert final_manifest == original_manifest


def test_delete_success(test_repo: Path, mocker: MockerFixture) -> None:
    """Test successfully marking a dataset for deletion."""
    os.chdir(test_repo)
    # Mock the text confirmation to succeed
    mocker.patch(
        "questionary.text",
        return_value=mocker.Mock(ask=mocker.Mock(return_value="core-dataset.sqlite")),
    )

    result = runner.invoke(app, ["delete", "core-dataset.sqlite"])

    assert result.exit_code == 0, result.stdout
    assert "has been marked for deletion" in result.stdout

    # Verify the manifest was updated with the status flag
    dataset = manifest.get_dataset("core-dataset.sqlite")
    assert dataset is not None
    assert dataset.get("status") == "pending-deletion"


def test_delete_confirmation_failed(test_repo: Path, mocker: MockerFixture) -> None:
    """Test that deletion is cancelled if the typed confirmation is wrong."""
    os.chdir(test_repo)
    # Mock the text confirmation to fail
    mocker.patch(
        "questionary.text",
        return_value=mocker.Mock(ask=mocker.Mock(return_value="wrong-name")),
    )
    original_manifest = manifest.read_manifest()

    result = runner.invoke(app, ["delete", "core-dataset.sqlite"])

    assert result.exit_code == 0, result.stdout
    assert "Confirmation failed. Deletion cancelled." in result.stdout

    # Verify the manifest was NOT changed
    final_manifest = manifest.read_manifest()
    assert final_manifest == original_manifest


def test_prune_versions_success(test_repo: Path, mocker: MockerFixture) -> None:
    """Test successfully marking old versions for deletion."""
    os.chdir(test_repo)
    mocker.patch("datamanager.__main__._ask_confirm", return_value=True)

    # Act: Keep the latest 1 version, which should mark v1 for deletion
    result = runner.invoke(
        app, ["prune-versions", "core-dataset.sqlite", "--keep", "1"]
    )

    assert result.exit_code == 0, result.stdout
    assert "1 version(s) have been marked for deletion" in result.stdout

    # Assert: Check that the v1 entry in the manifest is now marked
    dataset = manifest.get_dataset("core-dataset.sqlite")
    assert dataset is not None
    v2_entry = dataset["history"][0]
    v1_entry = dataset["history"][1]
    assert "status" not in v2_entry  # v2 is kept, should not be marked
    assert v1_entry.get("status") == "pending-deletion"


def test_prune_versions_no_op(test_repo: Path, mocker: MockerFixture) -> None:
    """Test pruning when the number to keep is >= the number of versions."""
    os.chdir(test_repo)
    result = runner.invoke(
        app, ["prune-versions", "core-dataset.sqlite", "--keep", "5"]
    )

    assert result.exit_code == 0, result.stdout
    assert "No action needed" in result.stdout


def test_prepare_internal_bucket(test_repo: Path, mocker: MockerFixture) -> None:
    """Test the 'prepare' command for internal bucket."""
    os.chdir(test_repo)
    new_file = test_repo / "internal_dataset.sqlite"
    new_file.touch()

    mocker.patch("datamanager.core.get_r2_client")
    mock_upload = mocker.patch("datamanager.core.upload_to_staging")

    result = runner.invoke(
        app,
        ["prepare", "internal-dataset.sqlite", str(new_file), "--bucket", "internal"],
    )

    assert result.exit_code == 0, result.stdout
    assert "Preparation complete!" in result.stdout
    mock_upload.assert_called_once()

    # Verify the manifest entry was created correctly for internal bucket
    dataset = manifest.get_dataset("internal-dataset.sqlite")
    assert dataset is not None
    assert dataset["bucket"] == "internal"
    assert dataset["history"][0]["diffFromPrevious"] is None
    assert dataset["history"][0]["description"] == "pending-merge"


def test_pull_internal_bucket(test_repo: Path, mocker: MockerFixture) -> None:
    """Test the 'pull' command for internal bucket."""
    os.chdir(test_repo)
    mock_pull = mocker.patch("datamanager.core.pull_and_verify", return_value=True)

    result = runner.invoke(app, ["pull", "core-dataset.sqlite", "--bucket", "internal"])

    assert result.exit_code == 0
    assert "✅ Success!" in result.stdout
    mock_pull.assert_called_once()
    call_args = mock_pull.call_args[0]
    # Should use internal bucket
    assert call_args[3] == "internal"  # bucket parameter
