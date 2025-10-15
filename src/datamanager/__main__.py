# datamanager/__main__.py
import subprocess
from datetime import datetime, timezone
import tempfile
import re
from dateutil.parser import isoparse
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.table import Table

from typing import Callable, Optional, Any

from datamanager.config import settings
from datamanager import core, manifest


# Common options for all commands
COMMON_OPTIONS = dict(
    no_prompt=typer.Option(
        False,
        "--yes",
        "-y",
        help="Run non-interactively: auto-accept all prompts and use defaults.",
    )
)


def _ask_confirm(ctx: typer.Context, prompt: str, default: bool = False) -> bool:
    if ctx.obj.get("no_prompt"):
        return True
    result: Optional[bool] = questionary.confirm(prompt, default=default).ask()
    return bool(result)  # Cast to bool to avoid NoneType issues


def _rel(iso: str) -> str:
    dt = isoparse(iso)
    delta = datetime.now(timezone.utc) - dt
    hours = int(delta.total_seconds() // 3600)
    return f"{hours} h ago"


def _validate_temoa_hash(temoa_hash: str) -> bool:
    """
    Validates that a temoa repo hash looks like a valid git commit hash.
    accepts 4-40 hexadecimal characters (case-insensitive).
    """
    if not temoa_hash or not temoa_hash.strip():
        return True  # Empty is allowed (optional field)

    temoa_hash = temoa_hash.strip()
    # Git commit hashes are hexadecimal and can be 4-40 characters
    if re.match(r"^[a-fA-F0-9]{4,40}$", temoa_hash):
        return True

    return False


# Initialize Typer app and Rich console
app = typer.Typer(
    name="datamanager",
    help="A CLI for managing versioned data in R2.",
    add_completion=False,
)
console = Console()


@app.command()
def verify(ctx: typer.Context) -> None:
    """Verifies Cloudflare R2 credentials and granular bucket permissions."""
    console.print("🔍 Verifying Cloudflare R2 configuration...")

    results = core.verify_r2_access()

    table = Table(
        "Target",
        "Status",
        "Read",
        "Write",
        "Delete",
        "Details",
        title="R2 Bucket Permissions Report",
    )

    overall_success = True
    for res in results:
        status_icon = (
            "✅" if res["exists"] and all(res["permissions"].values()) else "❌"
        )
        if not res["exists"]:
            overall_success = False

        table.add_row(
            f"[bold cyan]{res['bucket_name']}[/]",
            f"{status_icon} {res['message']}",
            "✅" if res["permissions"]["read"] else "❌",
            "✅" if res["permissions"]["write"] else "❌",
            "✅" if res["permissions"]["delete"] else "❌",
            res["message"] if not res["exists"] else "",
        )

    console.print(table)

    if not overall_success:
        console.print("\n[bold red]One or more critical checks failed.[/]")
        raise typer.Exit(1)
    else:
        console.print("\n[bold green]All checks passed with expected permissions.[/]")


@app.command()
def list_datasets(ctx: typer.Context) -> None:
    """Lists all datasets tracked in the manifest."""
    data = manifest.read_manifest()
    table = Table(
        "Dataset Name", "Latest Version", "Last Updated", "SHA256", "Temoa Hash"
    )
    for item in data:
        latest = item["history"][0]
        temoa_hash_display = "N/A"
        if latest.get("temoaRepoHash"):
            temoa_hash_display = (
                f"{latest['temoaRepoHash'][:12]}..."
                if len(str(latest["temoaRepoHash"])) > 12
                else str(latest["temoaRepoHash"])
            )

        table.add_row(
            item["fileName"],
            latest["version"],
            # latest["timestamp"],
            f"{_rel(latest['timestamp'])} ({latest['timestamp']})",
            f"{latest['sha256'][:12]}...",
            temoa_hash_display,
        )
    console.print(table)


def _run_pull_logic(name: str, version: str, output: Optional[Path]) -> None:
    """The core logic for pulling and verifying a dataset."""
    console.print(f"🔎 Locating version '{version}' for dataset '{name}'...")
    version_entry = manifest.get_version_entry(name, version)

    if not version_entry:
        console.print(
            f"[bold red]Error:[/] Could not find version '{version}' for dataset '{name}'."
        )
        raise typer.Exit(1)

    if output is None:
        final_path = Path(name)
    elif output.is_dir():
        final_path = output / name
    else:
        final_path = output

    temoa_hash_info = ""
    if version_entry.get("temoaRepoHash"):
        temoa_hash_info = f", temoa: {version_entry['temoaRepoHash']}"

    console.print(
        f"Pulling version [magenta]{version_entry['version']}[/] (commit: {version_entry['commit']}{temoa_hash_info}) to [cyan]{final_path}[/]"
    )

    success = core.pull_and_verify(
        version_entry["r2_object_key"], version_entry["sha256"], final_path
    )

    if success:
        console.print(
            f"\n[bold green]✅ Success![/] File saved to [cyan]{final_path}[/]"
        )
    else:
        console.print(
            "\n[bold red]❌ Pull failed.[/] Please check the error messages above."
        )
        raise typer.Exit(1)


@app.command()
def pull(
    name: str = typer.Argument(..., help="The logical name of the dataset to pull."),
    version: str = typer.Option(
        "latest",
        "--version",
        "-v",
        help="Version to pull (e.g., 'v1'). Defaults to latest.",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output path for the file. Defaults to the dataset name in the current directory.",
    ),
) -> None:
    """Pulls a specific version of a dataset from R2 and verifies its integrity."""
    _run_pull_logic(name, version, output)


def _pull_interactive(ctx: typer.Context) -> None:
    """Guides the user through pulling a specific dataset version interactively."""
    console.print("\n[bold]Interactive Dataset Pull[/]")

    all_datasets: list[dict[str, Any]] = manifest.read_manifest()
    if not all_datasets:
        console.print("[yellow]No datasets found in the manifest to pull.[/]")
        return

    dataset_names = [ds["fileName"] for ds in all_datasets]
    selected_name = questionary.select(
        "Which dataset would you like to pull?", choices=dataset_names
    ).ask()

    if selected_name is None:
        console.print("Pull cancelled.")
        return

    dataset = manifest.get_dataset(selected_name)
    if not dataset or not dataset["history"]:
        console.print(f"[red]Error: No version history found for {selected_name}.[/]")
        return

    version_choices = []
    for entry in dataset["history"]:
        temoa_info = f", temoa: {entry.get('temoaRepoHash', 'N/A')}"
        version_choices.append(
            f"{entry['version']} (commit: {entry['commit']}, {_rel(entry['timestamp'])}{temoa_info})"
        )
    selected_version_str = questionary.select(
        "Which version would you like to pull?", choices=version_choices
    ).ask()

    if selected_version_str is None:
        console.print("Pull cancelled.")
        return

    version_to_pull = selected_version_str.split(" ")[0]

    output_path_str = questionary.path(
        "Enter the output path (or press Enter to save in current directory):",
        default=f"./{selected_name}",
    ).ask()

    if output_path_str is None:
        console.print("Pull cancelled.")
        return

    try:
        _run_pull_logic(
            name=selected_name,
            version=version_to_pull,
            output=Path(output_path_str),
        )
    except typer.Exit:
        pass


def _run_prepare_logic(ctx: typer.Context, name: str, file: Path) -> None:
    """The core logic for preparing a dataset for release."""
    console.print(f"🚀 Preparing update for [cyan]{name}[/]...")

    new_hash = core.hash_file(file)
    dataset = manifest.get_dataset(name)
    client = core.get_r2_client()  # Moved up to be available for diffing

    # Prompt for temoa repo hash (optional)
    temoa_hash = None
    if not ctx.obj.get("no_prompt"):
        console.print("\n[bold]Temoa Repository Hash[/]")
        console.print(
            "This helps track which version of the temoa repository this database works against."
        )

        while True:
            temoa_hash_input = questionary.text(
                "Enter the temoa repository commit hash (optional, press Enter to skip):",
                default="",
            ).ask()

            if not temoa_hash_input or not temoa_hash_input.strip():
                console.print("Skipping temoa repo hash (optional field).")
                break

            temoa_hash_candidate = temoa_hash_input.strip()
            if _validate_temoa_hash(temoa_hash_candidate):
                temoa_hash = temoa_hash_candidate
                console.print(f"Using temoa repo hash: [green]{temoa_hash}[/]")
                break
            else:
                console.print(
                    f"[bold red]Invalid format:[/] '{temoa_hash_candidate}' doesn't look like a valid git commit hash."
                )
                console.print(
                    "Git commit hashes should contain only hexadecimal characters (0-9, a-f, A-F) and be 4-40 characters long."
                )
                retry = questionary.confirm(
                    "Would you like to try again?", default=True
                ).ask()
                if not retry:
                    console.print("Skipping temoa repo hash.")
                    break
    else:
        # In non-interactive mode, temoa hash is not provided
        console.print(
            "Running in non-interactive mode - temoa repo hash not specified."
        )

    # Check for changes BEFORE doing any uploads.
    if dataset:
        latest_version = dataset["history"][0]
        if new_hash == latest_version["sha256"]:
            console.print("✅ No changes detected. Manifest is already up to date.")
            return

    # If we've reached this point, an upload is necessary.
    staging_key = f"staging-uploads/{new_hash}.sqlite"
    core.upload_to_staging(client, file, staging_key)

    # Now, determine if this is a create or update to build the manifest entry
    if dataset:
        # --- This is an UPDATE ---
        latest_version = dataset["history"][0]
        prev_version_num = int(latest_version["version"].lstrip("v"))
        new_version = f"v{prev_version_num + 1}"
        r2_dir = Path(latest_version["r2_object_key"]).parent
        final_r2_key = f"{r2_dir}/{new_version}-{new_hash}.sqlite"

        console.print(f"Change detected! Preparing new version: {new_version}")

        console.print("Downloading previous version to generate diff...")
        diff_git_path: Optional[Path] = None
        with tempfile.TemporaryDirectory() as tempdir:
            old_path = Path(tempdir) / "prev.sqlite"
            # Download from the PRODUCTION bucket
            core.download_from_r2(client, latest_version["r2_object_key"], old_path)

            full_diff, summary = core.generate_sql_diff(old_path, file)

        if full_diff.count("\n") <= settings.max_diff_lines:
            to_save = summary + full_diff
            msg = "📝  Full diff stored in Git at:"
        else:
            to_save = summary
            msg = "📝  Full diff too large; summary stored in Git at:"

        diff_filename = f"diff-{latest_version['version']}-to-{new_version}.diff"
        diff_git_path = Path("diffs") / name / diff_filename
        diff_git_path.parent.mkdir(parents=True, exist_ok=True)
        diff_git_path.write_text(to_save)
        subprocess.run(["git", "add", str(diff_git_path)])

        console.print(f"{msg} [green]{diff_git_path}[/]")

        new_entry = {
            "version": new_version,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sha256": new_hash,
            "r2_object_key": final_r2_key,
            "staging_key": staging_key,
            "diffFromPrevious": str(diff_git_path)
            if diff_git_path
            else None,  # Add path to entry
            "commit": "pending-merge",
            "temoaRepoHash": temoa_hash,
            "description": "pending-merge",
        }
        manifest.add_history_entry(name, new_entry)

    else:
        # --- This is for CREATE ---
        new_dataset_obj = {
            "fileName": name,
            "latestVersion": "v1",
            "history": [
                {
                    "version": "v1",
                    "timestamp": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "sha256": new_hash,
                    "r2_object_key": f"{Path(Path(name).stem)}/v1-{new_hash}.sqlite",
                    "staging_key": staging_key,
                    "diffFromPrevious": None,  # Explicitly None for new datasets
                    "commit": "pending-merge",
                    "temoaRepoHash": temoa_hash,
                    "description": "pending-merge",
                }
            ],
        }
        manifest.add_new_dataset(new_dataset_obj)

    console.print(
        f"\n[bold green]✅ Preparation complete![/] Manifest file '{settings.manifest_file}' has been updated."
    )
    console.print(
        "\nNext steps:\n"
        "  1. [cyan]git add .[/]\n"
        '  2. [cyan]git commit -m "feat: Prepare update for {name}"[/]\n'
        "  3. [cyan]git push[/]\n"
        "  4. Open a Pull Request to merge your changes into the main branch."
    )


@app.command()
def prepare(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="The logical name of the dataset."),
    file: Path = typer.Argument(..., help="Path to the .sqlite file.", exists=True),
    no_prompt: bool = COMMON_OPTIONS["no_prompt"],
) -> None:
    """
    Prepares a dataset for release: uploads to staging and updates the manifest.
    This is the first step in the CI/CD-driven workflow.
    """
    ctx.obj["no_prompt"] = no_prompt or ctx.obj.get("no_prompt")
    _run_prepare_logic(ctx, name, file)


