# The Data Publishing Workflow

All changes to the data—whether creating, updating, or deleting—follow a strict, safe, and reviewable Git-based workflow.

## Step 1: Create a New Branch

Always start by creating a new branch from the latest version of `main`. This isolates your changes.

```bash
git checkout main
git pull
git checkout -b feat/update-energy-data
```

## Step 2: Prepare Your Changes

Use the `datamanager` tool to stage your changes. The `prepare` command handles both creating new datasets and updating existing ones.

```bash
# This uploads the file to the staging bucket and updates manifest.json locally
uv run datamanager prepare energy-data.sqlite ./local-files/new-energy.sqlite
```

The tool will guide you through the process. For other maintenance tasks like `rollback` or `delete`, use the corresponding command.

## Step 3: Commit and Push

Commit the modified `manifest.json` file to your branch with a descriptive message. This message will become the official description for the new data version.

```bash
git add manifest.json
git commit -m "feat: Add 2025 energy data with new technology columns"
git push --set-upstream origin feat/update-energy-data
```

## Step 4: Open a Pull Request

Go to GitHub and open a pull request from your feature branch to `main`. The diff will clearly show the proposed changes to the manifest for your team to review.

## Step 5: Merge and Automate

Once the PR is reviewed, approved, and all status checks pass, merge it. The CI/CD pipeline takes over automatically:

- It copies the data from the staging bucket to the appropriate target bucket (production or internal).
- It finalizes the `manifest.json` with the new commit hash and description.
- It pushes a final commit back to `main`.

The new data version is now live and available via `datamanager pull`. **Note:** Internal datasets are only accessible to team members with appropriate R2 bucket permissions.
