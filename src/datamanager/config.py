# src/datamanager/config.py
from __future__ import annotations

import warnings
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from dotenv import dotenv_values, find_dotenv

_ENV_PATH = Path(find_dotenv()) if find_dotenv() else None
_ENV = dotenv_values(_ENV_PATH) if _ENV_PATH else {}


def _need(var: str) -> str:
    val = _ENV.get(var)
    if val is None:
        warnings.warn(
            f"Env var {var} is missing – using dummy value for offline/test mode",
            RuntimeWarning,
        )
        val = "DUMMY"
    return str(val)


@dataclass(frozen=True)
class Settings:
    account_id: str = _need("R2_ACCOUNT_ID")
    access_key: str = _need("R2_ACCESS_KEY_ID")
    secret_key: str = _need("R2_SECRET_ACCESS_KEY")
    bucket: str = _need("R2_PRODUCTION_BUCKET")
    staging_bucket: str = _need("R2_STAGING_BUCKET")
    manifest_file: str = "manifest.json"
    max_diff_lines: int = 500

    @cached_property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


settings = Settings()
