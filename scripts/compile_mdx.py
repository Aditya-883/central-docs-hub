#!/usr/bin/env python3
r"""Crawl JSON / API JSON / technical JSON -> Docusaurus v3 .mdx compiler (Google Gemini).

Expected project layout (this script lives in central-docs-hub/scripts/):

    central-docs-hub/
      frontend/<admin|client|public|...>/      -> docusaurus/docs/<portal>/user-guide/*.mdx
      backend/api/liferay/                     -> docusaurus/docs/<portal>/api-documentation/liferay-api/*.mdx
      backend/api/springboot/                  -> docusaurus/docs/<portal>/api-documentation/springboot-api/*.mdx
      backend/technical/<architecture|database|authentication|integrations|infrastructure>/
                                               -> docusaurus/docs/technical/<category>/*.mdx
      scripts/compile_mdx.py
      docusaurus/docs, docusaurus/static       (screenshots go to docusaurus/static/img/<portal>/)

Quick start (Windows cmd, from central-docs-hub\scripts):

    set GEMINI_API_KEY=your-key
    set GEMINI_MODEL=gemini-3.5-flash-lite

    python compile_mdx.py --self-test                 offline check, no API call, no key needed
    python compile_mdx.py --mock                      whole project, no API call
    python compile_mdx.py --limit 1                   ONE real page first (cheap test)
    python compile_mdx.py                             whole project (frontend + api + technical)
    python compile_mdx.py --only frontend             only frontend/ user guides
    python compile_mdx.py --only api                  only backend/api/
    python compile_mdx.py --only technical            only backend/technical/
    python compile_mdx.py --only frontend --portal admin

The project root is auto-detected (the folder above scripts/). Override anything with
--project-dir, --docs-root, --static-root, --manifest. Single files can still be passed with
--frontend-json, --liferay-api-json, --springboot-api-json (each repeatable).

Needs: Python 3.10+, pydantic>=2, google-genai, python-dotenv (optional).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import quote, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(dotenv_path=SCRIPT_DIR / ".env", override=False)
    load_dotenv(dotenv_path=SCRIPT_DIR.parent / ".env", override=False)
except ImportError:
    pass

from pydantic import BaseModel, Field, ValidationError, field_validator

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # only needed for real API calls; --mock and --self-test work without it
    types = None


def detect_project_root() -> Path:
    """central-docs-hub root: the folder (script dir, its parent, or cwd) that has frontend/ or backend/."""
    for base in (SCRIPT_DIR.parent, SCRIPT_DIR, Path.cwd()):
        if (base / "frontend").is_dir() or (base / "backend").is_dir():
            return base
    return SCRIPT_DIR.parent if SCRIPT_DIR.name.lower() == "scripts" else SCRIPT_DIR


PROJECT_ROOT = detect_project_root()
DEFAULT_DOCS_ROOT = PROJECT_ROOT / "docusaurus" / "docs"
DEFAULT_MANIFEST = SCRIPT_DIR / ".sync-manifest.json"


# =====================================================================
# 0. LOGGING
# =====================================================================
LOG_FILE = str(SCRIPT_DIR / "compile_mdx.log")


def setup_logging() -> logging.Logger:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: never crash on a odd character
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
    for noisy in ("httpx", "httpcore", "google_genai"):  # one INFO line per HTTP call is just noise
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("mdx_compiler")


logger = setup_logging()


# =====================================================================
# 1. CONSTANTS
# =====================================================================
ALLOWED_PORTALS = ("admin", "client", "govcon360", "public", "asponte")
ALLOWED_API_SOURCES = ("liferay-api", "springboot-api")
PROJECT_SECTIONS = ("frontend", "api", "technical")
DEFAULT_FRONTEND_PORTAL = "govcon360"
DEFAULT_API_PORTAL = "admin"
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")  # same default as generate_mdx.py
MAX_LLM_ATTEMPTS = 3
LLM_TIMEOUT_SECONDS = 120.0
MIN_SECONDS_BETWEEN_CALLS = float(os.getenv("GEMINI_MIN_INTERVAL", "7"))  # free tier ~10 requests/min
MAX_ENDPOINTS_PER_MDX = 15
TECH_CHUNK_CHARS = int(os.getenv("TECH_CHUNK_CHARS", "60000"))  # bigger technical JSON is split into parts
PROMPT_VERSION = 6  # bumped for backend/technical docs + secret redaction
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
GENERIC_IMAGE_STEMS = frozenset({"screenshot", "screen", "image", "capture", "page"})
GENERIC_JSON_STEMS = frozenset({"page", "index", "data", "knowledge"})

# folder name -> (sidebar label, sidebar position, description)
TECHNICAL_CATEGORIES: dict[str, tuple[str, int, str]] = {
    "architecture": ("Architecture", 1, "System architecture, components and how they interact."),
    "database": ("Database", 2, "Database schemas, tables, relationships and data models."),
    "authentication": ("Authentication", 3, "Authentication and authorization design."),
    "integrations": ("Integrations", 4, "Third-party and internal system integrations."),
    "infrastructure": ("Infrastructure", 5, "Hosting, deployment and infrastructure setup."),
}
TECHNICAL_ORDER = {name: meta[1] for name, meta in TECHNICAL_CATEGORIES.items()}

POISON_PAGE_TITLES = frozenset({
    "404 not found", "page not found", "500 internal server error", "502 bad gateway",
    "503 service unavailable", "access denied", "403 forbidden",
})

# Keys of a crawler record that carry bookkeeping, not page content.
# Note: outgoingLinks/links are extracted separately into FrontendPageItem.links.
META_KEYS = frozenset({
    "screenshot", "screenshots", "evidence", "sourceBlockIndex", "pageHeadingEvidence",
    "source", "outgoingLinks", "links", "pageTypeConfidence",
})
RECORD_HINT_KEYS = frozenset({
    "pageHeading", "pageTitle", "title", "url", "pageUrl", "canonicalUrl", "sections", "intro",
})


class FatalLLMError(RuntimeError):
    """Bad key / unknown model / no permission: every page would fail the same way, so stop the run."""


# =====================================================================
# 2. PYDANTIC (v2) INPUT SCHEMAS
# =====================================================================
class FormFieldItem(BaseModel):
    label: str = Field(..., min_length=1)
    field_type: str = "text"
    required: bool = False
    placeholder: str = ""
    validation_note: str = ""


class LinkItem(BaseModel):
    """One verified internal Docusaurus documentation link (./slug.mdx)."""

    label: str = Field(..., min_length=1)
    target: str = Field(..., min_length=1)


class SectionItem(BaseModel):
    """One heading of a crawled page with its text. Children keep the page hierarchy."""

    heading: str = ""
    paragraphs: list[str] = Field(default_factory=list)
    list_items: list[str] = Field(default_factory=list)
    children: list[SectionItem] = Field(default_factory=list)


SectionItem.model_rebuild()


def _section_has_text(section: SectionItem) -> bool:
    return bool(section.paragraphs or section.list_items) or any(_section_has_text(c) for c in section.children)


class FrontendPageItem(BaseModel):
    """One crawled portal screen."""

    route: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    breadcrumbs: list[str] = Field(default_factory=list)
    headings: list[str] = Field(default_factory=list)
    summary_text: str = ""
    sections: list[SectionItem] = Field(default_factory=list)
    form_fields: list[FormFieldItem] = Field(default_factory=list)
    buttons: list[str] = Field(default_factory=list)
    table_headers: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    links: list[LinkItem] = Field(default_factory=list)
    workflow_steps: list[str] = Field(default_factory=list)
    raw_content: dict[str, Any] = Field(default_factory=dict)  # only for crawl layouts we do not know

    def is_error_page(self) -> bool:
        return self.title.strip().lower() in POISON_PAGE_TITLES

    def has_meaningful_content(self) -> bool:
        if len(self.summary_text.strip()) >= 15:
            return True
        return bool(
            self.headings or self.form_fields or self.buttons or self.table_headers
            or self.workflow_steps or self.raw_content or any(_section_has_text(s) for s in self.sections)
        )


class ApiParameterItem(BaseModel):
    name: str = Field(..., min_length=1)
    location: str = "query"
    data_type: str = "string"
    required: bool = False
    description: str = ""


class ApiEndpointItem(BaseModel):
    method: str = Field(..., min_length=3)
    path: str = Field(..., min_length=1)
    summary: str = ""
    description: str = ""
    controller_or_tag: str = "General"
    auth_required: bool = True
    roles_allowed: list[str] = Field(default_factory=list)
    parameters: list[ApiParameterItem] = Field(default_factory=list)
    request_body_schema: dict[str, Any] | str = Field(default_factory=dict)
    response_schema: dict[str, Any] | str = Field(default_factory=dict)
    error_codes: list[str] = Field(default_factory=list)

    @field_validator("method")
    @classmethod
    def normalize_http_method(cls, v: str) -> str:
        m = v.strip().upper()
        if m not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
            raise ValueError(f"Invalid HTTP method: {v}")
        return m


class BackendApiBundle(BaseModel):
    portal: Literal["admin", "client", "govcon360", "public", "asponte"] = "admin"
    api_source: Literal["liferay-api", "springboot-api"]
    service_name: str = "Core Service"
    version: str = "v1.0"
    base_path: str = "/"
    endpoints: list[ApiEndpointItem] = Field(..., min_length=1)


# =====================================================================
# 3. PRE-LLM CHECKS, PII MASKING, INPUT NORMALIZATION
# =====================================================================
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Needs separators (202-555-0143, (202) 555-0143, +1 202 555 0143) so plain IDs/timestamps are not hit.
PHONE_RE = re.compile(
    r"(?<![\w/.\-])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}(?![\w\-])"
)
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# scheme://user:password@host  -> the password part is a secret (DB / broker connection strings)
URL_CREDENTIALS_RE = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(@)")

# GovCon-specific PII / sensitive entity patterns (CAGE, UEI, Client Name, Primary NAICS)
CAGE_LABELED_RE = re.compile(
    r"(\bCAGE(?:\s+Code)?\s*[:#=\-]\s*)([A-Za-z0-9]{5})\b",
    re.IGNORECASE,
)
UEI_LABELED_RE = re.compile(
    r"(\b(?:UEI|SAM\s+UEI|Unique\s+Entity\s+(?:ID|Identifier))(?:\s+Code)?\s*[:#=\-]\s*)([A-Za-z0-9]{12})\b",
    re.IGNORECASE,
)
PRIMARY_NAICS_LABELED_RE = re.compile(
    r"(\bPrimary\s+NAICS(?:\s+Code)?\s*[:#=\-]\s*)(\d{6}(?:\s*[-–—]\s*[^,;\n|]+)?)",
    re.IGNORECASE,
)
CLIENT_NAME_LABELED_RE = re.compile(
    r"(\bClient\s+Name\s*[:=\-]\s*)([^,;\n|]+)",
    re.IGNORECASE,
)

# JSON keys whose entire string values represent sensitive fields
SENSITIVE_KEY_TOKENS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cagecode", "cage_code", "cage"), "[REDACTED_CAGE]"),
    (("ueicode", "uei_code", "samuei", "uniqueentityid", "uei"), "[REDACTED_UEI]"),
    (("primarynaics", "primary_naics", "primarynaicscode"), "[REDACTED_PRIMARY_NAICS]"),
    (("clientname", "client_name"), "[REDACTED_CLIENT_NAME]"),
    # backend/technical files often hold real credentials
    (("password", "passwd", "secret", "clientsecret", "apikey", "accesstoken", "refreshtoken",
      "privatekey", "connectionstring"), "[REDACTED_SECRET]"),
)


def _redaction_token_for_key(key: str) -> str | None:
    norm = re.sub(r"[^a-z0-9]", "", key.lower())
    for aliases, token in SENSITIVE_KEY_TOKENS:
        if norm in aliases:
            return token
    return None


def mask_pii(text: str) -> str:
    if not text:
        return ""
    text = URL_CREDENTIALS_RE.sub(r"\1[REDACTED_SECRET]\3", text)   # before EMAIL_RE (user:pass@host)
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = SSN_RE.sub("[REDACTED_SSN]", text)
    text = PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = CAGE_LABELED_RE.sub(r"\1[REDACTED_CAGE]", text)
    text = UEI_LABELED_RE.sub(r"\1[REDACTED_UEI]", text)
    text = PRIMARY_NAICS_LABELED_RE.sub(r"\1[REDACTED_PRIMARY_NAICS]", text)
    text = CLIENT_NAME_LABELED_RE.sub(r"\1[REDACTED_CLIENT_NAME]", text)
    return text


def sanitize_json_tree(obj: Any, parent_key: str = "") -> Any:
    """Strip control characters and mask PII in every string of a JSON tree."""
    if isinstance(obj, str):
        token = _redaction_token_for_key(parent_key)
        if token and obj.strip():
            return token
        return mask_pii(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", obj).strip())
    if isinstance(obj, list):
        return [sanitize_json_tree(item, parent_key=parent_key) for item in obj if item is not None]
    if isinstance(obj, dict):
        return {str(k): sanitize_json_tree(v, parent_key=str(k)) for k, v in obj.items() if v is not None}
    return obj


def load_and_check_json_file(file_path: Path) -> Any:
    """Load a JSON file; reject missing, empty, non-UTF-8 and malformed files BEFORE any API call."""
    if not file_path.exists():
        raise FileNotFoundError(f"Input JSON file does not exist: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"Input path is not a file: {file_path}")
    raw_bytes = file_path.read_bytes()
    if not raw_bytes.strip():
        raise ValueError(f"Input JSON file is empty: {file_path}")
    try:
        raw_text = raw_bytes.decode("utf-8-sig")  # utf-8-sig also accepts the BOM Windows tools add
    except UnicodeDecodeError as exc:
        raise ValueError(f"Input file is not valid UTF-8: {file_path} ({exc})") from exc
    if raw_text.strip() in ("{}", "[]", "null"):
        raise ValueError(f"Input JSON file has an empty payload: {file_path}")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON in {file_path} at line {exc.lineno}, col {exc.colno}: {exc.msg}") from exc
    if not isinstance(data, (dict, list)) or not data:
        raise ValueError(f"Root JSON in {file_path} must be a non-empty object or list.")
    return sanitize_json_tree(data)


def normalize_openapi_if_needed(raw_data: dict[str, Any], default_portal: str, default_api_source: str) -> dict[str, Any]:
    """Turn a Swagger/OpenAPI 3 document into the BackendApiBundle layout (other layouts pass through)."""
    if "endpoints" in raw_data:
        raw_data.setdefault("portal", default_portal)
        raw_data.setdefault("api_source", default_api_source)
        return raw_data
    if isinstance(raw_data.get("paths"), dict):
        info = raw_data.get("info", {})
        endpoints: list[dict[str, Any]] = []
        for path_str, methods_obj in raw_data["paths"].items():
            if not isinstance(methods_obj, dict):
                continue
            for http_method, spec in methods_obj.items():
                if http_method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or not isinstance(spec, dict):
                    continue
                params = []
                for p in spec.get("parameters", []):
                    if isinstance(p, dict) and p.get("name"):
                        params.append({
                            "name": p["name"],
                            "location": p.get("in", "query"),
                            "data_type": (p.get("schema") or {}).get("type", "string"),
                            "required": bool(p.get("required", False)),
                            "description": p.get("description", ""),
                        })
                endpoints.append({
                    "method": http_method.upper(),
                    "path": path_str,
                    "summary": spec.get("summary", ""),
                    "description": spec.get("description", ""),
                    "controller_or_tag": (spec.get("tags") or ["General"])[0],
                    "parameters": params,
                    "request_body_schema": spec.get("requestBody", {}),
                    "response_schema": spec.get("responses", {}),
                })
        return {
            "portal": raw_data.get("portal", default_portal),
            "api_source": raw_data.get("api_source", default_api_source),
            "service_name": info.get("title", default_api_source.upper()),
            "version": info.get("version", "v1.0"),
            "endpoints": endpoints,
        }
    return raw_data


class ScreenshotPublisher:
    """Finds a screenshot named in the JSON, copies it to static/img/<portal>/ and returns its site path."""

    def __init__(self, static_root: Path, portal: str, search_roots: list[Path]) -> None:
        self.static_root = static_root
        self.portal = portal
        self.search_roots = [r for r in search_roots if r]

    def _find_local(self, ref: str) -> Path | None:
        normalized = ref.replace("\\", "/")        # JSON from Windows tools contains backslashes
        name = PureWindowsPath(ref).name
        candidates = [Path(ref), Path(normalized)]
        for root in self.search_roots:
            candidates += [root / normalized, root / "screenshots" / name, root / name]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        if self.search_roots and self.search_roots[0].is_dir():   # only the input folder is searched deeply
            return next(self.search_roots[0].rglob(name), None)
        return None

    def publish(self, ref: Any) -> str | None:
        if isinstance(ref, dict):
            refs = [v for v in (ref.get("path"), ref.get("filename")) if isinstance(v, str) and v.strip()]
        elif isinstance(ref, str) and ref.strip():
            refs = [ref.strip()]
        else:
            refs = []
        for r in refs:
            local = self._find_local(r)
            if local:
                raw_name = PureWindowsPath(local.name).name
                parent_name = local.parent.name
                if (
                    local.stem.lower() in GENERIC_IMAGE_STEMS
                    and parent_name
                    and parent_name.lower() not in {"screenshots", "screenshot", "img", "images", "output", "knowledge"}
                ):
                    name = f"{slugify(parent_name)}-{raw_name}"
                else:
                    name = raw_name
                dest = self.static_root / "img" / self.portal / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local, dest)
                return f"/img/{self.portal}/{quote(name)}"
        for r in refs:
            if r.startswith(("/", "http://", "https://")):   # already a site path or URL
                return r
        if refs:
            logger.warning("Screenshot not found, page will have no image: %s", refs[0])
        return None


def _first_text(rec: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_route_path(raw_url: str) -> str:
    """Extracts a clean leading-slash path without trailing slash (except root '/')."""
    if not raw_url:
        return ""
    parsed = urlparse(raw_url.strip())
    path = parsed.path if parsed.scheme else raw_url.strip()
    path = path.split("?", 1)[0].split("#", 1)[0].strip()
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


def _resolve_doc_link_target(
    raw_href: str,
    current_route: str,
    known_route_to_slug: dict[str, str],
) -> str | None:
    """Resolves a crawled link STRICTLY to an internal Docusaurus documentation page (`./<slug>.mdx`).

    - Matches the link's path against `known_route_to_slug` (pages that have a real `.mdx` doc page).
    - NEVER returns raw portal `http(s)://...` URLs (so clicking a link in Docusaurus ALWAYS opens
      the corresponding documentation page, never the live portal website).
    - Drops self-links and any uncrawled/undocumented portal URLs so `docusaurus build` never fails.
    """
    href = (raw_href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None

    # Allow pre-resolved relative .mdx links if explicitly provided in JSON
    if href.startswith("./") and (href.endswith(".mdx") or href.endswith(".md")):
        return href

    route_path = _normalize_route_path(href)
    curr_norm = _normalize_route_path(current_route)

    if route_path == curr_norm:
        return None  # never link a page to itself

    if route_path in known_route_to_slug:
        return f"./{known_route_to_slug[route_path]}.mdx"

    # Drop raw portal URLs and undocumented routes completely
    return None


def _extract_links_from_record(
    rec: dict[str, Any],
    current_route: str,
    known_route_to_slug: dict[str, str],
) -> list[dict[str, str]]:
    """Collects ONLY verified internal Docusaurus doc links from `outgoingLinks`, `links`, and `sections`."""
    raw_candidates: list[tuple[str, str]] = []

    for key in ("outgoingLinks", "links"):
        items = rec.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                href = _first_text(item, "href", "url", "target", "path", "to")
                label = _first_text(item, "label", "text", "title", "name")
                if href:
                    raw_candidates.append((label or href, href))
            elif isinstance(item, str) and item.strip():
                raw_candidates.append((item.strip(), item.strip()))

    def _walk_sections(secs: Any) -> None:
        if not isinstance(secs, list):
            return
        for sec in secs:
            if not isinstance(sec, dict):
                continue
            for block in sec.get("content") or []:
                if isinstance(block, dict):
                    href = _first_text(block, "href", "url", "target")
                    label = _first_text(block, "text", "label", "title")
                    if href:
                        raw_candidates.append((label or href, href))
                    for sub in block.get("links") or []:
                        if isinstance(sub, dict):
                            s_href = _first_text(sub, "href", "url", "target")
                            s_lbl = _first_text(sub, "text", "label", "title")
                            if s_href:
                                raw_candidates.append((s_lbl or s_href, s_href))
            _walk_sections(sec.get("children"))

    _walk_sections(rec.get("sections"))

    resolved: list[dict[str, str]] = []
    seen_targets: set[str] = set()
    for label, href in raw_candidates:
        target = _resolve_doc_link_target(href, current_route, known_route_to_slug)
        if not target or target in seen_targets:
            continue
        seen_targets.add(target)
        clean_label = " ".join(label.split())[:100]
        if clean_label:
            resolved.append({"label": clean_label, "target": target})
    return resolved


def _block_texts(blocks: Any) -> list[str]:
    if isinstance(blocks, str):
        return [blocks.strip()] if blocks.strip() else []
    texts: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        text = block if isinstance(block, str) else (block.get("text") if isinstance(block, dict) else "")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


def _section_from_record(sec: Any, depth: int = 0) -> dict[str, Any] | None:
    if not isinstance(sec, dict) or depth > 8:
        return None
    paragraphs: list[str] = []
    items: list[str] = []
    for block in sec.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip():
            (items if block.get("type") == "list_item" else paragraphs).append(block["text"].strip())
        elif isinstance(block, str) and block.strip():
            paragraphs.append(block.strip())
    children = [c for c in (_section_from_record(x, depth + 1) for x in sec.get("children") or []) if c]
    heading = sec["heading"].strip() if isinstance(sec.get("heading"), str) else ""
    if not (heading or paragraphs or items or children):
        return None
    return {"heading": heading, "paragraphs": paragraphs, "list_items": items, "children": children}


def _strip_meta(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items() if k not in META_KEYS}
    if isinstance(obj, list):
        return [_strip_meta(v) for v in obj]
    return obj


def _discover_sibling_screenshots(source_dir: Path | None) -> list[str]:
    """Find image files sitting next to a page.json inside a page folder (e.g. 01-home/screenshot.png)."""
    if not source_dir or not source_dir.is_dir():
        return []
    if source_dir.name.lower() in {"knowledge", "output", "docs"}:
        return []
    imgs = sorted(
        (p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
        key=_natural_sort_key,
    )
    return [str(p) for p in imgs]


def page_from_crawl_record(
    rec: dict[str, Any],
    stem: str,
    publisher: ScreenshotPublisher,
    known_route_to_slug: dict[str, str] | None = None,
    source_dir: Path | None = None,
) -> dict[str, Any]:
    """Convert one crawler record (url / pageHeading|pageTitle|title / intro / sections / screenshot) to a page."""
    title = _first_text(rec, "pageHeading", "pageTitle", "title") or stem
    url = _first_text(rec, "url", "pageUrl", "canonicalUrl", "route")
    parsed = urlparse(url)
    route = parsed.path if parsed.scheme else url
    route = route or f"/{stem}"
    if not route.startswith("/"):
        route = "/" + route

    sections = [s for s in (_section_from_record(x) for x in rec.get("sections") or []) if s]
    summary = " ".join(_block_texts(rec.get("intro")) + _block_texts(rec.get("description")))
    links = _extract_links_from_record(rec, route, known_route_to_slug or {})

    page: dict[str, Any] = {
        "route": route,
        "title": title,
        "headings": list(dict.fromkeys(s["heading"] for s in sections if s["heading"])),
        "summary_text": summary,
        "sections": sections,
        "links": links,
    }
    for extra_key in ("breadcrumbs", "form_fields", "buttons", "table_headers", "workflow_steps"):
        if isinstance(rec.get(extra_key), list) and rec[extra_key]:
            page[extra_key] = rec[extra_key]

    if not sections and not summary and not page.get("form_fields") and not page.get("workflow_steps"):
        page["raw_content"] = _strip_meta(rec)
        logger.warning("Unrecognised page layout in %s: sending its raw JSON to the model.", stem)

    refs: list[Any] = []
    if rec.get("screenshot"):
        refs.append(rec["screenshot"])
    if isinstance(rec.get("screenshots"), list):
        refs += rec["screenshots"]
    if not refs:
        refs = _discover_sibling_screenshots(source_dir)
    page["screenshots"] = [p for p in (publisher.publish(r) for r in refs) if p]
    return page


def normalize_frontend_pages(
    raw: Any,
    stem: str,
    publisher: ScreenshotPublisher,
    known_route_to_slug: dict[str, str] | None = None,
    source_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Accept {"portal","pages":[...]} bundles, a list of pages, or ONE crawler record."""
    route_map = known_route_to_slug or {}
    if isinstance(raw, list):
        raw = {"pages": raw}
    if isinstance(raw.get("pages"), list):
        pages = []
        for i, item in enumerate(raw["pages"], start=1):
            if not isinstance(item, dict):
                continue
            if "route" in item or "summary_text" in item:      # already in page layout
                page = dict(item)
                refs = list(item.get("screenshots", []))
                if not refs and len(raw["pages"]) == 1:
                    refs = _discover_sibling_screenshots(source_dir)
                page["screenshots"] = [p for p in (publisher.publish(s) for s in refs) if p]
                page["links"] = _extract_links_from_record(item, page.get("route", f"/{stem}-{i}"), route_map)
                pages.append(page)
            else:
                pages.append(page_from_crawl_record(item, f"{stem}-{i}", publisher, route_map, source_dir))
        return pages
    if RECORD_HINT_KEYS & set(raw):
        return [page_from_crawl_record(raw, stem, publisher, route_map, source_dir)]
    raise ValueError(
        f"Unrecognised JSON layout in '{stem}'. Top-level keys: {sorted(raw)[:20]}. "
        "Run `python show_shape.py <file>` and share the output so the reader can be extended."
    )


