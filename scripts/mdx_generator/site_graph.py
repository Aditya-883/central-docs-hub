from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import CATEGORY_ORDER, POISON_PAGE_TITLES
from .preprocessor import clean_page_title, load_and_check_json_file, slugify


@dataclass
class PlannedDoc:
    json_path: Path
    route: str
    title: str
    category_slug: str
    category_label: str
    doc_slug: str
    rel_doc_path: str  # e.g. "market-intel/awards.mdx"


def normalize_route_path(raw_url: str) -> str:
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


def infer_category(route: str, title: str, nav_route_to_cat: dict[str, str]) -> tuple[str, str]:
    """Assign a page to its module category using navigation tree first, then route/title heuristics."""
    norm = normalize_route_path(route)
    low_title = title.lower()

    if norm in ("/", "/login", "/sign-in", "/forgot-password", "/reset-password", "/dashboard", "/downloads"):
        return "getting-started", CATEGORY_ORDER["getting-started"][0]
    if low_title in ("signing in", "dashboard", "finding your way around"):
        return "getting-started", CATEGORY_ORDER["getting-started"][0]

    if norm in nav_route_to_cat:
        cat_slug = nav_route_to_cat[norm]
        label = CATEGORY_ORDER.get(cat_slug, (cat_slug.replace("-", " ").title(), 50))[0]
        return cat_slug, label

    if norm.startswith("/data/") or any(k in norm for k in ("award", "solicitation", "vendor", "agenc", "people")):
        return "market-intel", CATEGORY_ORDER["market-intel"][0]
    if any(k in norm for k in ("lead", "recompete", "procurement-notice", "capture")):
        return "lead-prospector", CATEGORY_ORDER["lead-prospector"][0]
    if any(k in norm for k in ("opportunit", "account", "contact")):
        return "business-development", CATEGORY_ORDER["business-development"][0]
    if any(k in norm for k in ("partner", "teaming", "deal")):
        return "teaming-partnerships", CATEGORY_ORDER["teaming-partnerships"][0]
    if any(k in norm for k in ("proposal", "rfp")):
        return "proposals", CATEGORY_ORDER["proposals"][0]
    if any(k in norm for k in ("contract", "clause", "wage")):
        return "contracts", CATEGORY_ORDER["contracts"][0]
    if any(k in norm for k in ("user", "admin", "notification", "company-information", "saved-filter", "analytics")):
        return "administration", CATEGORY_ORDER["administration"][0]
    if any(k in norm for k in ("glossary", "naics", "psc", "sin", "sic", "terminology")):
        return "reference", CATEGORY_ORDER["reference"][0]

    return "general", CATEGORY_ORDER["general"][0]


def _extract_nav_categories(raw: dict[str, Any]) -> dict[str, str]:
    """Read dropdown-toggle -> dropdown-item pairs from crawled navigation arrays."""
    mapping: dict[str, str] = {}
    for nav in raw.get("navigation") or []:
        if not isinstance(nav, dict):
            continue
        current_cat = ""
        for lnk in nav.get("links") or []:
            if not isinstance(lnk, dict):
                continue
            cls = (lnk.get("className") or "").lower()
            text = (lnk.get("text") or "").strip()
            href = normalize_route_path(lnk.get("href") or "")
            if "dropdown-toggle" in cls and text:
                current_cat = slugify(text)
            elif "dropdown-item" in cls and current_cat and href and href != "/":
                mapping[href] = current_cat
    return mapping


def build_site_graph(json_files: list[Path]) -> tuple[list[PlannedDoc], dict[str, str]]:
    """Pass 1: Deduplicate crawled pages and build route -> relative doc path registry."""
    nav_route_to_cat: dict[str, str] = {}
    raw_records: list[tuple[Path, dict[str, Any]]] = []

    for jf in json_files:
        try:
            raw = load_and_check_json_file(jf)
        except Exception:
            continue
        if isinstance(raw, dict):
            nav_route_to_cat.update(_extract_nav_categories(raw))
            raw_records.append((jf, raw))

    planned: list[PlannedDoc] = []
    seen_dedupe_keys: set[tuple[str, str]] = set()
    used_rel_paths: set[str] = set()
    route_to_rel_path: dict[str, str] = {}

    for jf, rec in raw_records:
        raw_title = rec.get("pageHeading") or rec.get("pageTitle") or rec.get("title") or jf.stem
        title = clean_page_title(str(raw_title), jf.parent.name)
        if title.lower() in POISON_PAGE_TITLES:
            continue

        url = rec.get("pageUrl") or rec.get("url") or rec.get("route") or f"/{jf.parent.name}"
        route = normalize_route_path(str(url))

        # Deduplicate identical pages (e.g., "/" and "/login" both yielding "Signing in")
        dedupe_key = (route, title.lower())
        if dedupe_key in seen_dedupe_keys:
            continue
        seen_dedupe_keys.add(dedupe_key)

        cat_slug, cat_label = infer_category(route, title, nav_route_to_cat)
        doc_slug = slugify(title if route == "/" else route.split("/")[-1])
        rel_path = f"{cat_slug}/{doc_slug}.mdx"

        if rel_path in used_rel_paths:
            continue  # Skip exact duplicate screen

        used_rel_paths.add(rel_path)
        route_to_rel_path[route] = rel_path
        planned.append(PlannedDoc(jf, route, title, cat_slug, cat_label, doc_slug, rel_path))

    return planned, route_to_rel_path


def compute_relative_doc_link(from_rel_path: str, to_rel_path: str) -> str:
    """Compute a Docusaurus-valid relative link between two .mdx files in different category folders."""
    from_dir = os.path.dirname(from_rel_path)
    rel = os.path.relpath(to_rel_path, start=from_dir).replace("\\", "/")
    if not rel.startswith("."):
        rel = "./" + rel
    return rel


def build_welcome_mdx(portal_label: str, planned_docs: list[PlannedDoc]) -> str:
    """Auto-generate a clean Welcome landing page (matching Image 1) with inline links."""
    by_cat: dict[str, list[PlannedDoc]] = {}
    for doc in planned_docs:
        by_cat.setdefault(doc.category_label, []).append(doc)

    lines = [
        "---",
        'id: "welcome"',
        f'title: "Welcome to {portal_label}"',
        'sidebar_label: "Welcome"',
        "sidebar_position: 0",
        'slug: "/"',
        f'description: "Documentation home for {portal_label}."',
        "---",
        "",
        f"{portal_label} brings federal market data, capture workflows, and team collaboration into one portal.",
        "",
        "## Start here",
        "",
    ]

    getting_started = by_cat.get("Getting started", [])
    if getting_started:
        for doc in getting_started[:5]:
            lines.append(f"- [{doc.title}](./{doc.rel_doc_path})")
    else:
        for doc in planned_docs[:3]:
            lines.append(f"- [{doc.title}](./{doc.rel_doc_path})")

    lines += ["", "## Sections", ""]
    for cat_label, docs in by_cat.items():
        if cat_label == "Getting started":
            continue
        sample_links = ", ".join(f"[{d.title}](./{d.rel_doc_path})" for d in docs[:4])
        lines.append(f"- **{cat_label}**: {sample_links}")

    lines.append("")
    return "\n".join(lines)