def _prepare_interactive(ctx: typer.Context) -> None:
    """Guides the user through preparing a dataset for release."""
    console.print("\n[bold]Interactive Dataset Preparation[/]")

    selected_file_str = questionary.path(
        "Enter the path to the .sqlite file:",
        validate=lambda path: Path(path).is_file(),
    ).ask()
    if selected_file_str is None:
        console.print("Preparation cancelled.")
        return

    default_name = Path(selected_file_str).name
    selected_name = questionary.text(
        "Enter the logical name for this dataset:",
        default=default_name,
        validate=lambda text: len(text) > 0 or "Name cannot be empty.",
    ).ask()
    if selected_name is None:
        console.print("Preparation cancelled.")
        return

    console.print(
        f"\nYou are about to prepare dataset [cyan]{selected_name}[/] from file [green]{selected_file_str}[/]."
    )
    proceed = _ask_confirm(ctx, "Do you want to continue?", default=False)
    if not proceed:
        console.print("Preparation cancelled.")
        return

    try:
        _run_prepare_logic(ctx, name=selected_name, file=Path(selected_file_str))
    except typer.Exit:
        pass


def _run_rollback_logic(ctx: typer.Context, name: str, to_version: str) -> None:
    """The core logic for rolling back a dataset to a previous version."""
    console.print(
        f"⏪ Preparing rollback for [cyan]{name}[/] to version [magenta]{to_version}[/]..."
    )

    dataset = manifest.get_dataset(name)
    if not dataset:
        console.print(f"[red]Error: Dataset '{name}' not found.[/]")
        raise typer.Exit(1)

    target_entry = manifest.get_version_entry(name, to_version)
    if not target_entry:
        console.print(
            f"[red]Error: Version '{to_version}' not found for dataset '{name}'.[/]"
        )
        raise typer.Exit(1)

    latest_version = dataset["history"][0]
    if latest_version["sha256"] == target_entry["sha256"]:
        console.print(
            f"✅ No action needed. The latest version is already identical to '{to_version}'."
        )
        return

    new_version_num = int(latest_version["version"].lstrip("v")) + 1
    new_version = f"v{new_version_num}"

    console.print(
        f"This will create a new version, [magenta]{new_version}[/], whose contents will be identical to [magenta]{to_version}[/]."
    )
    proceed = _ask_confirm(ctx, "Do you want to continue?", default=False)
    if not proceed:
        console.print("Rollback cancelled.")
        return

    rollback_entry = {
        "version": new_version,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sha256": target_entry["sha256"],
        "r2_object_key": target_entry["r2_object_key"],
        "diffFromPrevious": None,
        "commit": "pending-merge",
        "temoaRepoHash": target_entry.get("temoaRepoHash"),
        "description": f"Rollback to version {target_entry['version']}",
    }

    manifest.add_history_entry(name, rollback_entry)
    manifest.update_latest_version(name, new_version)

    console.print(
        "\n[bold green]✅ Rollback prepared![/] Manifest file has been updated."
    )
    console.print(
        "\nNext steps:\n"
        f"  1. [cyan]git add {settings.manifest_file}[/]\n"
        f'  2. [cyan]git commit -m "revert: Roll back {name} to {to_version}"[/]\n'
        "  3. [cyan]git push[/]\n"
        "  4. Open a Pull Request to finalize the rollback."
    )


