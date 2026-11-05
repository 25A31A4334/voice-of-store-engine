"""Centralized Application Configuration (Team CodeNova).

Follows Rule 2 (Put settings in one config file) & Rule 3 (Keep secrets out of code).
Enforces security safeguards:
- No predictable/default secrets in non-test execution.
- Safe failure on missing or obviously unsafe secrets.
- Support for explicit test-only secrets during testing.
- Controlled toggle for demo accounts.
- Zero secret values printed in error messages or logs.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import List, Set

ROOT = Path(__file__).resolve().parents[1]
FULL_CODE_DIR = ROOT / "FULL_CODE"


def _load_env_file(env_path: Path) -> None:
    """Robust standard-library .env loader (without external dependencies)."""
    if not env_path.is_file():
        return
    try:
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                elif " #" in val:
                    val = val.split(" #", 1)[0].strip()
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


# Load local .env from all expected locations (project root, CWD, FULL_CODE)
for _dir in [ROOT, Path.cwd(), FULL_CODE_DIR]:
    _load_env_file(_dir / ".env")
    _load_env_file(_dir / ".env.local")
    _load_env_file(_dir / ".env.txt")


class Settings:
    """Application configuration settings."""

    # Project Information
    APP_NAME: str = "R5 — Voice-of-Store Engine"
    TEAM_NAME: str = "CodeNova"
    VERSION: str = "1.1.0"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

    # Application Execution Mode: 'supabase', 'local_dev', or 'offline_eval'
    APP_MODE: str = os.getenv(
        "APP_MODE", "supabase" if os.getenv("SUPABASE_URL") else "local_dev"
    ).lower()

    # Supabase Cloud Configuration
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_PUBLISHABLE_KEY: str = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    )
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    SUPABASE_JWT_AUDIENCE: str = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")

    # Environment mode: 'production', 'development', or 'test'
    APP_ENV: str = os.getenv("APP_ENV", "development").lower()

    # Google Gemini Hybrid AI Configuration
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    GEMINI_ENABLED: bool = os.getenv("GEMINI_ENABLED", "true").lower() in ("true", "1", "yes")
    GEMINI_TIMEOUT_SECONDS: int = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "8"))

    # Test-mode flag (activated during unit/integration tests)
    TESTING: bool = (
        os.getenv("TESTING", "false").lower() in ("true", "1", "yes")
        or "pytest" in sys.modules
        or os.getenv("APP_ENV", "").lower() == "test"
    )

    # Demo accounts toggle (strictly restricted to development/demo mode)
    ENABLE_DEMO_ACCOUNTS: bool = (
        os.getenv(
            "ENABLE_DEMO_ACCOUNTS",
            "true" if os.getenv("APP_ENV", "development").lower() == "development" else "false",
        ).lower() in ("true", "1", "yes")
    )

    # Known unsafe / predictable secrets blacklist
    UNSAFE_SECRETS: Set[str] = {
        "secret",
        "password",
        "admin",
        "123456",
        "12345678",
        "1234567890",
        "changeme",
        "default",
        "jwtsecret",
        "change-this-to-a-secure-random-secret-in-production-min-32-chars",
        "codenova-talentap-hackathon-2026-voice-of-store-dev-secret-key-min32chars",
    }

    # Dedicated explicit test-only secret for automated test environments
    EXPLICIT_TEST_SECRET: str = "test-only-explicit-jwt-secret-key-strictly-for-testing-purposes-min32chars"

    @property
    def JWT_SECRET_KEY(self) -> str:
        """Retrieves and validates the JWT signing secret.

        Fails safely if missing or unsafe in non-test mode. Never prints the secret.
        """
        raw_key = os.getenv("JWT_SECRET_KEY")

        # In test mode, use provided test key or safe test-only fallback
        if self.TESTING or self.APP_ENV == "test":
            return raw_key if (raw_key and raw_key.strip()) else self.EXPLICIT_TEST_SECRET

        # In non-test execution: NO default secret. Fail startup safely.
        if not raw_key or not raw_key.strip():
            raise RuntimeError(
                "FATAL SECURITY CONFIGURATION ERROR: 'JWT_SECRET_KEY' environment variable is missing or empty. "
                "Set a cryptographically strong secret (minimum 32 characters) via environment variable or .env file before running."
            )

        clean_key = raw_key.strip()
        if len(clean_key) < 32:
            raise RuntimeError(
                f"FATAL SECURITY CONFIGURATION ERROR: 'JWT_SECRET_KEY' is insecure (length {len(clean_key)} characters). "
                "A minimum of 32 characters is required for HMAC-SHA256 signing security."
            )

        if clean_key.lower() in self.UNSAFE_SECRETS:
            raise RuntimeError(
                "FATAL SECURITY CONFIGURATION ERROR: 'JWT_SECRET_KEY' matches a known predictable/placeholder pattern. "
                "You must supply an unpredictable random secret."
            )

        return clean_key

    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

    # Password Hashing Parameters (NIST / OWASP standard)
    PBKDF2_ITERATIONS: int = 100_000
    SALT_BYTE_LENGTH: int = 16

    # Database Configuration (Rule 2: schema in repo, .db ignored)
    DATABASE_PATH: Path = Path(os.getenv("DATABASE_PATH", str(FULL_CODE_DIR / "r5_store.db")))

    # CORS Allowed Origins (Rule 4: Restrict CORS)
    CORS_ORIGINS: List[str] = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:3000",
        ).split(",")
        if origin.strip()
    ]

    def validate_startup(self) -> None:
        """Validates configuration at application boot; fails early and safely if misconfigured."""
        if self.APP_MODE == "offline_eval":
            return

        # 1. Supabase Mode Safeguards (when running in Supabase mode in non-test execution)
        if self.APP_MODE == "supabase" and not (self.TESTING or self.APP_ENV == "test"):
            if not self.SUPABASE_URL:
                raise RuntimeError(
                    "FATAL SECURITY CONFIGURATION ERROR: 'SUPABASE_URL' is missing or empty. "
                    "Please configure SUPABASE_URL in your environment or .env file before starting."
                )
            if not self.SUPABASE_PUBLISHABLE_KEY:
                raise RuntimeError(
                    "FATAL SECURITY CONFIGURATION ERROR: 'SUPABASE_PUBLISHABLE_KEY' (or SUPABASE_ANON_KEY) is missing. "
                    "Please configure it in your environment or .env file before starting."
                )

        # 2. Local JWT & Session Safeguards (validates secret length, predictability, and safety)
        _ = self.JWT_SECRET_KEY


settings = Settings()
