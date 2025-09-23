# Setup and Installation

This tool requires three Cloudflare R2 buckets:

- **Production bucket**: For publicly accessible datasets
- **Staging bucket**: For temporary uploads during the review process
- **Internal bucket**: For team-only datasets with restricted access

1. **Clone the Repository:**

    ```bash
    git clone git@github.com:TemoaProject/data.git
    cd data
    ```

2. **Install Dependencies:**
    This project uses and recommends `uv` for fast and reliable dependency management.

    ```bash
    # Create a virtual environment and install dependencies
    uv venv
    source .venv/bin/activate
    uv pip install -e .
    ```

    The `-e` flag installs the package in "editable" mode, so changes to the source code are immediately reflected.

3. **Configure Environment Variables:**
    The tool is configured using a `.env` file. Create one by copying the example:

    ```bash
    cp .env.example .env
    ```

    Now, edit the `.env` file with your Cloudflare R2 credentials. **This file should be in your `.gitignore` and never committed to the repository.**

    **`.env`**

    ```ini
    # Get these from your Cloudflare R2 dashboard
    R2_ACCOUNT_ID="your_cloudflare_account_id"
    R2_ACCESS_KEY_ID="your_r2_access_key"
    R2_SECRET_ACCESS_KEY="your_r2_secret_key"
    R2_PRODUCTION_BUCKET="your-production-bucket-name"
    R2_STAGING_BUCKET="your-staging-bucket-name"
    R2_INTERNAL_BUCKET="your-internal-bucket-name"
    ```

4. **Verify Configuration:**
    Run the `verify` command to ensure your credentials and bucket access are correct.

    ```bash
    uv run datamanager verify
    ```

    ![Verify Output](../../assets/verification.png)