@app.command()
def rollback(
    ctx: typer.Context,
    name: str = typer.Argument(
        ..., help="The logical name of the dataset to roll back."
    ),
    to_version: str = typer.Option(
        ..., "--to-version", "-v", help="The stable version to restore (e.g., 'v2')."
    ),
    no_prompt: bool = COMMON_OPTIONS["no_prompt"],
) -> None:
    """
    Rolls back a dataset to a previous stable version by creating a new version entry.
    """
    ctx.obj["no_prompt"] = no_prompt or ctx.obj.get("no_prompt")
    _run_rollback_logic(ctx, name, to_version)


def _rollback_interactive(ctx: typer.Context) -> None:
    """Guides the user through rolling back a dataset interactively."""
    console.print("\n[bold]Interactive Dataset Rollback[/]")

    all_datasets: list[dict[str, Any]] = manifest.read_manifest()
    if not all_datasets:
        console.print("[yellow]No datasets found in the manifest to roll back.[/]")
        return

    dataset_names = [ds["fileName"] for ds in all_datasets]
    selected_name = questionary.select(
        "Which dataset would you like to roll back?", choices=dataset_names
    ).ask()

    if selected_name is None:
        console.print("Rollback cancelled.")
        return

    dataset = manifest.get_dataset(selected_name)
    if not dataset or len(dataset["history"]) < 2:
        console.print(
            f"[yellow]Not enough version history for '{selected_name}' to perform a rollback.[/]"
        )
        return

    # Exclude the latest version from the choices, as you can't roll back to it.
    version_choices = []
    for entry in dataset["history"][1:]:  # Start from the second entry
        temoa_info = f", temoa: {entry.get('temoaRepoHash', 'N/A')}"
        version_choices.append(
            f"{entry['version']} (commit: {entry['commit']}, {_rel(entry['timestamp'])}{temoa_info})"
        )
    selected_version_str = questionary.select(
        "Which version do you want to restore?", choices=version_choices
    ).ask()

    if selected_version_str is None:
        console.print("Rollback cancelled.")
        return

    version_to_restore = selected_version_str.split(" ")[0]

    try:
        _run_rollback_logic(ctx, name=selected_name, to_version=version_to_restore)
    except typer.Exit:
        pass