def split_json_payload(data: Any, max_chars: int) -> list[Any]:
    """Split a big list/dict into parts of roughly max_chars so one page never exceeds the model's output."""
    if len(json.dumps(data, ensure_ascii=False)) <= max_chars:
        return [data]
    if isinstance(data, list):
        items: list[tuple[Any, Any]] = [(None, x) for x in data]
    elif isinstance(data, dict):
        items = list(data.items())
    else:
        return [data]
    chunks: list[list[tuple[Any, Any]]] = []
    current: list[tuple[Any, Any]] = []
    size = 0
    for key, value in items:
        n = len(json.dumps(value, ensure_ascii=False)) + (len(str(key)) if key is not None else 0)
        if current and size + n > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append((key, value))
        size += n
    if current:
        chunks.append(current)
    if isinstance(data, list):
        return [[v for _, v in c] for c in chunks]
    return [dict(c) for c in chunks]


# =====================================================================
# 4. POST-LLM MDX v3 SANITIZER + VALIDATOR
# =====================================================================
FENCE_SPLIT_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")
OUTER_MDX_FENCE_RE = re.compile(r"^```(?:mdx|markdown|md)?[ \t]*\r?\n([\s\S]*?)\r?\n```[ \t]*$", re.IGNORECASE)
FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)")
# /api/v1/sites/{siteId}  -> must live in inline code, bare braces crash MDX
BARE_PATH_RE = re.compile(r"(?<![`\w\\])(/[A-Za-z0-9_.\-/]*\{[A-Za-z0-9_.\-]+\}[A-Za-z0-9_.\-/{}]*)")
# a "<" that does NOT start one of the JSX tags we allow (Tabs, TabItem, details, summary, br)
BAD_LT_RE = re.compile(r"<(?!/?(?:Tabs|TabItem|details|summary)\b[^<>]*>|br\s*/?>)")
BRACE_RE = re.compile(r"(?<!\\)[{}]")


