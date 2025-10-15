
# Usage

The primary workflow is now to **prepare** a dataset, then use standard Git practices to propose the change.

## Interactive TUI

For a guided experience, simply run the command with no arguments:

```bash
uv run datamanager
```

This will launch a menu where you can choose your desired action, including the new "Prepare a dataset for release" option.

![TUI](../../assets/tui.png)

## Command-Line Interface (CLI)

You can also use the command-line interface directly for specific tasks or for scripting purposes.

![CLI](../../assets/cli.png)

## Core Commands

### `prepare`

Prepares a dataset for release by uploading it to the staging area and updating the manifest locally. This command intelligently handles both creating new datasets and updating existing ones.

**This is the first step of the new workflow.**

```bash
uv run datamanager prepare <dataset-name.sqlite> <path/to/local/file.sqlite>
```

When preparing a dataset, you will be prompted for an optional **Temoa Repository Hash** (git commit hash). This helps track which version of the temoa repository this database works against. You can:

- Enter a valid git commit hash (e.g., `abc1234` or `a1b2c3d4e5f6...`)
- Press Enter to skip (optional field)

After running `prepare`, follow the on-screen instructions:

1. `git add manifest.json`
2. `git commit -m "Your descriptive message"`
3. `git push`
4. Open a Pull Request in GitHub.

![prepare](../../assets/prepare.png)

### `list-datasets`

Lists all datasets currently tracked in `manifest.json`, including the latest version, update time, SHA256 hash, and Temoa repository hash (if available).

```bash
uv run datamanager list-datasets
```

The output includes:

- **Dataset Name**: The logical name of the dataset
- **Latest Version**: The most recent version tag
- **Last Updated**: When the latest version was created (relative time and absolute timestamp)
- **SHA256**: First 12 characters of the file hash
- **Temoa Hash**: First 12 characters of the temoa repository commit hash (or "N/A" if not specified)

![list_datasets](../../assets/list_datasets.png)

### `pull`

Downloads a dataset from the **production** R2 bucket and verifies its integrity.

```bash
# Pull the latest version
uv run datamanager pull user-profiles.sqlite

# Pull a specific version
uv run datamanager pull user-profiles.sqlite --version v2
```

![pull](../../assets/pull.png)

## Maintenance Commands

### `rollback`

Prepares a rollback to a previous stable version by creating a new version entry that points to the old data.

```bash
uv run datamanager rollback <dataset-name.sqlite> --to-version v1
```

### `delete`

Prepares the **permanent** deletion of an entire dataset and all its versions. Requires strong confirmation.

```bash
uv run datamanager delete <dataset-name.sqlite>
```

### `prune-versions`

Prepares the permanent deletion of old versions of a dataset, keeping a specified number of recent versions.

```bash
uv run datamanager prune-versions <dataset-name.sqlite> --keep 5
```

### `verify`

Checks R2 credentials and reports granular read/write/delete permissions for both production and staging buckets.

```bash
uv run datamanager verify
```