def _run_delete_logic(ctx: typer.Context, name: str) -> None:
    """The core logic for marking a dataset for deletion."""
    console.print(f"🗑️  Preparing deletion for [bold red]{name}[/].")

    if not manifest.get_dataset(name):
        console.print(f"[red]Error: Dataset '{name}' not found.[/]")
        raise typer.Exit(1)

    console.print(
        "[bold yellow]WARNING:[/] This will propose the [underline]permanent deletion[/] of the dataset and all its version history from Cloudflare R2."
    )

    confirmation = questionary.text(
        f"To confirm, please type the name of the dataset ({name}):"
    ).ask()

    if confirmation != name:
        console.print("Confirmation failed. Deletion cancelled.")
        return

    if manifest.mark_for_deletion(name):
        console.print(
            f"\n[bold green]✅ Dataset '{name}' has been marked for deletion.[/]"
        )
        console.print(
            "\nNext steps:\n"
            f"  1. [cyan]git add {settings.manifest_file}[/]\n"
            f'  2. [cyan]git commit -m "chore: Mark {name} for deletion"[/]\n'
            "  3. [cyan]git push[/]\n"
            "  4. Open a Pull Request to finalize the deletion."
        )
    else:
        console.print(f"[red]Error: Could not mark '{name}' for deletion.[/]")
        raise typer.Exit(1)