def _escape_text(text: str) -> str:
    text = BRACE_RE.sub(lambda m: "\\" + m.group(0), text)
    return BAD_LT_RE.sub("&lt;", text)


def _escape_prose(prose: str) -> str:
    """Escape { } < in prose; wrap bare API paths in backticks (and do not escape inside them)."""
    out: list[str] = []
    pos = 0
    for match in BARE_PATH_RE.finditer(prose):
        out.append(_escape_text(prose[pos:match.start()]))
        out.append(f"`{match.group(1)}`")
        pos = match.end()
    out.append(_escape_text(prose[pos:]))
    return "".join(out)


def yaml_quote(value: str) -> str:
    one_line = " ".join(value.split())
    return '"' + one_line.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sanitize_mdx_for_docusaurus_v3(
    raw_mdx: str, doc_id: str, title: str, position: int, with_frontmatter: bool = True
) -> str:
    """Make model output safe for Docusaurus v3: fixed front matter, escaped { } <, no stray fences."""
    cleaned = raw_mdx.strip()
    outer = OUTER_MDX_FENCE_RE.match(cleaned)
    if outer:
        cleaned = outer.group(1).strip()

    description = ""
    fm = FRONTMATTER_RE.match(cleaned)
    if fm:
        found = re.search(r"^description:\s*(.+)$", fm.group(1), re.MULTILINE)
        if found:
            description = found.group(1).strip().strip("\"'")
        body = cleaned[fm.end():]
    elif cleaned.startswith("---"):               # unclosed front matter: drop the opening line
        body = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
    else:
        body = cleaned

    parts = FENCE_SPLIT_RE.split(body)
    for idx in range(0, len(parts), 2):           # even parts are prose, odd parts are code
        parts[idx] = _escape_prose(parts[idx])
    body = "".join(parts).strip()

    if with_frontmatter:
        desc_val = description or f"Documentation for {title}."
        lines = [
            "---",
            f"id: {yaml_quote(doc_id)}",
            f"title: {yaml_quote(title)}",
            f"sidebar_position: {position}",
            f"description: {yaml_quote(desc_val)}",
            "---",
        ]
        return "\n".join(lines) + "\n\n" + body + "\n"
    if not re.match(r"#\s", body):
        body = f"# {_escape_text(title)}\n\n{body}"
    return body + "\n"


def validate_mdx_structure(mdx_text: str, require_frontmatter: bool = True) -> list[str]:
    """Deterministic checks on the final MDX string. Returns a list of problems (empty = OK)."""
    errors: list[str] = []
    body = mdx_text
    if require_frontmatter:
        fm = FRONTMATTER_RE.match(mdx_text)
        if not mdx_text.startswith("---\n") or not fm:
            errors.append("Missing or unclosed YAML front matter.")
        else:
            for key in ("id:", "title:"):
                if key not in fm.group(1):
                    errors.append(f"Front matter is missing '{key}'.")
            body = mdx_text[fm.end():]

    fences = len(re.findall(r"^\s*```", body, flags=re.MULTILINE))
    if fences % 2:
        errors.append(f"Unbalanced code fences (count={fences}).")
    admonitions = len(re.findall(r"^:::", body, flags=re.MULTILINE))
    if admonitions % 2:
        errors.append(f"Unbalanced ':::' admonition blocks (count={admonitions}).")

    for idx, prose in enumerate(FENCE_SPLIT_RE.split(body)):
        if idx % 2:
            continue
        if re.search(r"(?<!\\)[{}]", prose) or BAD_LT_RE.search(prose):
            errors.append("Unescaped '{', '}' or '<' outside code (would break the MDX build).")
            break
    return errors


# =====================================================================
# 5. PROMPTS + GEMINI COMPILER
# =====================================================================
FRONTEND_SYSTEM_PROMPT = """You are a Senior Technical Documentation Engineer specializing in Docusaurus v3 and MDX user guides.
Convert the JSON of ONE crawled frontend page into an accurate, professional, human-friendly Docusaurus MDX guide.

STRUCTURE (include only sections supported by the JSON — NEVER generate empty sections):
- Start with a YAML front matter block containing only: description: "<one short sentence>".
- Do NOT write a top-level "# " heading.
- ## Overview
  Explain what the page is used for, its purpose, and what the user can accomplish on the page.
- ## Screen Reference
  Include screenshots if `screenshots` is present, using each exact path unchanged:
  ![Page Screenshot](screenshot_path)
- ## Page Features
  Explain the sections and features in the JSON, preserving the original hierarchy (headings, paragraphs, list items, nested children). Drop duplicates and empty UI labels.
- ## Step-by-Step Workflow
  If `workflow_steps` exist, convert them into numbered instructions with exact button and field names in **bold**.
- ## Form Fields Reference
  If `form_fields` exist, create a Markdown table:
  | Field Label | Type | Required | Validation / Description |
  |---|---|---|---|
- ## Additional Information
  Include any other meaningful content from `raw_content` or the JSON without duplication.
- End with one relevant :::tip or :::note admonition with practical guidance.

INLINE DOCUMENTATION HYPERLINKS ONLY:
- Each entry in `links` points to another Docusaurus documentation page (`{"label": "...", "target": "./<slug>.mdx"}`).
- Weave these links INLINE directly inside body sentences or feature bullet descriptions where that feature or workflow is naturally mentioned — for example:
  * `- **[Market Intel](./market-intel.mdx)**: awards, solicitations, contracts, and agencies.`
  * `Start with [Signing in](./signing-in.mdx) and [finding your way around](./finding-your-way.mdx).`
- DO NOT create a separate "## Related pages", "## Links", or bare list of links at the bottom of the page.
- Only link a `target` from `links` when that topic is actually discussed in the page content.
- NEVER output external portal URLs (`https://...`) and NEVER invent any `.mdx` link not present in `links`.

STRICT CONTENT & MDX v3 SAFETY RULES:
1. Output ONLY raw MDX. Never wrap the entire output in Markdown code fences, and include no commentary outside the MDX.
2. Use ONLY information supplied in the JSON. Never invent buttons, fields, workflows, or numbers.
3. Omit any value that looks like [REDACTED_...]. Never expose emails, phone numbers, CAGE/UEI codes, Primary NAICS codes, or client/personal names.
4. Escape or place literal `{`, `}`, and `<` characters inside inline code or fenced code blocks. Balance all code fences and `:::` admonitions.
"""

