from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

LOG_FILE = "compile_mdx.log"
ALLOWED_PORTALS = ("admin", "client", "govcon360", "public", "asponte")
ALLOWED_API_SOURCES = ("liferay-api", "springboot-api")
TECHNICAL_CATEGORIES = ("architecture", "database", "authentication", "integrations", "infrastructure")

PORTAL_DISPLAY_NAMES: dict[str, str] = {
    "client": "Client Documentation",
    "public": "GovCon360 System Documentation",
    "admin": "Admin Documentation",
    "govcon360": "GovCon360 System Documentation",
    "asponte": "Client Documentation",
}

PORTAL_SIDEBAR_POSITIONS: dict[str, int] = {
    "client": 1,
    "public": 2,
    "admin": 3,
    "govcon360": 2,
    "asponte": 1,
}

DEFAULT_FRONTEND_PORTAL = "public"
DEFAULT_API_PORTAL = "admin"
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
# Automatic fallback model used on the final retry if the primary model keeps returning 503/429.
# Set GEMINI_FALLBACK_MODEL="" in .env to disable fallback.
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-4-hydrogen").strip()

MAX_LLM_ATTEMPTS = 3
LLM_TIMEOUT_SECONDS = 120.0
MIN_SECONDS_BETWEEN_CALLS = float(os.getenv("GEMINI_MIN_INTERVAL", "7"))
MAX_ENDPOINTS_PER_MDX = 15
# Cap Liferay API MDX files per run during pipeline testing (0 = unlimited).
# Set MAX_LIFERAY_API_FILES=0 in env when ready for full production runs.
MAX_LIFERAY_API_FILES = max(0, int(os.getenv("MAX_LIFERAY_API_FILES", "15")))
PROMPT_VERSION = 12

# --- Concurrency and chunking ---------------------------------------------------------------
# Gemini calls running at the same time (2 is safer than 3 on Free-tier keys to avoid simultaneous 503s).
GEMINI_CONCURRENCY = max(1, int(os.getenv("GEMINI_CONCURRENCY", "2")))
# Screenshots blurred at the same time (Tesseract is CPU heavy).
OCR_CONCURRENCY = max(1, int(os.getenv("OCR_CONCURRENCY", "2")))
# Context window of the model, and how many tokens of JSON ONE chunk may use. The chunk budget is
# kept well below the window: the system prompt, the screenshot and the generated MDX share it.
MODEL_CONTEXT_TOKENS = int(os.getenv("GEMINI_CONTEXT_TOKENS", "100000"))
CHUNK_TOKEN_BUDGET = max(2000, min(int(os.getenv("CHUNK_TOKEN_BUDGET", "30000")), int(MODEL_CONTEXT_TOKENS * 0.6)))

# Blur sensitive data (email, phone, UEI, CAGE, Primary NAICS, Client Name) in screenshots before they
# are published to static/img or sent to Gemini. Set REDACT_SCREENSHOTS=0 to turn it off.
REDACT_SCREENSHOTS = os.getenv("REDACT_SCREENSHOTS", "1") != "0"
# Extra strings to always blur, separated by '|' (e.g. your company name): REDACT_TERMS="Acme Inc|Some Name"
REDACT_TERMS = [t.strip() for t in os.getenv("REDACT_TERMS", "").split("|") if t.strip()]
# Also blur people's names that appear next to an email address. Off by default.
REDACT_PERSON_NAMES = os.getenv("REDACT_PERSON_NAMES", "0") == "1"

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
GENERIC_IMAGE_STEMS = frozenset({"screenshot", "screen", "image", "capture", "page"})
GENERIC_JSON_STEMS = frozenset({"page", "index", "data", "knowledge"})
IGNORED_DIR_NAMES = frozenset({"docs", "static", "node_modules", ".git", "__pycache__", ".venv", "venv", "build"})

POISON_PAGE_TITLES = frozenset({
    "404 not found", "page not found", "500 internal server error", "502 bad gateway",
    "503 service unavailable", "access denied", "403 forbidden",
})

# Ordering for auto-generated Docusaurus module categories
CATEGORY_ORDER: dict[str, tuple[str, int]] = {
    "getting-started": ("Getting started", 1),
    "market-intel": ("Market Intel", 2),
    "lead-prospector": ("Lead Prospector", 3),
    "business-development": ("Business Development", 4),
    "teaming-partnerships": ("Teaming Partnerships", 5),
    "proposals": ("Proposals", 6),
    "contracts": ("Contracts", 7),
    "administration": ("Administration", 8),
    "data-management": ("Data Management", 9),
    "commerce": ("Commerce", 10),
    "marketing-management": ("Marketing Management", 11),
    "reference": ("Reference", 12),
    "general": ("General", 50),
}


class FatalLLMError(RuntimeError):
    """Bad key / unknown model / no permission: stop the run immediately."""


def setup_logging() -> logging.Logger:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    root = logging.getLogger()
    if not root.handlers:
        fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
        logfile = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        logfile.setFormatter(fmt)
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(logfile)
        root.addHandler(console)
        root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("mdx_compiler")


logger = setup_logging()