def _run_prune_versions_logic(ctx: typer.Context, name: str, keep: int) -> None:
    """The core logic for marking old versions for deletion."""
    console.print(f"🔪 Preparing to prune old versions of [cyan]{name}[/]...")

    dataset = manifest.get_dataset(name)
    if not dataset:
        console.print(f"[red]Error: Dataset '{name}' not found.[/]")
        raise typer.Exit(1)

    history = dataset.get("history", [])
    if len(history) <= keep:
        console.print(
            f"✅ No action needed. Dataset has {len(history)} version(s), which is not more than the {keep} to keep."
        )
        return

    versions_to_keep = [entry["version"] for entry in history[:keep]]
    versions_to_delete = [entry["version"] for entry in history[keep:]]

    console.print(
        f"You have chosen to keep the [bold green]{keep}[/] most recent version(s):"
    )
    for v in versions_to_keep:
        console.print(f"  - [green]{v}[/]")

    console.print(
        f"\nThe following [bold red]{len(versions_to_delete)}[/] older version(s) will be marked for permanent deletion:"
    )
    for v in versions_to_delete:
        console.print(f"  - [red]{v}[/]")

    proceed = _ask_confirm(ctx, "\nDo you want to continue?", default=False)
    if not proceed:
        console.print("Pruning cancelled.")
        return

    manifest.mark_versions_for_deletion(name, versions_to_delete)
    console.print(
        f"\n[bold green]✅ {len(versions_to_delete)} version(s) have been marked for deletion.[/]"
    )
    console.print(
        "\nNext steps:\n"
        f"  1. [cyan]git add {settings.manifest_file}[/]\n"
        f'  2. [cyan]git commit -m "chore: Prune old versions of {name}, keeping {keep}"[/]\n'
        "  3. [cyan]git push[/]\n"
        "  4. Open a Pull Request to finalize the deletion."
    )