MERMAID_AND_API_SAFETY_RULES = """
MERMAID FLOWCHART GENERATION RULES:
1. Every API documentation MDX page MUST contain an `## API Architecture and Flowchart` section with a fenced ```mermaid block starting with `flowchart TD`.
2. Prefer top-to-bottom flowcharts showing the actual sequence of API processing and the response flow back to the caller.
3. Use meaningful node labels (Frontend, Controller, Resource, Service, Repository, Database, External API, Response) and exact class/method/component names when supplied by the JSON.
4. Use database cylinder notation for database nodes: `DB[(Database)]`.
5. Use decision nodes only when the JSON identifies conditional logic, and show error paths when the JSON provides error-handling details.
6. If multiple endpoints share the same architecture, use one shared architecture diagram rather than repeating identical diagrams. Generate separate diagrams only when endpoints have genuinely different processing flows.
7. NEVER invent service classes, repositories, database tables, or dependencies. If internal architecture details are not in the JSON, generate a limited flowchart using only confirmed components and clearly note what internal details are unavailable.
8. Ensure every Mermaid diagram is syntactically valid for Docusaurus.

STRICT CONTENT & MDX v3 SAFETY RULES:
1. Output ONLY raw MDX. Never wrap the entire output in Markdown code fences, and include no commentary outside the MDX.
2. Start with a YAML front matter block containing only: description: "<one short sentence>". Do NOT write a top-level "# " heading.
3. Use ONLY information supplied in the JSON. Never invent endpoints, parameters, schemas, status codes, or internal steps. Do not include empty sections.
4. Put every endpoint path containing placeholders (e.g., `/api/v1/sites/{siteId}`) and every generic Java type (e.g., `ResponseEntity<List<UserDTO>>`) inside inline code with single backticks.
5. Omit any value that looks like [REDACTED_...]. Never expose personal names, emails, or phone numbers.
6. Balance all code fences and Docusaurus admonitions, and use valid Markdown tables.
"""

LIFERAY_API_SYSTEM_PROMPT = f"""You are a Senior Technical Documentation Engineer and API Architect specializing in Docusaurus v3, MDX, Liferay 7.4+ REST/OSGi/JAX-RS APIs, and Mermaid flowcharts.
Convert the provided Liferay API JSON (`api_source: "liferay-api"`) into a thorough Docusaurus MDX API reference page.

REQUIRED STRUCTURE (omit only subsections that have no data in the JSON):
- YAML front matter with only: description: "<one short sentence>"
- ## Service Overview
  Document portal, API source, service name, version, base path, authentication (if provided), and purpose of the service.
- ## API Endpoints Summary
  Markdown table with all endpoints supplied in the JSON:
  | Method | Endpoint | Description | Authentication |
  |---|---|---|---|
- ## API Architecture and Flowchart
  Generate a Mermaid `flowchart TD` explaining the Liferay API request lifecycle using ONLY components supported by the JSON (e.g., Frontend Application, HTTP Request, Liferay REST Resource / OSGi / JAX-RS component, Authentication & Authorization, Request Validation, Service / Business Logic, Database `[(Database)]` or External Service, Response Generation, HTTP Response). Do not assume a generic architecture if the JSON specifies concrete OSGi/JAX-RS/custom object components.
- ## Endpoint Details
  Create one `### METHOD /endpoint/path` (with path in backticks) subsection per endpoint including (when available):
  - Description, Purpose, Authentication, Roles or permissions
  - `#### Parameters` table (`| Name | Location | Type | Required | Description |`)
  - `#### Request Example` (fenced `json` block)
  - `#### Response Example` (fenced `json` block)
  - `#### Error Responses` (available HTTP status codes and error responses)
{MERMAID_AND_API_SAFETY_RULES}"""

SPRINGBOOT_API_SYSTEM_PROMPT = f"""You are a Senior Technical Documentation Engineer and API Architect specializing in Docusaurus v3, MDX, Spring Boot REST APIs, and Mermaid flowcharts.
Convert the provided Spring Boot API JSON (`api_source: "springboot-api"`) into a thorough Docusaurus MDX API reference page.

REQUIRED STRUCTURE (omit only subsections that have no data in the JSON):
- YAML front matter with only: description: "<one short sentence>"
- ## Service Overview
  Explain the purpose of the Spring Boot service, controller responsibilities, portal, API source, version, base path, and security configuration (when provided).
- ## API Endpoints Summary
  Markdown table with all endpoints supplied in the JSON:
  | Method | Endpoint | Description | Authentication |
  |---|---|---|---|
- ## API Architecture and Flowchart
  Generate a Mermaid `flowchart TD` illustrating the actual Spring Boot request lifecycle using ONLY components supported by the JSON (e.g., Frontend Application, HTTP Request, Spring Security, REST Controller, Request Validation, Service Layer, Business Logic, Repository Layer, Database `[(Database)]`, External Services, Response DTO, HTTP Response). Use exact controller, service, repository, and DTO names from the JSON when present.
- ## Endpoint Details
  Create one `### METHOD /endpoint/path` (with path in backticks) subsection per endpoint with:
  - `#### Description`
  - `#### Authentication and Authorization` (if provided)
  - `#### Request Parameters` table (`| Name | Location | Type | Required | Description |`)
  - `#### Request Body` (schema and fenced `json` example when available)
  - `#### Response` (response DTO, schema, and fenced `json` example when available)
  - `#### Error Responses` (available HTTP status codes and exception handling)
{MERMAID_AND_API_SAFETY_RULES}"""

BACKEND_API_SYSTEM_PROMPT = SPRINGBOOT_API_SYSTEM_PROMPT  # backward-compatible alias

TECHNICAL_BASE_PROMPT = """You are a Senior Technical Documentation Engineer specializing in Docusaurus v3 and MDX.
Convert the JSON of ONE backend technical artifact into an accurate, professional Docusaurus MDX reference page for developers and operators.
The JSON has this shape: {"category": ..., "source": <file name>, "title": ..., "content": <the data to document>}. Document the `content`.

STRUCTURE (include only sections supported by the JSON — NEVER generate empty sections):
- Start with a YAML front matter block containing only: description: "<one short sentence>".
- Do NOT write a top-level "# " heading.
- ## Overview
  What this artifact describes and why it matters.
- Then the category-specific sections below, using only data present in the JSON.
- ## Additional Information
  Any other meaningful content not covered above, without duplication.
- End with one relevant :::note or :::tip admonition.
If the content is a partial section of a larger file, document just what is present and do not mention missing parts.
"""

TECHNICAL_CATEGORY_FOCUS: dict[str, str] = {
    "architecture": """
CATEGORY FOCUS - ARCHITECTURE:
- ## Components: table of components/modules/services with responsibility and technology.
- ## Component Interactions: how components communicate (protocols, direction, data exchanged).
- ## Architecture Diagram: a Mermaid `flowchart TD` (or `flowchart LR`) using ONLY components in the JSON.
""",
    "database": """
CATEGORY FOCUS - DATABASE:
- ## Tables / Collections: one `###` subsection per table with a column table (| Column | Type | Nullable | Key | Description |), indexes and constraints when present.
- ## Relationships: foreign keys / references described in words.
- ## Entity Relationship Diagram: a Mermaid `erDiagram` using ONLY tables, columns and relations in the JSON.
""",
    "authentication": """
CATEGORY FOCUS - AUTHENTICATION:
- ## Authentication Flow: step-by-step description of login, token issue, refresh and logout when present.
- ## Roles and Permissions: table of roles and what they may access, when present.
- ## Authentication Sequence Diagram: a Mermaid `sequenceDiagram` using ONLY actors and steps in the JSON.
- NEVER show real tokens, keys, secrets or credentials; describe their purpose and format only.
""",
    "integrations": """
CATEGORY FOCUS - INTEGRATIONS:
- ## Integrated Systems: table of each external/internal system with purpose, protocol and direction.
- ## Integration Details: one `###` subsection per integration (endpoints, data exchanged, authentication method, error handling) when present.
- ## Integration Diagram: a Mermaid `flowchart LR` using ONLY systems in the JSON.
""",
    "infrastructure": """
CATEGORY FOCUS - INFRASTRUCTURE:
- ## Environments: table of environments (dev/test/prod etc.) when present.
- ## Services and Hosting: servers, containers, services, networking, storage, CI/CD as present in the JSON.
- ## Deployment Diagram: a Mermaid `flowchart TD` using ONLY components in the JSON.
- NEVER expose real hostnames, IP addresses, account IDs, passwords, keys or connection strings; use role names such as "Application Server".
""",
}

TECHNICAL_SAFETY_RULES = """
MERMAID RULES (diagram is optional: only include one if the JSON supplies enough confirmed components, never invent any):
1. Use fenced ```mermaid blocks. Put node labels that contain parentheses, slashes, colons or special characters inside double quotes, e.g. A["Auth Service (OAuth2)"].
2. Use `DB[(Database)]` for database nodes. Keep diagrams under about 25 nodes.

STRICT CONTENT & MDX v3 SAFETY RULES:
1. Output ONLY raw MDX. Never wrap the entire output in Markdown code fences, and include no commentary outside the MDX.
2. Use ONLY information supplied in the JSON. Never invent components, tables, columns, endpoints, hosts, versions or numbers.
3. Omit any value that looks like [REDACTED_...]. NEVER output passwords, secrets, tokens, API keys, private keys, connection strings, internal IP addresses or personal data.
4. Put every path containing placeholders (e.g., `/api/v1/sites/{siteId}`) and every generic type (e.g., `List<UserDTO>`) inside inline code with single backticks. Escape or place any other literal `{`, `}` and `<` inside code. Balance all code fences and `:::` admonitions, and use valid Markdown tables.
"""


def build_technical_prompt(category: str | None) -> str:
    focus = TECHNICAL_CATEGORY_FOCUS.get((category or "").lower(), "")
    return TECHNICAL_BASE_PROMPT + focus + TECHNICAL_SAFETY_RULES


def _prune_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _prune_empty(v) for k, v in value.items() if v not in ("", [], {}, None)}
    if isinstance(value, list):
        return [_prune_empty(v) for v in value]
    return value