@app.command()
def delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="The logical name of the dataset to delete."),
) -> None:
    """Marks a dataset for permanent deletion via a PR."""
    _run_delete_logic(ctx, name)


@app.command()
def prune_versions(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="The logical name of the dataset to prune."),
    keep: int = typer.Option(
        ..., "--keep", "-k", help="The number of most recent versions to keep."
    ),
) -> None:
    """Marks old versions of a dataset for permanent deletion via a PR."""
    _run_prune_versions_logic(ctx, name, keep)


def _delete_interactive(ctx: typer.Context) -> None:
    """Guides the user through deleting a dataset interactively."""
    console.print("\n[bold]Interactive Dataset Deletion[/]")
    all_datasets = manifest.read_manifest()
    if not all_datasets:
        console.print("[yellow]No datasets found to delete.[/]")
        return

    dataset_names = [ds["fileName"] for ds in all_datasets]
    selected_name = questionary.select(
        "Which dataset would you like to mark for deletion?", choices=dataset_names
    ).ask()

    if selected_name is None:
        console.print("Deletion cancelled.")
        return

    try:
        _run_delete_logic(ctx, name=selected_name)
    except typer.Exit:
        pass


def _prune_versions_interactive(ctx: typer.Context) -> None:
    """Guides the user through pruning old versions interactively."""
    console.print("\n[bold]Interactive Version Pruning[/]")
    all_datasets = manifest.read_manifest()
    if not all_datasets:
        console.print("[yellow]No datasets found to prune.[/]")
        return

    dataset_names = [ds["fileName"] for ds in all_datasets]
    selected_name = questionary.select(
        "Which dataset would you like to prune?", choices=dataset_names
    ).ask()

    if selected_name is None:
        console.print("Pruning cancelled.")
        return

    keep_str = questionary.text(
        "How many of the most recent versions do you want to keep?",
        validate=lambda text: text.isdigit()
        and int(text) > 0
        or "Please enter a positive number.",
    ).ask()

    if keep_str is None:
        console.print("Pruning cancelled.")
        return

    try:
        _run_prune_versions_logic(ctx, name=selected_name, keep=int(keep_str))
    except typer.Exit:
        pass


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, no_prompt: bool = COMMON_OPTIONS["no_prompt"]) -> None:
    """
    Entrypoint – when no sub-command is given we show a simple TUI unless
    --yes is supplied (non-interactive mode).
    """
    ctx.ensure_object(dict)
    ctx.obj["no_prompt"] = no_prompt

    if ctx.invoked_subcommand:
        return

    if no_prompt:
        console.print("[red]--yes supplied but no sub-command given; exiting.[/]")
        raise typer.Exit(code=1)

    console.print("[bold yellow]Welcome to the Data Manager TUI![/]")

    actions: dict[str, Callable[[typer.Context], None] | str] = {
        "List all datasets": list_datasets,
        "Prepare a dataset for release": _prepare_interactive,
        "Pull a dataset version": _pull_interactive,
        "Rollback a dataset to a previous version": _rollback_interactive,
        "Delete a dataset": _delete_interactive,
        "Prune old dataset versions": _prune_versions_interactive,
        "Verify R2 configuration": verify,
        "Exit": "exit",
    }

    choice = questionary.select(
        "What would you like to do?", choices=list(actions.keys())
    ).ask()

    if choice == "Exit" or choice is None:
        console.print("Goodbye!")
        raise typer.Exit()

    action = actions[choice]
    if callable(action):
        action(ctx)
    else:
        raise NotImplementedError(f"Action '{choice}' is not implemented yet.")


if __name__ == "__main__":
    app()