class GeminiMdxCompiler:
    """JSON -> validated MDX with throttling, retries (429/5xx only) and one repair attempt."""

    def __init__(self, api_key: str | None = None, model_name: str = DEFAULT_MODEL, mock_mode: bool = False) -> None:
        self.model_name = model_name
        self.mock_mode = mock_mode
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.token_stats = {"total_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                            "total_tokens": 0, "failed_attempts": 0}
        self._last_call = 0.0
        self.client = None
        if not mock_mode:
            if genai is None:
                raise RuntimeError("google-genai is not installed. Run: pip install google-genai")
            if not self.api_key:
                raise ValueError("GEMINI_API_KEY is missing. In cmd run: set GEMINI_API_KEY=your-key")
            self.client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=int(LLM_TIMEOUT_SECONDS * 1000)),  # milliseconds
            )

    @property
    def mode_label(self) -> str:
        return "mock" if self.mock_mode else self.model_name

    def _throttle(self) -> None:
        wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _call_gemini_with_retry(self, system_instruction: str, user_prompt: str) -> str:
        last_err: Exception | None = None
        for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
            self._throttle()
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.1),
                )
                self.token_stats["total_calls"] += 1
                usage = getattr(response, "usage_metadata", None)
                if usage:
                    prompt = getattr(usage, "prompt_token_count", 0) or 0
                    output = (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
                    self.token_stats["prompt_tokens"] += prompt
                    self.token_stats["completion_tokens"] += output
                    self.token_stats["total_tokens"] += prompt + output
                text = (response.text or "").strip()
                if not text:
                    raise ValueError("Gemini returned an empty response")
                return text
            except Exception as exc:
                last_err = exc
                self.token_stats["failed_attempts"] += 1
                code = getattr(exc, "code", None)          # google.genai.errors.APIError has .code
                if code in (400, 401, 403, 404):
                    raise FatalLLMError(
                        f"Gemini rejected the request (HTTP {code}): {str(exc)[:300]}. "
                        f"Check GEMINI_API_KEY and the model name '{self.model_name}'."
                    ) from exc
                wait = 20 * attempt if code == 429 else 2 ** attempt
                logger.warning("Gemini attempt %d/%d failed (%s). %s",
                               attempt, MAX_LLM_ATTEMPTS, str(exc)[:200],
                               f"Retrying in {wait}s." if attempt < MAX_LLM_ATTEMPTS else "Giving up.")
                if attempt < MAX_LLM_ATTEMPTS:
                    time.sleep(wait)
        raise RuntimeError(f"Gemini failed {MAX_LLM_ATTEMPTS} times. Last error: {last_err}")

    def compile_to_validated_mdx(
        self, payload: dict[str, Any],
        doc_kind: Literal["frontend", "liferay_api", "springboot_api", "backend_api", "technical"],
        doc_id: str, title: str, position: int, with_frontmatter: bool = True,
    ) -> str:
        payload = _prune_empty(payload)
        if doc_kind == "frontend":
            system_prompt = FRONTEND_SYSTEM_PROMPT
        elif doc_kind == "technical":
            system_prompt = build_technical_prompt(payload.get("category"))
        elif doc_kind == "liferay_api" or payload.get("api_source") == "liferay-api":
            system_prompt = LIFERAY_API_SYSTEM_PROMPT
        else:
            system_prompt = SPRINGBOOT_API_SYSTEM_PROMPT
        if self.mock_mode:
            raw_mdx = build_mock_mdx(payload, doc_kind)
        else:
            user_prompt = (f"Page title: {title}\n\nJSON:\n{json.dumps(payload, indent=2, ensure_ascii=False)}")
            raw_mdx = self._call_gemini_with_retry(system_prompt, user_prompt)

        mdx = sanitize_mdx_for_docusaurus_v3(raw_mdx, doc_id, title, position, with_frontmatter)
        problems = validate_mdx_structure(mdx, with_frontmatter)
        if problems and not self.mock_mode:
            logger.warning("MDX check found %d problem(s) in %s: %s. Asking the model to repair it.",
                           len(problems), doc_id, problems)
            repair = (f"This Docusaurus v3 MDX has problems: {problems}.\n"
                      f"Return ONLY the corrected raw MDX:\n\n{mdx}")
            mdx = sanitize_mdx_for_docusaurus_v3(
                self._call_gemini_with_retry(system_prompt, repair), doc_id, title, position, with_frontmatter)
            problems = validate_mdx_structure(mdx, with_frontmatter)
        if problems:
            raise ValueError(f"Generated MDX for {doc_id} is still invalid after repair: {problems}")
        return mdx


def _mock_sections(sections: list[dict[str, Any]], depth: int, lines: list[str]) -> None:
    for sec in sections:
        if sec.get("heading"):
            lines += [f"{'#' * min(depth + 2, 6)} {sec['heading']}", ""]
        lines += [p for text in sec.get("paragraphs", []) for p in (text, "")]
        if sec.get("list_items"):
            lines += [f"- {item}" for item in sec["list_items"]] + [""]
        _mock_sections(sec.get("children", []), depth + 1, lines)


def build_mock_mdx(payload: dict[str, Any], doc_kind: str) -> str:
    """Offline stand-in for Gemini (used by --mock and --self-test): deterministic, no API call."""
    if doc_kind == "technical":
        category = payload.get("category", "technical")
        return "\n".join([
            "---", 'description: "Mock technical reference."', "---", "",
            "## Overview", "",
            f"Technical reference for **{category}** from `{payload.get('source', 'source.json')}`.", "",
            "## Source data", "",
            "```json", json.dumps(payload.get("content"), indent=2, ensure_ascii=False)[:3000], "```", "",
            ":::note", "Mock output: no model was called.", ":::",
        ])

    if doc_kind in ("backend_api", "liferay_api", "springboot_api"):
        is_liferay = doc_kind == "liferay_api" or payload.get("api_source") == "liferay-api"
        component = "Liferay REST Resource" if is_liferay else "Spring Boot REST Controller"
        ctrl = payload.get("controller_or_tag") or component
        lines = [
            "---", 'description: "Mock API reference."', "---", "",
            "## Service Overview", "",
            f"- **Portal**: `{payload.get('portal', 'admin')}`",
            f"- **API Source**: `{payload.get('api_source', 'api')}`",
            f"- **Service**: `{payload.get('service_name', 'Service')}`",
            f"- **Version**: `{payload.get('version', 'v1.0')}`",
            f"- **Base Path**: `{payload.get('base_path', '/')}`", "",
            "## API Endpoints Summary", "",
            "| Method | Endpoint | Description | Authentication |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for ep in payload.get("endpoints", []):
            auth = "Required" if ep.get("auth_required", True) else "Public"
            lines.append(f"| `{ep.get('method')}` | `{ep.get('path')}` | {ep.get('summary', '')} | {auth} |")
        lines += [
            "", "## API Architecture and Flowchart", "",
            "```mermaid",
            "flowchart TD",
            "    A[Frontend Application] --> B[HTTP Request]",
            f"    B --> C[{ctrl}]",
            "    C --> D[HTTP Response]",
            "    D --> A",
            "```", "",
            "## Endpoint Details", "",
        ]
        for ep in payload.get("endpoints", []):
            lines += [f"### {ep.get('method')} `{ep.get('path')}`", "", ep.get("description") or ep.get("summary") or "Endpoint.", ""]
            if ep.get("response_schema"):
                lines += ["#### Response Example", "", "```json", json.dumps(ep["response_schema"], indent=2), "```", ""]
        return "\n".join(lines)

    overview = payload.get("summary_text") or f"Guide for **{payload.get('title', 'this page')}**."
    if payload.get("links"):
        inline_refs = ", ".join(f"[{lnk['label']}]({lnk['target']})" for lnk in payload["links"])
        overview = f"{overview} See also {inline_refs}."

    lines = ["---", 'description: "Mock guide generated without an API call."', "---", "",
             "## Overview", "", overview, ""]
    _mock_sections(payload.get("sections", []), 1, lines)
    if payload.get("raw_content"):
        lines += ["## Source data", "", "```json", json.dumps(payload["raw_content"], indent=2)[:2000], "```", ""]
    if payload.get("workflow_steps"):
        lines += ["## Workflow", ""] + [f"{i}. {s}" for i, s in enumerate(payload["workflow_steps"], 1)] + [""]
    if payload.get("form_fields"):
        lines += ["## Form fields", "", "| Label | Type | Required |", "| :--- | :--- | :--- |"]
        lines += [f"| **{f['label']}** | `{f.get('field_type', 'text')}` | `{f.get('required', False)}` |" for f in payload["form_fields"]]
        lines.append("")
    for shot in payload.get("screenshots", []):
        lines += [f"![{payload.get('title', 'Screenshot')}]({shot})", ""]
    lines += [":::note", "Mock output: no model was called.", ":::"]
    return "\n".join(lines)


# =====================================================================
# 6. FILE HELPERS
# =====================================================================
def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-") or "index"


def atomic_write_text(target_path: Path, content: str) -> None:
    """Write via a temp file so a crash never leaves a half-written file in docs/."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=str(target_path.parent),
                                     delete=False, prefix=".tmp_", suffix=".tmp") as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, target_path)


def write_docusaurus_category_json(folder_path: Path, label: str, position: int, description: str) -> None:
    payload = {"label": label, "position": position,
               "link": {"type": "generated-index", "title": label, "description": description}}
    atomic_write_text(folder_path / "_category_.json", json.dumps(payload, indent=2) + "\n")


def compute_sha256(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _natural_sort_key(path: Path) -> list[Any]:
    """Sort paths in human/numeric order so 01-home < 02-dashboard < 10-settings."""
    parts = re.split(r"(\d+)", str(path).lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _stem_for_json(json_path: Path) -> str:
    """Derive a meaningful page stem; for 01-home/page.json use the parent folder name '01-home'."""
    stem = json_path.stem.removesuffix("-knowledge")
    parent = json_path.parent.name
    if stem.lower() in GENERIC_JSON_STEMS and parent and parent.lower() not in {"knowledge", "output", "docs"}:
        return parent
    return stem


IGNORED_DIR_NAMES = frozenset({"docs", "static", "node_modules", ".git", "__pycache__", ".venv", "venv", "build"})
IGNORED_JSON_NAMES = frozenset({"crawl-summary.json", "package.json", "package-lock.json", "tsconfig.json"})


def _is_ignored_json(path: Path, base: Path | None = None) -> bool:
    """True for bookkeeping JSON. Folder names are checked relative to `base` so a repo that happens to
    live under e.g. C:\\Users\\me\\docs\\ is not ignored as a whole."""
    name = path.name.lower()
    if name.startswith(".") or name == "_category_.json" or name.endswith("manifest.json") or name in IGNORED_JSON_NAMES:
        return True
    try:
        parts = path.relative_to(base).parts[:-1] if base else path.parts[:-1]
    except ValueError:
        parts = path.parts[:-1]
    return any(part.lower() in IGNORED_DIR_NAMES for part in parts)


def find_page_json_files(input_dir: Path) -> list[Path]:
    """Discover crawl JSON files in natural numeric order.

    Supports:
      1. Flat `knowledge/*.json` (or `output/knowledge/*.json`)
      2. Direct `*.json` inside `input_dir`
      3. Ordered page subfolders like `01-home/page.json`, `02-dashboard/page.json`, `10-settings/page.json`
    """
    for candidate in (input_dir / "knowledge", input_dir / "output" / "knowledge", input_dir):
        if candidate.is_dir():
            files = sorted(
                (p for p in candidate.glob("*.json") if p.is_file() and not _is_ignored_json(p, input_dir)),
                key=_natural_sort_key,
            )
            if files:
                return files
    if input_dir.is_dir():
        nested = sorted(
            (p for p in input_dir.rglob("*.json") if p.is_file() and not _is_ignored_json(p, input_dir)),
            key=_natural_sort_key,
        )
        if nested:
            return nested
    raise FileNotFoundError(f"No *.json files found in {input_dir} (or in its subfolders).")


def build_route_to_slug_map(json_files: list[Path], limit: int = 0, existing_guide_dir: Path | None = None) -> dict[str, str]:
    """Pre-scans crawl JSONs to map each documented page's route -> generated Docusaurus slug.

    When `--limit` is set, only includes pages that will be generated in this run OR already exist
    in `docs/<portal>/user-guide/*.mdx`, preventing broken links during partial test runs.
    """
    existing_slugs: set[str] = set()
    if existing_guide_dir and existing_guide_dir.is_dir():
        existing_slugs = {p.stem for p in existing_guide_dir.glob("*.mdx")}

    route_map: dict[str, str] = {}
    used_slugs: set[str] = set()
    planned_count = 0
    for jf in json_files:
        try:
            raw = load_and_check_json_file(jf)
        except Exception:
            continue
        stem = _stem_for_json(jf)
        items = raw if isinstance(raw, list) else (raw.get("pages") if isinstance(raw.get("pages"), list) else [raw])
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            if not (RECORD_HINT_KEYS & set(item) or "route" in item or "summary_text" in item):
                continue
            title = _first_text(item, "pageHeading", "pageTitle", "title") or f"{stem}-{idx}"
            if title.lower() in POISON_PAGE_TITLES:
                continue
            url = _first_text(item, "url", "pageUrl", "canonicalUrl", "route")
            route = _normalize_route_path(url) if url else f"/{stem}"
            slug = slugify(route if route != "/" else title)
            if slug in used_slugs:
                slug = f"{slug}-{len(used_slugs)}"
            used_slugs.add(slug)

            # Include in link map if no limit, within limit, or already built on disk
            if not limit or planned_count < limit or slug in existing_slugs:
                route_map[route] = slug
                planned_count += 1
    return route_map


# =====================================================================
# 7. PIPELINE
# =====================================================================
class DocumentationPipeline:
    def __init__(self, docs_root: Path, static_root: Path, manifest_path: Path, compiler: GeminiMdxCompiler,
                 force_rebuild: bool = False, with_frontmatter: bool = True, limit: int = 0) -> None:
        self.docs_root, self.static_root, self.manifest_path = docs_root, static_root, manifest_path
        self.compiler, self.force_rebuild = compiler, force_rebuild
        self.with_frontmatter, self.limit = with_frontmatter, limit
        self.manifest: dict[str, str] = self._load_manifest()
        self.generated_files: list[str] = []
        self.skipped_files: list[str] = []
        self.failures: list[str] = []
        self._positions: dict[str, int] = {}
        self._slugs: set[tuple[str, str]] = set()

    def _load_manifest(self) -> dict[str, str]:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                logger.warning("Could not read %s (%s). Starting with an empty manifest.", self.manifest_path, exc)
        return {}

    def save_manifest(self) -> None:
        atomic_write_text(self.manifest_path, json.dumps(self.manifest, indent=2, sort_keys=True) + "\n")

    def _limit_reached(self) -> bool:
        return bool(self.limit) and len(self.generated_files) >= self.limit

    def _write_if_changed(self, key: str, payload_for_hash: dict[str, Any], target: Path, **compile_args: Any) -> None:
        digest = compute_sha256({"payload": payload_for_hash, "mode": self.compiler.mode_label,
                                 "prompt": PROMPT_VERSION, "frontmatter": self.with_frontmatter})
        if not self.force_rebuild and self.manifest.get(key) == digest and target.exists():
            logger.info("Unchanged, skipped: %s", target)
            self.skipped_files.append(str(target))
            return
        mdx = self.compiler.compile_to_validated_mdx(with_frontmatter=self.with_frontmatter, **compile_args)
        atomic_write_text(target, mdx)
        self.manifest[key] = digest
        self.save_manifest()                      # saved after EVERY page, so a crash loses nothing
        self.generated_files.append(str(target))
        logger.info("Generated: %s", target)

    # ---------------------------------------------------------------- frontend
    def process_frontend_json(
        self,
        json_path: Path,
        portal: str | None = None,
        input_dir: Path | None = None,
        known_route_to_slug: dict[str, str] | None = None,
    ) -> None:
        logger.info("Reading crawl JSON: %s", json_path)
        raw = load_and_check_json_file(json_path)
        bundle_portal = raw.get("portal") if isinstance(raw, dict) else None
        portal = portal or bundle_portal or DEFAULT_FRONTEND_PORTAL
        if portal not in ALLOWED_PORTALS:
            raise ValueError(f"Portal must be one of {ALLOWED_PORTALS}, got '{portal}'")

        roots = [r for r in (json_path.parent, input_dir, json_path.parent.parent, Path.cwd()) if r]
        publisher = ScreenshotPublisher(self.static_root, portal, roots)
        stem = _stem_for_json(json_path)
        pages = normalize_frontend_pages(raw, stem, publisher, known_route_to_slug, source_dir=json_path.parent)

        guide_dir = self.docs_root / portal / "user-guide"
        write_docusaurus_category_json(guide_dir, "User Guide & Workflows", 1,
                                       f"Step-by-step guides for the {portal.upper()} portal.")
        for page_dict in pages:
            if self._limit_reached():
                logger.info("Limit of %d page(s) reached.", self.limit)
                return
            try:
                page = FrontendPageItem.model_validate(page_dict)
            except ValidationError as exc:
                self.failures.append(f"{json_path.name}: invalid page: {exc.errors()[:2]}")
                logger.error("Skipping invalid page in %s: %s", json_path.name, exc.errors()[:2])
                continue
            if page.is_error_page():
                logger.warning("Skipping error page '%s' (%s).", page.title, page.route)
                continue
            if not page.has_meaningful_content():
                logger.warning("Skipping empty page '%s' (%s): no text, headings or fields.", page.title, page.route)
                continue

            slug = slugify(page.route if page.route != "/" else page.title)
            if (portal, slug) in self._slugs:
                slug = f"{slug}-{len(self._slugs)}"
            self._slugs.add((portal, slug))
            self._positions[portal] = self._positions.get(portal, 0) + 1
            self._write_if_changed(
                key=f"frontend::{portal}::{slug}",
                payload_for_hash=page.model_dump(),
                target=guide_dir / f"{slug}.mdx",
                payload=page.model_dump(), doc_kind="frontend", doc_id=slug,
                title=page.title, position=self._positions[portal],
            )

    def process_frontend_folder(self, input_dir: Path, portal: str | None = None) -> None:
        files = find_page_json_files(input_dir)
        logger.info("Found %d JSON file(s) in %s", len(files), input_dir)
        target_portal = portal or DEFAULT_FRONTEND_PORTAL
        guide_dir = self.docs_root / target_portal / "user-guide"
        route_map = build_route_to_slug_map(files, limit=self.limit, existing_guide_dir=guide_dir)
        logger.info("Mapped %d internal doc route(s) for inline cross-linking.", len(route_map))
        for json_file in files:
            if self._limit_reached():
                break
            try:
                self.process_frontend_json(json_file, portal, input_dir, known_route_to_slug=route_map)
            except FatalLLMError:
                raise
            except Exception as exc:               # one bad file must not stop the others
                self.failures.append(f"{json_file.name}: {exc}")
                logger.error("FAILED %s: %s", json_file.name, str(exc).splitlines()[0][:300])

    # ---------------------------------------------------------------- backend api
    def process_backend_api_json(self, json_path: Path, api_source: str, portal: str | None = None) -> None:
        logger.info("Reading %s JSON: %s", api_source, json_path)
        raw = load_and_check_json_file(json_path)
        if not isinstance(raw, dict):
            raise ValueError(f"{json_path} must contain a JSON object.")
        portal = portal or DEFAULT_API_PORTAL
        data = normalize_openapi_if_needed(raw, portal, api_source)
        data["portal"] = portal if portal != DEFAULT_API_PORTAL or "portal" not in data else data["portal"]
        try:
            bundle = BackendApiBundle.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"API JSON schema validation failed for {json_path}:\n{exc}") from exc

        parent = self.docs_root / bundle.portal / "api-documentation"
        write_docusaurus_category_json(parent, "API Documentation", 2,
                                       "REST API specifications for Liferay and Spring Boot services.")
        is_liferay = bundle.api_source == "liferay-api"
        sub_dir = parent / bundle.api_source
        write_docusaurus_category_json(
            sub_dir, "Liferay API" if is_liferay else "Spring Boot API", 1 if is_liferay else 2,
            "Liferay headless and OSGi JAX-RS endpoints." if is_liferay else "Spring Boot controllers and endpoints.")

        grouped: dict[str, list[ApiEndpointItem]] = {}
        for ep in bundle.endpoints:
            grouped.setdefault(ep.controller_or_tag.strip() or "General", []).append(ep)

        position = 0
        for tag, endpoints in grouped.items():
            for start in range(0, len(endpoints), MAX_ENDPOINTS_PER_MDX):
                if self._limit_reached():
                    return
                position += 1
                part = start // MAX_ENDPOINTS_PER_MDX + 1
                multi = len(endpoints) > MAX_ENDPOINTS_PER_MDX
                slug = slugify(f"{tag}-part-{part}" if multi else tag)
                title = f"{tag} (Part {part})" if multi else tag
                payload = {"portal": bundle.portal, "api_source": bundle.api_source,
                           "service_name": bundle.service_name, "version": bundle.version,
                           "base_path": bundle.base_path, "controller_or_tag": title,
                           "endpoints": [e.model_dump() for e in endpoints[start:start + MAX_ENDPOINTS_PER_MDX]]}
                try:
                    api_doc_kind: Literal["liferay_api", "springboot_api"] = (
                        "liferay_api" if is_liferay else "springboot_api"
                    )
                    self._write_if_changed(
                        key=f"backend::{bundle.portal}::{bundle.api_source}::{slug}",
                        payload_for_hash=payload, target=sub_dir / f"{slug}.mdx",
                        payload=payload, doc_kind=api_doc_kind, doc_id=slug, title=title, position=position)
                except FatalLLMError:
                    raise
                except Exception as exc:
                    self.failures.append(f"{json_path.name}/{slug}: {exc}")
                    logger.error("FAILED %s: %s", slug, str(exc)[:300])

    # ---------------------------------------------------------------- backend technical
    def process_technical_json(self, json_path: Path, category: str, category_root: Path, category_dir: Path) -> None:
        """One backend/technical/<category>/**/*.json -> docs/technical/<category>/<slug>.mdx (split if huge)."""
        logger.info("Reading technical JSON (%s): %s", category, json_path)
        raw = load_and_check_json_file(json_path)

        parts = list(json_path.relative_to(category_root).with_suffix("").parts)
        parts[-1] = parts[-1].removesuffix("-knowledge")
        if len(parts) > 1 and parts[-1].lower() in GENERIC_JSON_STEMS:
            parts.pop()                              # database/orders/index.json -> "orders"
        stem = "-".join(parts)
        base_title = ((_first_text(raw, "title", "name", "pageTitle") if isinstance(raw, dict) else "")
                      or stem.replace("-", " ").replace("_", " ").title())

        chunks = split_json_payload(raw, TECH_CHUNK_CHARS)
        if len(chunks) > 1:
            logger.info("%s is large: splitting into %d parts.", json_path.name, len(chunks))
        scope = f"technical/{category}"
        for part, chunk in enumerate(chunks, start=1):
            if self._limit_reached():
                logger.info("Limit of %d page(s) reached.", self.limit)
                return
            multi = len(chunks) > 1
            slug = slugify(f"{stem}-part-{part}" if multi else stem)
            if (scope, slug) in self._slugs:
                slug = f"{slug}-{len(self._slugs)}"
            self._slugs.add((scope, slug))
            title = f"{base_title} (Part {part})" if multi else base_title
            self._positions[scope] = self._positions.get(scope, 0) + 1
            payload = {"category": category, "source": json_path.name, "title": title, "content": chunk}
            self._write_if_changed(
                key=f"technical::{category}::{slug}",
                payload_for_hash=payload, target=category_dir / f"{slug}.mdx",
                payload=payload, doc_kind="technical", doc_id=slug, title=title, position=self._positions[scope],
            )

    def process_technical_dir(self, tech_root: Path) -> bool:
        """backend/technical/ -> docs/technical/<category>/. Returns True when any JSON was found."""
        groups: list[tuple[str, Path, list[Path]]] = []
        root_files = sorted((p for p in tech_root.glob("*.json") if p.is_file() and not _is_ignored_json(p, tech_root)),
                            key=_natural_sort_key)
        if root_files:
            groups.append(("general", tech_root, root_files))
        category_dirs = sorted((p for p in tech_root.iterdir() if p.is_dir() and not p.name.startswith(".")),
                               key=lambda p: (TECHNICAL_ORDER.get(p.name.lower(), 99), p.name.lower()))
        for cdir in category_dirs:
            files = sorted((p for p in cdir.rglob("*.json") if p.is_file() and not _is_ignored_json(p, cdir)),
                           key=_natural_sort_key)
            if files:
                groups.append((cdir.name.lower(), cdir, files))
        if not groups:
            return False

        tech_dir = self.docs_root / "technical"
        write_docusaurus_category_json(tech_dir, "Technical Documentation", 3,
                                       "Architecture, database, authentication, integrations and infrastructure.")
        for category, cdir, files in groups:
            label, position, description = TECHNICAL_CATEGORIES.get(
                category, (category.replace("-", " ").replace("_", " ").title(), 99, f"Technical notes: {category}."))
            category_dir = tech_dir / slugify(category)
            write_docusaurus_category_json(category_dir, label, position, description)
            logger.info("Technical category '%s': %d JSON file(s)", category, len(files))
            for jf in files:
                if self._limit_reached():
                    return True
                try:
                    self.process_technical_json(jf, category, cdir, category_dir)
                except FatalLLMError:
                    raise
                except Exception as exc:
                    self.failures.append(f"{jf.name}: {exc}")
                    logger.error("FAILED %s: %s", jf.name, str(exc).splitlines()[0][:300])
        return True

    # ---------------------------------------------------------------- full project auto-discovery
    def process_project_dir(self, project_dir: Path, portal_filter: str | None = None,
                            sections: tuple[str, ...] = PROJECT_SECTIONS) -> None:
        """Auto-discover and compile the central-docs-hub layout in natural folder order:
            frontend/<portal>/**               -> docs/<portal>/user-guide/*.mdx
            backend/api/liferay/**             -> docs/<portal>/api-documentation/liferay-api/*.mdx
            backend/api/springboot/**          -> docs/<portal>/api-documentation/springboot-api/*.mdx
            backend/technical/<category>/**    -> docs/technical/<category>/*.mdx
        `sections` limits the run to any of: frontend, api, technical.
        """
        found_any = False

        # ---- frontend
        frontend_root = project_dir / "frontend"
        if "frontend" in sections and frontend_root.is_dir():
            portal_dirs = sorted((p for p in frontend_root.iterdir() if p.is_dir() and not p.name.startswith(".")),
                                 key=_natural_sort_key)
            for pdir in portal_dirs:
                pname = pdir.name.lower()
                if portal_filter and pname != portal_filter:
                    continue
                if pname not in ALLOWED_PORTALS:
                    logger.warning("Skipping unknown frontend portal folder '%s' (allowed: %s).", pname, ALLOWED_PORTALS)
                    continue
                found_any = True
                logger.info("Auto-discovered frontend portal: %s (%s)", pname, pdir)
                try:
                    self.process_frontend_folder(pdir, portal=pname)
                except FileNotFoundError:
                    logger.warning("No JSON files found in frontend portal folder: %s", pdir)

        # ---- backend api
        if "api" in sections:
            backend_candidates = (
                ("liferay-api", project_dir / "backend" / "api" / "liferay"),
                ("liferay-api", project_dir / "backend" / "liferay"),
                ("springboot-api", project_dir / "backend" / "api" / "springboot"),
                ("springboot-api", project_dir / "backend" / "springboot"),
            )
            seen_api_dirs: set[Path] = set()
            for api_source, api_dir in backend_candidates:
                resolved = api_dir.resolve()
                if not api_dir.is_dir() or resolved in seen_api_dirs:
                    continue
                seen_api_dirs.add(resolved)
                api_files = sorted(
                    (p for p in api_dir.rglob("*.json") if p.is_file() and not _is_ignored_json(p, api_dir)),
                    key=_natural_sort_key,
                )
                if not api_files:
                    continue
                found_any = True
                logger.info("Auto-discovered %d %s file(s) in %s", len(api_files), api_source, api_dir)
                for jf in api_files:
                    if self._limit_reached():
                        return
                    try:
                        self.process_backend_api_json(jf, api_source, portal_filter)
                    except FatalLLMError:
                        raise
                    except Exception as exc:
                        self.failures.append(f"{jf.name}: {exc}")
                        logger.error("FAILED %s: %s", jf.name, str(exc)[:300])

        # ---- backend technical
        tech_root = project_dir / "backend" / "technical"
        if "technical" in sections and tech_root.is_dir() and not self._limit_reached():
            if self.process_technical_dir(tech_root):
                found_any = True
            else:
                logger.warning("No JSON files found in %s", tech_root)

        if not found_any:
            raise FileNotFoundError(
                f"No JSON files found for {sections} in {project_dir} "
                "(expected frontend/<portal>/, backend/api/{liferay,springboot}/, backend/technical/<category>/)."
            )


# =====================================================================
# 8. SELF-TEST + CLI
# =====================================================================
def run_self_test() -> None:
    logger.info("Self-test (offline, no API call)...")
    # --- PII / secret masking
    assert "[REDACTED_EMAIL]" in mask_pii("mail a@b.com")
    assert "[REDACTED_PHONE]" in mask_pii("call +1 202-555-0143")
    assert "[REDACTED_SSN]" in mask_pii("ssn 123-45-6789")
    assert "[REDACTED_CAGE]" in mask_pii("CAGE Code: 1A2B3")
    assert "[REDACTED_UEI]" in mask_pii("UEI: J9K8L7M6N5P4")
    assert "[REDACTED_PRIMARY_NAICS]" in mask_pii("Primary NAICS: 541512")
    assert "[REDACTED_CLIENT_NAME]" in mask_pii("Client Name: Acme Federal LLC")
    conn = mask_pii("postgres://admin:s3cret@db.local:5432/app")
    assert "s3cret" not in conn and "[REDACTED_SECRET]" in conn and "db.local" in conn
    assert mask_pii("https://x.test/pricing") == "https://x.test/pricing"
    assert mask_pii("orderId 1234567890 ts 1731830400000") == "orderId 1234567890 ts 1731830400000"
    assert mask_pii("FY 2024 2025 2026") == "FY 2024 2025 2026"
    sanitized = sanitize_json_tree({
        "cageCode": "7Z9X1",
        "uei": "A1B2C3D4E5F6",
        "clientName": "Apex Defense Corp",
        "primaryNaics": "541511",
        "password": "hunter2",
        "api_key": "abc123",
    })
    assert sanitized["cageCode"] == "[REDACTED_CAGE]"
    assert sanitized["uei"] == "[REDACTED_UEI]"
    assert sanitized["clientName"] == "[REDACTED_CLIENT_NAME]"
    assert sanitized["primaryNaics"] == "[REDACTED_PRIMARY_NAICS]"
    assert sanitized["password"] == "[REDACTED_SECRET]" and sanitized["api_key"] == "[REDACTED_SECRET]"

    # --- JSON splitting for huge technical files
    big = [{"n": i, "pad": "x" * 100} for i in range(10)]
    parts = split_json_payload(big, 400)
    assert len(parts) > 1 and [x for part in parts for x in part] == big
    assert split_json_payload({"a": 1}, 400) == [{"a": 1}]
    big_dict = {f"t{i}": "y" * 100 for i in range(10)}
    dparts = split_json_payload(big_dict, 400)
    assert len(dparts) > 1 and {k: v for d in dparts for k, v in d.items()} == big_dict

    # --- ignore rules are relative to the base folder (repo may live under a folder called "docs")
    assert _is_ignored_json(Path("/x/frontend/crawl-summary.json"))
    assert not _is_ignored_json(Path("/home/docs/hub/frontend/admin/a.json"), Path("/home/docs/hub/frontend/admin"))
    assert _is_ignored_json(Path("/hub/frontend/admin/build/a.json"), Path("/hub/frontend/admin"))

    # --- MDX sanitizer
    def body(text: str) -> str:
        return sanitize_mdx_for_docusaurus_v3(text, "x", "X", 1).split("\n---\n", 1)[1].strip()
    assert body("Call /sites/{siteId}/pages now.") == "Call `/sites/{siteId}/pages` now."
    assert body("Use `/a/{id}` here") == "Use `/a/{id}` here"
    assert "<" not in body("Returns ResponseEntity<List<UserDTO>> ok").replace("&lt;", "")
    assert "<summaryDTO" not in body("Type <summaryDTO> used").replace("&lt;summaryDTO", "")
    assert body("<Tabs>\n<TabItem value=\"a\">x</TabItem>\n</Tabs>").startswith("<Tabs>")
    assert body("```json\n{\"a\": 1}\n```").startswith("```json\n{")
    assert not validate_mdx_structure(sanitize_mdx_for_docusaurus_v3("```mdx\n---\ndescription: hi\n---\n## A {b}\n```", "x", "X", 1))

    # --- prompts
    assert "erDiagram" in build_technical_prompt("database")
    assert "sequenceDiagram" in build_technical_prompt("authentication")
    assert "CATEGORY FOCUS" not in build_technical_prompt("unknown-category")

    with tempfile.TemporaryDirectory(prefix="mdx_self_test_") as tmp:
        root = Path(tmp)
        (root / "out" / "knowledge").mkdir(parents=True)
        (root / "out" / "screenshots").mkdir()
        for name in ("a.png", "b.png"):
            (root / "out" / "screenshots" / name).write_bytes(b"\x89PNG fake")
        # crawl records: a-knowledge links to Home (documented -> ./home.mdx) and uncrawled portal URLs (dropped)
        records = {
            "a-knowledge.json": {
                "url": "https://x.test/data/a", "title": "A", "pageHeading": "Agencies",
                "intro": [{"type": "paragraph", "text": "Explore agencies. CAGE: 1A2B3"}],
                "sections": [{"heading": "Filters", "level": "h2", "content": [
                    {"type": "list_item", "text": "Keyword"}, {"type": "paragraph", "text": "Use {braces} <here>."}],
                    "children": []}],
                "outgoingLinks": [
                    {"label": "Home Dashboard", "href": "https://x.test/"},
                    {"label": "Pricing", "href": "https://x.test/pricing"},
                    {"label": "Broken Uncrawled", "href": "/does-not-exist"},
                ],
                "screenshot": {"filename": "a.png", "path": "output/screenshots/a.png"},
            },
            "page.json": {
                "pageTitle": "Home", "pageUrl": "https://x.test/", "hero": "Welcome to GovCon",
                "screenshot": "C:\\Users\\pc\\Desktop\\md-gen\\output\\screenshots\\b.png",
            },
            "bad.json": {"nothing": "useful"},
        }
        for name, rec in records.items():
            (root / "out" / "knowledge" / name).write_text(json.dumps(rec), encoding="utf-8")
        (root / "out" / "knowledge" / "empty.json").write_text("", encoding="utf-8")

        pipe = DocumentationPipeline(root / "docs", root / "static", root / "manifest.json",
                                     GeminiMdxCompiler(mock_mode=True))
        pipe.process_frontend_folder(root / "out", "govcon360")
        pipe.save_manifest()
        guide = root / "docs" / "govcon360" / "user-guide"
        for expected in ("data-a.mdx", "home.mdx", "_category_.json"):
            assert (guide / expected).exists(), f"missing {expected}"
        assert (root / "static" / "img" / "govcon360" / "a.png").exists()
        assert (root / "static" / "img" / "govcon360" / "b.png").exists()
        assert len(pipe.failures) == 2, pipe.failures

        text = (guide / "data-a.mdx").read_text(encoding="utf-8")
        assert "\\{braces\\}" in text and "&lt;here>" in text and "/img/govcon360/a.png" in text
        assert "[REDACTED_CAGE]" in text and "1A2B3" not in text
        assert "[Home Dashboard](./home.mdx)" in text                       # internal doc link woven inline
        assert "https://x.test/pricing" not in text                         # uncrawled live portal URL dropped
        assert "## Related pages" not in text                               # no bottom link-dump section
        assert not validate_mdx_structure(text)

        again = DocumentationPipeline(root / "docs", root / "static", root / "manifest.json",
                                      GeminiMdxCompiler(mock_mode=True))
        again.process_frontend_folder(root / "out", "govcon360")
        assert not again.generated_files and len(again.skipped_files) == 2

        api = {"portal": "admin", "api_source": "springboot-api", "service_name": "Svc", "version": "v1",
               "base_path": "/api", "endpoints": [{"method": "post", "path": "/a/{id}/approve",
                                                   "summary": "Approve", "controller_or_tag": "AccountController",
                                                   "description": "Returns ResponseEntity<Dto>."}]}
        (root / "api.json").write_text(json.dumps(api), encoding="utf-8")
        pipe.process_backend_api_json(root / "api.json", "springboot-api", "admin")
        sb_mdx = root / "docs" / "admin" / "api-documentation" / "springboot-api" / "accountcontroller.mdx"
        assert sb_mdx.exists()
        assert "```mermaid" in sb_mdx.read_text(encoding="utf-8")

        # --- central-docs-hub layout: frontend/<portal>/NN-page/page.json + backend/api + backend/technical
        hub = root / "central-docs-hub"
        for folder, route, title in (
            ("01-home", "/", "Home"),
            ("02-dashboard", "/dashboard", "Dashboard"),
            ("10-settings", "/settings", "Settings"),
        ):
            fdir = hub / "frontend" / "public" / folder
            fdir.mkdir(parents=True)
            (fdir / "page.json").write_text(json.dumps({
                "url": f"https://x.test{route}", "title": title,
                "intro": [{"type": "paragraph", "text": f"Welcome to {title} screen."}],
            }), encoding="utf-8")
            (fdir / "screenshot.png").write_bytes(b"\x89PNG " + folder.encode("utf-8"))
        (hub / "frontend" / "crawl-summary.json").write_text(json.dumps({"pages": 3}), encoding="utf-8")
        lf_dir = hub / "backend" / "api" / "liferay"
        lf_dir.mkdir(parents=True)
        (lf_dir / "liferay.json").write_text(json.dumps({
            "portal": "admin", "api_source": "liferay-api", "service_name": "Liferay Headless",
            "version": "v1.0", "base_path": "/o/headless",
            "endpoints": [{"method": "get", "path": "/sites/{siteId}", "summary": "List sites", "controller_or_tag": "SiteResource"}],
        }), encoding="utf-8")
        db_dir = hub / "backend" / "technical" / "database"
        db_dir.mkdir(parents=True)
        (db_dir / "schema.json").write_text(json.dumps({
            "title": "Primary Schema",
            "tables": [{"name": "users", "columns": ["id", "email_verified"]}],
            "password": "hunter2",
            "url": "postgres://admin:s3cret@db.local:5432/app",
        }), encoding="utf-8")
        arch_dir = hub / "backend" / "technical" / "architecture" / "overview"
        arch_dir.mkdir(parents=True)
        (arch_dir / "index.json").write_text(json.dumps({"components": ["Web", "API", "DB"]}), encoding="utf-8")

        hub_pipe = DocumentationPipeline(root / "hub_docs", root / "hub_static", root / "hub_manifest.json",
                                         GeminiMdxCompiler(mock_mode=True))
        hub_pipe.process_project_dir(hub)
        assert not hub_pipe.failures, hub_pipe.failures
        g = root / "hub_docs" / "public" / "user-guide"
        assert "sidebar_position: 1" in (g / "home.mdx").read_text(encoding="utf-8")
        assert "sidebar_position: 2" in (g / "dashboard.mdx").read_text(encoding="utf-8")
        assert "sidebar_position: 3" in (g / "settings.mdx").read_text(encoding="utf-8")
        assert (root / "hub_static" / "img" / "public" / "01-home-screenshot.png").exists()
        lf_mdx = root / "hub_docs" / "admin" / "api-documentation" / "liferay-api" / "siteresource.mdx"
        assert lf_mdx.exists() and "```mermaid" in lf_mdx.read_text(encoding="utf-8")
        tech = root / "hub_docs" / "technical"
        assert (tech / "_category_.json").exists() and (tech / "database" / "_category_.json").exists()
        schema_text = (tech / "database" / "schema.mdx").read_text(encoding="utf-8")
        assert 'title: "Primary Schema"' in schema_text
        assert "hunter2" not in schema_text and "s3cret" not in schema_text and "[REDACTED_SECRET]" in schema_text
        assert (tech / "architecture" / "overview.mdx").exists()       # index.json -> folder name
        assert not validate_mdx_structure(schema_text)

        only = DocumentationPipeline(root / "only_docs", root / "only_static", root / "only_manifest.json",
                                     GeminiMdxCompiler(mock_mode=True))
        only.process_project_dir(hub, sections=("technical",))
        assert (root / "only_docs" / "technical" / "database" / "schema.mdx").exists()
        assert not (root / "only_docs" / "public").exists()
    logger.info("[PASS] self-test OK")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compile crawl / API / technical JSON into Docusaurus v3 MDX with Gemini.")
    p.add_argument("--project-dir", type=Path,
                   help=f"project root with frontend/, backend/api/, backend/technical/ (default: {PROJECT_ROOT})")
    p.add_argument("--only", choices=PROJECT_SECTIONS, action="append",
                   help="limit a project run to frontend, api or technical (repeatable; default: all)")
    p.add_argument("--input-dir", type=Path, help="folder with knowledge/*.json, numbered page folders (01-home/), or a project root")
    p.add_argument("--frontend-json", type=Path, action="append", default=[], help="a crawl JSON file (repeatable)")
    p.add_argument("--liferay-api-json", type=Path, action="append", default=[])
    p.add_argument("--springboot-api-json", type=Path, action="append", default=[])
    p.add_argument("--portal", choices=ALLOWED_PORTALS, help="default: folder name for frontend/<portal>, govcon360 for loose crawls, admin for API files")
    p.add_argument("--docs-root", type=Path, default=DEFAULT_DOCS_ROOT, help=f"default: {DEFAULT_DOCS_ROOT}")
    p.add_argument("--static-root", type=Path, help="Docusaurus static folder (default: <docs-root>/../static)")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help=f"default: {DEFAULT_MANIFEST}")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    p.add_argument("--limit", type=int, default=0, help="generate at most N pages (use 1 for a first test)")
    p.add_argument("--no-frontmatter", action="store_true", help="write no id/title/sidebar front matter (adds a # heading)")
    p.add_argument("--force", action="store_true", help="regenerate even if the input did not change")
    p.add_argument("--mock", action="store_true", help="no API call: deterministic placeholder pages")
    p.add_argument("--self-test", action="store_true", help="run the offline checks and exit")
    args = p.parse_args(argv)

    if args.self_test:
        run_self_test()
        return 0

    sections = tuple(args.only) if args.only else PROJECT_SECTIONS

    if not (args.project_dir or args.input_dir or args.frontend_json or args.liferay_api_json or args.springboot_api_json):
        detected = False
        search_bases: list[Path] = []
        for base in (PROJECT_ROOT, Path.cwd(), SCRIPT_DIR):
            if base not in search_bases:
                search_bases.append(base)
        for base in search_bases:
            if (base / "frontend").is_dir() or (base / "backend").is_dir():
                args.project_dir = base
                logger.info("Auto-detected project root: %s", base)
                detected = True
                break
            if (base / "Project" / "frontend").is_dir() or (base / "Project" / "backend").is_dir():
                args.project_dir = base / "Project"
                logger.info("Auto-detected Project/ folder in %s", args.project_dir)
                detected = True
                break
            if (base / "output").is_dir():
                try:
                    find_page_json_files(base / "output")
                    args.input_dir = base / "output"
                    logger.info("Auto-detected output/ folder in %s", args.input_dir)
                    detected = True
                    break
                except FileNotFoundError:
                    pass
            try:
                find_page_json_files(base)
                args.input_dir = base
                logger.info("Auto-detected crawl JSON files in %s", base)
                detected = True
                break
            except FileNotFoundError:
                pass
        if not detected:
            p.error(
                "No project found. Run from central-docs-hub/scripts (frontend/ and backend/ one level up), "
                "or pass --project-dir / --input-dir / --self-test."
            )

    try:
        compiler = GeminiMdxCompiler(model_name=args.model, mock_mode=args.mock)
    except (RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 2
    pipeline = DocumentationPipeline(
        docs_root=args.docs_root, static_root=args.static_root or args.docs_root.parent / "static",
        manifest_path=args.manifest, compiler=compiler, force_rebuild=args.force,
        with_frontmatter=not args.no_frontmatter, limit=args.limit)

    logger.info("Model: %s | docs: %s | static: %s", compiler.mode_label, pipeline.docs_root, pipeline.static_root)
    try:
        if args.project_dir:
            pipeline.process_project_dir(args.project_dir, args.portal, sections)
        if args.input_dir:
            if (args.input_dir / "frontend").is_dir() or (args.input_dir / "backend").is_dir():
                pipeline.process_project_dir(args.input_dir, args.portal, sections)
            else:
                pipeline.process_frontend_folder(args.input_dir, args.portal)
        for f in args.frontend_json:
            try:
                pipeline.process_frontend_json(f, args.portal)
            except FatalLLMError:
                raise
            except Exception as exc:
                pipeline.failures.append(f"{f.name}: {exc}")
                logger.error("FAILED %s: %s", f.name, str(exc).splitlines()[0][:300])
        for source, files in (("liferay-api", args.liferay_api_json), ("springboot-api", args.springboot_api_json)):
            for f in files:
                try:
                    pipeline.process_backend_api_json(f, source, args.portal)
                except FatalLLMError:
                    raise
                except Exception as exc:
                    pipeline.failures.append(f"{f.name}: {exc}")
                    logger.error("FAILED %s: %s", f.name, str(exc).splitlines()[0][:300])
    except FatalLLMError as exc:
        logger.error("STOPPED: %s", exc)
        pipeline.save_manifest()
        return 2

    pipeline.save_manifest()
    logger.info("Done | generated: %d | unchanged: %d | failed: %d | tokens: %s",
                len(pipeline.generated_files), len(pipeline.skipped_files), len(pipeline.failures), compiler.token_stats)
    for failure in pipeline.failures:
        logger.warning("  failed: %s", failure[:300])
    return 1 if pipeline.failures else 0


if __name__ == "__main__":
    sys.exit(main())
