from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote

from .chunker import split_payload
from .compiler import GeminiMdxCompiler
from .config import (
    ALLOWED_PORTALS,
    CATEGORY_ORDER,
    CHUNK_TOKEN_BUDGET,
    DEFAULT_API_PORTAL,
    DEFAULT_FRONTEND_PORTAL,
    FatalLLMError,
    GENERIC_IMAGE_STEMS,
    IGNORED_DIR_NAMES,
    IMAGE_EXTENSIONS,
    MAX_ENDPOINTS_PER_MDX,
    MAX_LIFERAY_API_FILES,
    OCR_CONCURRENCY,
    PORTAL_DISPLAY_NAMES,
    PORTAL_SIDEBAR_POSITIONS,
    PROMPT_VERSION,
    REDACT_PERSON_NAMES,
    REDACT_SCREENSHOTS,
    REDACT_TERMS,
    TECHNICAL_CATEGORIES,
    logger,
)
from .preprocessor import (
    compact_technical_json,
    extract_structured_frontend_fields,
    load_and_check_json_file,
    slugify,
)
from .redactor import collect_pii_terms, redact_image
from .schemas import BackendApiBundle, FrontendPageItem, TechnicalDocItem
from .site_graph import (
    build_site_graph,
    build_welcome_mdx,
    compute_relative_doc_link,
    normalize_route_path,
)

# Output folder mapping under docusaurus/docs/
PORTAL_OUTPUT_DIRS: dict[str, tuple[str, str, str]] = {
    # portal_key -> (root_dir_name, user_guide_subdir, user_guide_label)
    "client": ("client-documentation", "user-guide", "User Guide"),
    "asponte": ("client-documentation", "user-guide", "User Guide"),
    "public": ("govcon360-system-documentation", "user-guide", "User Guide"),
    "govcon360": ("govcon360-system-documentation", "user-guide", "User Guide"),
    "admin": ("admin-documentation", "admin-user-guide", "Admin User Guide"),
}

TECHNICAL_CATEGORY_META: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "architecture": (
        "Architecture",
        "OSGi modules, client extensions, and system architecture overview.",
        (),
    ),
    "database": (
        "Database",
        "Liferay custom objects, schemas, datalake, and data layer.",
        ("datalake", "object", "batch", "upgrade", "data-headless", "grid", "export", "schema", "database", "db"),
    ),
    "authentication": (
        "Authentication",
        "Keycloak, SSO, login, session, and user management flows.",
        ("keycloak", "login", "sso", "auth", "oauth", "account-creation", "user-management", "user-invite", "turnstile", "security"),
    ),
    "integrations": (
        "Integrations",
        "SAM.gov, business development, billing/commerce, and external services.",
        ("sam", "business-development", "lead", "recompete", "procurement", "commerce", "billing", "notification", "james", "virtual-assistant", "marketing-accelerator", "teaming", "partner", "email"),
    ),
    "infrastructure": (
        "Infrastructure",
        "Docker, Groovy operational scripts, environment configs, and deployment.",
        ("config", "docker", "groovy", "deploy", "log-management", "analytics", "provisioning", "cookie-banner", "override", "layout-provider", "theme", "site-initializer"),
    ),
}


def atomic_write_text(target_path: Path, content: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=str(target_path.parent),
        delete=False, prefix=".tmp_", suffix=".tmp",
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, target_path)


def write_docusaurus_category_json(folder_path: Path, label: str, position: int, description: str) -> None:
    payload = {
        "label": label,
        "position": position,
        "link": {"type": "generated-index", "title": label, "description": description},
    }
    atomic_write_text(folder_path / "_category_.json", json.dumps(payload, indent=2) + "\n")


def compute_sha256(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _natural_sort_key(path: Path) -> list[Any]:
    parts = re.split(r"(\d+)", str(path).lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _is_ignored_json(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith(".") or name in ("_category_.json", "crawl-summary.json") or name.endswith("manifest.json"):
        return True
    return any(part.lower() in IGNORED_DIR_NAMES for part in path.parts[:-1])


def find_json_files(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {input_dir}")
    files = sorted(
        (p for p in input_dir.rglob("*.json") if p.is_file() and not _is_ignored_json(p)),
        key=_natural_sort_key,
    )
    if not files:
        raise FileNotFoundError(f"No *.json files found in {input_dir}")
    return files


def _merge_compacted_technical_parts(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge already-compacted (~15-55 KB) technical summaries without holding multiple raw JSONs in RAM."""
    merged_summary: dict[str, Any] = {}
    merged_files_by_lang: dict[str, int] = {}
    merged_modules: dict[str, int] = {}
    merged_extensions: dict[str, dict[str, Any]] = {}
    merged_java_classes: dict[tuple[str, str], dict[str, Any]] = {}
    merged_raw: dict[str, Any] = {}

    for part in parts:
        summary = part.get("summary") or {}
        if summary.get("totalFiles"):
            merged_summary["totalFiles"] = max(merged_summary.get("totalFiles", 0), int(summary["totalFiles"]))
        for lang, cnt in (summary.get("filesByLanguage") or {}).items():
            merged_files_by_lang[lang] = max(merged_files_by_lang.get(lang, 0), int(cnt))
        for mod, cnt in (summary.get("modulesDiscovered") or {}).items():
            merged_modules[mod] = max(merged_modules.get(mod, 0), int(cnt))

        # Prefer the javaSummary that actually contains parsed module/class detail
        js = summary.get("javaSummary")
        if isinstance(js, dict):
            prev_js = merged_summary.get("javaSummary")
            if not isinstance(prev_js, dict) or (js.get("modules") and not prev_js.get("modules")):
                merged_summary["javaSummary"] = js

        for ext in part.get("client_extensions") or []:
            if isinstance(ext, dict) and ext.get("extensionName"):
                merged_extensions[ext["extensionName"]] = ext

        for cls in part.get("java_classes") or []:
            if isinstance(cls, dict) and cls.get("className"):
                key = (cls.get("module", ""), cls["className"])
                merged_java_classes[key] = cls

        for rk, rv in (part.get("raw_content") or {}).items():
            if not rv:
                continue
            if rk not in merged_raw:
                merged_raw[rk] = rv
            elif isinstance(rv, list) and isinstance(merged_raw[rk], list):
                merged_raw[rk] = merged_raw[rk] + rv
            elif isinstance(rv, dict) and isinstance(merged_raw[rk], dict):
                merged_raw[rk] = {**merged_raw[rk], **rv}

    if merged_files_by_lang:
        merged_summary["filesByLanguage"] = merged_files_by_lang
    if merged_modules:
        merged_summary["modulesDiscovered"] = merged_modules

    return {
        "summary": merged_summary,
        "client_extensions": list(merged_extensions.values()),
        "java_classes": list(merged_java_classes.values()),
        "raw_content": merged_raw,
    }


def _matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    low = (text or "").lower()
    return any(kw in low for kw in keywords)


def _slice_technical_for_category(merged: dict[str, Any], cat: str) -> dict[str, Any] | None:
    """Build a focused TechnicalDocItem payload for one output category from the merged compact summary."""
    label, _, keywords = TECHNICAL_CATEGORY_META.get(cat, (cat.title(), f"{cat.title()} documentation.", ()))
    summary = merged.get("summary") or {}
    all_exts = merged.get("client_extensions") or []
    all_classes = merged.get("java_classes") or []
    raw_content = merged.get("raw_content") or {}

    if cat == "architecture":
        return {
            "category": cat,
            "title": f"{label} Overview",
            "summary": summary,
            "client_extensions": all_exts,
            "java_classes": all_classes[:80],
            "raw_content": {
                k: v for k, v in raw_content.items()
                if k in ("pythonServices",)
            },
        }

    # Filter Java modules, Java classes, and client extensions by category keywords
    java_summary = summary.get("javaSummary") or {}
    filtered_modules = {
        m: stats for m, stats in (java_summary.get("modules") or {}).items()
        if _matches_keywords(m, keywords)
    }
    filtered_classes = [
        c for c in all_classes
        if _matches_keywords(f"{c.get('module', '')} {c.get('packageName', '')} {c.get('className', '')}", keywords)
    ]
    filtered_exts = [
        e for e in all_exts
        if _matches_keywords(e.get("extensionName", ""), keywords)
    ]

    cat_raw: dict[str, Any] = {}
    if cat == "infrastructure":
        if raw_content.get("configurations"):
            cat_raw["configurations"] = raw_content["configurations"]
        if raw_content.get("environmentVariables"):
            cat_raw["environmentVariables"] = raw_content["environmentVariables"]
    elif cat == "authentication":
        if raw_content.get("configurations"):
            auth_cfgs = [c for c in raw_content["configurations"] if _matches_keywords(str(c), keywords)]
            if auth_cfgs:
                cat_raw["configurations"] = auth_cfgs
        if raw_content.get("environmentVariables"):
            auth_envs = {k: v for k, v in raw_content["environmentVariables"].items() if _matches_keywords(k, keywords)}
            if auth_envs:
                cat_raw["environmentVariables"] = auth_envs
    elif cat == "integrations":
        if raw_content.get("pythonServices"):
            cat_raw["pythonServices"] = raw_content["pythonServices"]
    elif cat == "database":
        if raw_content.get("configurations"):
            db_cfgs = [c for c in raw_content["configurations"] if _matches_keywords(str(c), ("jdbc", "db", "sql", "mongo", "redis", "elastic", "data"))]
            if db_cfgs:
                cat_raw["configurations"] = db_cfgs

    if not (filtered_modules or filtered_classes or filtered_exts or cat_raw):
        return None

    cat_summary: dict[str, Any] = {
        "totalFiles": summary.get("totalFiles", 0),
        "matchedJavaModules": filtered_modules,
        "matchedClientExtensionCount": len(filtered_exts),
    }

    return {
        "category": cat,
        "title": f"{label} Overview",
        "summary": cat_summary,
        "client_extensions": filtered_exts,
        "java_classes": filtered_classes[:80],
        "raw_content": cat_raw,
    }


class ScreenshotPublisher:
    """Finds a page's screenshot. Copying/blurring it is done later, inside the page's own job."""

    def __init__(self, static_root: Path, portal: str, search_roots: list[Path]) -> None:
        self.static_root = static_root
        self.portal = portal
        self.search_roots = [r for r in search_roots if r]

    def resolve(self, ref: str | None, source_dir: Path | None = None) -> tuple[str | None, Path | None, Path | None]:
        """Return (web_path, source_file, destination_file) without copying anything."""
        candidates: list[Path] = []
        if ref:
            name = PureWindowsPath(ref).name
            candidates.append(Path(ref))
            for root in self.search_roots:
                candidates += [root / ref, root / "screenshots" / name, root / name]
        if source_dir and source_dir.is_dir():
            candidates += sorted(
                (p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
                key=_natural_sort_key,
            )
        for cand in candidates:
            try:
                if cand.is_file():
                    raw_name = cand.name
                    if cand.stem.lower() in GENERIC_IMAGE_STEMS and cand.parent.name:
                        raw_name = f"{slugify(cand.parent.name)}-{raw_name}"
                    dest = self.static_root / "img" / self.portal / raw_name
                    return f"/img/{self.portal}/{quote(raw_name)}", cand, dest
            except OSError:
                continue
        return None, None, None


@dataclass
class ImageSpec:
    src: Path
    dest: Path
    terms: list[str]

    def signature(self) -> str:
        """What the published image depends on: the source bytes and the blur settings."""
        h = hashlib.sha256(self.src.read_bytes())
        h.update(("|".join(self.terms) if REDACT_SCREENSHOTS else "no-redaction").encode("utf-8"))
        return h.hexdigest()


def _materialize_image(spec: ImageSpec) -> None:
    """Save the screenshot to static/img (blurred). If blurring fails nothing unblurred is left behind."""
    try:
        if REDACT_SCREENSHOTS:
            redact_image(spec.src, spec.dest, spec.terms)
        else:
            spec.dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(spec.src, spec.dest)
    except Exception:
        spec.dest.unlink(missing_ok=True)
        raise


@dataclass
class DocJob:
    """One MDX file to generate (a whole page, or one part of a split page)."""
    label: str
    key: str
    target: Path
    payload: dict[str, Any]
    compile_args: dict[str, Any]
    digest: str
    part: tuple[int, int] | None = None
    image: ImageSpec | None = None


@dataclass
class DocGroup:
    """All parts of one source document (used to clean up parts that no longer exist)."""
    base_key: str
    target_dir: Path
    slug: str
    total: int
    keys: list[str] = field(default_factory=list)


class DocumentationPipeline:
    def __init__(
        self,
        docs_root: Path,
        static_root: Path,
        manifest_path: Path,
        compiler: GeminiMdxCompiler,
        force_rebuild: bool = False,
        with_frontmatter: bool = True,
        limit: int = 0,
    ) -> None:
        self.docs_root = docs_root
        self.static_root = static_root
        self.manifest_path = manifest_path
        self.compiler = compiler
        self.force_rebuild = force_rebuild
        self.with_frontmatter = with_frontmatter
        self.limit = limit
        self.manifest: dict[str, str] = self._load_manifest()
        self.generated_files: list[str] = []
        self.skipped_files: list[str] = []
        self.failures: list[str] = []
        self.failure_details: list[dict[str, str]] = []
        self.failed_keys: set[str] = set()
        self._jobs: list[DocJob] = []
        self._groups: list[DocGroup] = []
        self._fatal: FatalLLMError | None = None
        self._ocr_sem = asyncio.Semaphore(OCR_CONCURRENCY)
        self._liferay_api_planned = 0

    # ------------------------------------------------------------------ manifest
    def _load_manifest(self) -> dict[str, str]:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
            except Exception:
                pass
        return {}

    def save_manifest(self) -> None:
        atomic_write_text(self.manifest_path, json.dumps(self.manifest, indent=2, sort_keys=True) + "\n")

    def _limit_reached(self) -> bool:
        return bool(self.limit) and len(self._jobs) >= self.limit

    def _record_failure(self, label: str, exc: BaseException, key: str | None = None) -> None:
        self.failures.append(f"{label}: {exc}")
        self.failure_details.append({"file": label, "reason": str(exc)[:500]})
        if key:
            self.failed_keys.add(key)
        logger.error("FAILED %s: %s", label, str(exc)[:300])

    def _portal_dirs(self, portal: str) -> tuple[Path, Path, str]:
        """Return (portal_root_dir, user_guide_dir, user_guide_label) matching the target docs layout."""
        root_name, guide_sub, guide_label = PORTAL_OUTPUT_DIRS.get(
            portal, (f"{portal}-documentation", "user-guide", "User Guide")
        )
        root_dir = self.docs_root / root_name
        return root_dir, root_dir / guide_sub, guide_label

    def _ensure_admin_developer_docs_dir(self) -> Path:
        """Ensure admin-documentation/ and admin-documentation/developer-docs/ exist."""
        admin_docs_dir, _, _ = self._portal_dirs("admin")
        write_docusaurus_category_json(
            admin_docs_dir,
            PORTAL_DISPLAY_NAMES["admin"],
            PORTAL_SIDEBAR_POSITIONS["admin"],
            "Admin portal guides and internal developer documentation.",
        )
        dev_docs_dir = admin_docs_dir / "developer-docs"
        write_docusaurus_category_json(
            dev_docs_dir,
            "Developer Documentation",
            2,
            "Internal system architecture, APIs, and technical documentation.",
        )
        return dev_docs_dir

    # ------------------------------------------------------------------ planning (ordered, no LLM calls)
    def _plan_document(
        self,
        *,
        key: str,
        target_dir: Path,
        slug: str,
        title: str,
        position: int,
        payload: dict[str, Any],
        doc_kind: str,
        label: str,
        image: ImageSpec | None = None,
        max_list_items: dict[str, int] | None = None,
        max_parts: int | None = None,
    ) -> int:
        """Split the payload if it is too big for the model, and queue one job per part.

        Returns how many sidebar positions the document used (1 + extra parts).
        """
        chunks = split_payload(payload, CHUNK_TOKEN_BUDGET, max_list_items=max_list_items)
        if max_parts is not None and max_parts >= 0 and len(chunks) > max_parts:
            chunks = chunks[:max_parts]
        total = len(chunks)
        if total == 0:
            return 0
        group = DocGroup(base_key=key, target_dir=target_dir, slug=slug, total=total)
        self._groups.append(group)
        image_sig = image.signature() if image else ""

        for i, chunk in enumerate(chunks, start=1):
            part_slug = slug if i == 1 else f"{slug}-part-{i}"
            part_key = key if i == 1 else f"{key}::part-{i}"
            part_label = label if total == 1 else f"{label} [part {i}/{total}]"
            part_title = title if total == 1 else f"{title} (Part {i} of {total})"
            target = target_dir / f"{part_slug}.mdx"
            part_image = image if i == 1 else None
            digest = compute_sha256({
                "payload": chunk,
                "part": [i, total],
                "image": image_sig if part_image else "",
                "mode": self.compiler.mode_label,
                "prompt": PROMPT_VERSION,
                "frontmatter": self.with_frontmatter,
            })
            group.keys.append(part_key)
            up_to_date = (
                not self.force_rebuild
                and self.manifest.get(part_key) == digest
                and target.exists()
                and (part_image is None or part_image.dest.exists())
            )
            if up_to_date:
                self.skipped_files.append(str(target))
                continue
            if self._limit_reached():
                continue
            self._jobs.append(DocJob(
                label=part_label,
                key=part_key,
                target=target,
                payload=chunk,
                compile_args={"doc_kind": doc_kind, "doc_id": part_slug, "title": part_title, "position": position + i - 1},
                digest=digest,
                part=(i, total) if total > 1 else None,
                image=part_image,
            ))
        return total

    # ------------------------------------------------------------------ running (concurrent)
    async def _run_job(self, job: DocJob) -> None:
        try:
            if job.image:
                async with self._ocr_sem:
                    await asyncio.to_thread(_materialize_image, job.image)
            mdx = await self.compiler.compile_to_validated_mdx(
                payload=job.payload,
                with_frontmatter=self.with_frontmatter,
                image_path=job.image.dest if job.image else None,
                part=job.part,
                **job.compile_args,
            )
            atomic_write_text(job.target, mdx)
            self.manifest[job.key] = job.digest        # recorded ONLY after a successful write
            self.save_manifest()
            self.generated_files.append(str(job.target))
            logger.info("Generated: %s", job.target)
        except (FatalLLMError, asyncio.CancelledError):
            raise
        except Exception as exc:
            self._record_failure(job.label, exc, job.key)

    async def run_jobs(self) -> None:
        jobs, self._jobs = self._jobs, []
        if not jobs:
            return
        logger.info("Generating %d file(s) with up to %d Gemini call(s) at a time.", len(jobs), self.compiler.concurrency)
        tasks: list[asyncio.Task[None]] = []

        async def guarded(job: DocJob) -> None:
            try:
                await self._run_job(job)
            except FatalLLMError as exc:               # e.g. bad API key: nothing else can succeed
                self._fatal = self._fatal or exc
                for t in tasks:
                    if t is not asyncio.current_task():
                        t.cancel()

        tasks.extend(asyncio.create_task(guarded(j)) for j in jobs)
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._fatal:
            self.save_manifest()
            raise self._fatal

    # ------------------------------------------------------------------ after the run
    def _cleanup_stale_parts(self) -> None:
        """Delete `<slug>-part-N.mdx` files left over from an earlier, longer split of the same document."""
        for g in self._groups:
            if any(k in self.failed_keys for k in g.keys):
                continue                                   # something failed: keep what is there
            for path in g.target_dir.glob(f"{g.slug}-part-*.mdx"):
                m = re.search(r"-part-(\d+)\.mdx$", path.name)
                part_key = f"{g.base_key}::part-{m.group(1)}" if m else ""
                if m and int(m.group(1)) > g.total and part_key in self.manifest:   # only files WE created
                    path.unlink(missing_ok=True)
                    self.manifest.pop(part_key, None)
                    logger.info("Removed stale part: %s", path)
        self.save_manifest()

    def _write_failure_report(self) -> None:
        report = self.manifest_path.with_name("compile_failures.json")
        if self.failure_details:
            atomic_write_text(report, json.dumps(self.failure_details, indent=2, ensure_ascii=False) + "\n")
            logger.error("%d file(s) FAILED (details in %s). Run again to retry only these:", len(self.failure_details), report)
            for item in self.failure_details:
                logger.error("  - %s: %s", item["file"], item["reason"][:160])
        else:
            report.unlink(missing_ok=True)

    async def run(self, project_dir: Path, portal_filter: str | None = None) -> None:
        """Plan everything in order, generate concurrently, then clean up and report."""
        self.process_project_dir(project_dir, portal_filter)
        try:
            await self.run_jobs()
        finally:
            self._cleanup_stale_parts()
            self._write_failure_report()

    # ---------------------------------------------------------------- Frontend
    def process_frontend_folder(self, input_dir: Path, portal: str | None = None) -> None:
        portal = portal or DEFAULT_FRONTEND_PORTAL
        files = find_json_files(input_dir)
        planned_docs, route_map = build_site_graph(files)
        logger.info("Portal '%s': %d unique page(s) after deduplication.", portal, len(planned_docs))

        portal_title = PORTAL_DISPLAY_NAMES.get(portal, f"{portal.title()} Documentation")
        portal_pos = PORTAL_SIDEBAR_POSITIONS.get(portal, 10)
        portal_root_dir, user_guide_dir, user_guide_label = self._portal_dirs(portal)

        write_docusaurus_category_json(portal_root_dir, portal_title, portal_pos, f"{portal_title} guides and workflows.")
        write_docusaurus_category_json(user_guide_dir, user_guide_label, 1, f"{portal_title} user guides.")

        # Auto-generate Welcome landing page inside user-guide / admin-user-guide
        if planned_docs:
            welcome_mdx = build_welcome_mdx(portal_title, planned_docs)
            atomic_write_text(user_guide_dir / "index.mdx", welcome_mdx)

        # Sensitive values found on ANY page of this portal are blurred on EVERY screenshot of it.
        pii_terms = (
            collect_pii_terms([d.json_path for d in planned_docs], REDACT_TERMS, REDACT_PERSON_NAMES)
            if REDACT_SCREENSHOTS else []
        )

        cat_positions: dict[str, int] = {}
        written_cats: set[str] = set()

        for doc in planned_docs:
            if self._limit_reached():
                break
            label = f"{portal}/{doc.rel_doc_path} ({doc.json_path.parent.name})"
            try:
                raw = load_and_check_json_file(doc.json_path)
                extracted = extract_structured_frontend_fields(raw)
                publisher = ScreenshotPublisher(self.static_root, portal, [doc.json_path.parent, input_dir])
                web_path, shot_src, shot_dest = publisher.resolve(raw.get("screenshot"), doc.json_path.parent)

                # Resolve inline cross-links using relative paths across category folders
                links = []
                seen_targets: set[str] = set()
                for lnk in raw.get("links") or []:
                    if not isinstance(lnk, dict):
                        continue
                    href_route = normalize_route_path(lnk.get("href") or "")
                    if href_route in route_map and href_route != doc.route:
                        rel_target = compute_relative_doc_link(doc.rel_doc_path, route_map[href_route])
                        if rel_target not in seen_targets:
                            seen_targets.add(rel_target)
                            lbl = (lnk.get("text") or lnk.get("title") or href_route).strip()
                            if lbl:
                                links.append({"label": lbl[:80], "target": rel_target})

                page = FrontendPageItem(
                    route=doc.route,
                    title=doc.title,
                    category_slug=doc.category_slug,
                    category_label=doc.category_label,
                    headings=extracted["headings"],
                    summary_text=extracted["summary_text"],
                    form_fields=extracted["form_fields"],
                    buttons=extracted["buttons"],
                    screenshots=[web_path] if web_path else [],
                    links=links,
                )

                cat_dir = user_guide_dir / doc.category_slug
                if doc.category_slug not in written_cats:
                    order = CATEGORY_ORDER.get(doc.category_slug, (doc.category_label, 50))[1]
                    write_docusaurus_category_json(cat_dir, doc.category_label, order, f"{doc.category_label} guides.")
                    written_cats.add(doc.category_slug)

                start = cat_positions.get(doc.category_slug, 0) + 1
                used = self._plan_document(
                    key=f"frontend::{portal}::{doc.rel_doc_path}",
                    target_dir=cat_dir,
                    slug=doc.doc_slug,
                    title=doc.title,
                    position=start,
                    payload=page.model_dump(),
                    doc_kind="frontend",
                    label=label,
                    image=ImageSpec(shot_src, shot_dest, pii_terms) if shot_src and shot_dest else None,
                )
                cat_positions[doc.category_slug] = start + used - 1
            except FatalLLMError:
                raise
            except Exception as exc:
                self._record_failure(label, exc)

    # ---------------------------------------------------------------- Backend Technical (Subset of Admin)
    def process_technical_dir(self, technical_root: Path) -> None:
        dev_docs_dir = self._ensure_admin_developer_docs_dir()
        tech_out_root = dev_docs_dir / "technical"
        write_docusaurus_category_json(
            tech_out_root,
            "Technical Documentation",
            2,
            "Architecture, database, authentication, integrations, and infrastructure documentation.",
        )

        # Track sidebar positions per output category so flat & subfolder inputs never collide
        cat_positions: dict[str, int] = {}

        # 1. Flat input JSONs directly inside backend/technical/ (e.g., response 1.json, response 2.json)
        #    Compact each file ONE BY ONE to keep RAM low, then merge only the small summaries.
        top_level_jsons = sorted(
            (p for p in technical_root.glob("*.json") if p.is_file() and not _is_ignored_json(p)),
            key=_natural_sort_key,
        )
        if top_level_jsons:
            compacted_parts: list[dict[str, Any]] = []
            for jf in top_level_jsons:
                try:
                    raw = load_and_check_json_file(jf)
                    if isinstance(raw, dict):
                        compacted_parts.append(compact_technical_json(raw, "architecture", jf.stem))
                    del raw  # free large raw dict immediately before loading the next file
                except FatalLLMError:
                    raise
                except Exception as exc:
                    self._record_failure(f"technical/{jf.name}", exc)

            if compacted_parts:
                merged_compact = _merge_compacted_technical_parts(compacted_parts)
                src_names = ", ".join(p.name for p in top_level_jsons)
                for idx, cat in enumerate(TECHNICAL_CATEGORIES, start=1):
                    if self._limit_reached():
                        return
                    sliced = _slice_technical_for_category(merged_compact, cat)
                    if not sliced:
                        continue
                    cat_label, cat_desc, _ = TECHNICAL_CATEGORY_META.get(
                        cat, (cat.title(), f"{cat.title()} documentation.", ())
                    )
                    cat_out = tech_out_root / cat
                    write_docusaurus_category_json(cat_out, cat_label, idx, cat_desc)
                    label = f"technical/{cat}/combined ({src_names})"
                    try:
                        item = TechnicalDocItem.model_validate(sliced)
                        slug = slugify(item.title)
                        pos = cat_positions.get(cat, 0) + 1
                        used = self._plan_document(
                            key=f"technical::{cat}::{slug}",
                            target_dir=cat_out,
                            slug=slug,
                            title=item.title,
                            position=pos,
                            payload=item.model_dump(),
                            doc_kind="technical",
                            label=label,
                        )
                        cat_positions[cat] = pos + used - 1
                    except FatalLLMError:
                        raise
                    except Exception as exc:
                        self._record_failure(label, exc)

        # 2. Also support pre-categorized subfolders (backend/technical/{architecture,database,...}/*.json)
        for idx, cat in enumerate(TECHNICAL_CATEGORIES, start=1):
            cat_src = technical_root / cat
            if not cat_src.is_dir():
                continue
            try:
                files = find_json_files(cat_src)
            except FileNotFoundError:
                continue

            cat_label, cat_desc, _ = TECHNICAL_CATEGORY_META.get(
                cat, (cat.title(), f"{cat.title()} documentation.", ())
            )
            cat_out = tech_out_root / cat
            write_docusaurus_category_json(cat_out, cat_label, idx, cat_desc)

            for jf in files:
                if self._limit_reached():
                    return
                label = f"technical/{cat}/{jf.name}"
                try:
                    raw = load_and_check_json_file(jf)
                    compacted = compact_technical_json(raw, cat, jf.stem)
                    del raw
                    item = TechnicalDocItem.model_validate(compacted)
                    slug = slugify(item.title)
                    pos = cat_positions.get(cat, 0) + 1
                    used = self._plan_document(
                        key=f"technical::{cat}::{slug}",
                        target_dir=cat_out,
                        slug=slug,
                        title=item.title,
                        position=pos,
                        payload=item.model_dump(),
                        doc_kind="technical",
                        label=label,
                    )
                    cat_positions[cat] = pos + used - 1
                except FatalLLMError:
                    raise
                except Exception as exc:
                    self._record_failure(label, exc)

    # ---------------------------------------------------------------- Backend API (Subset of Admin)
    def process_backend_api_json(self, json_path: Path, api_source: str, portal: str | None = None) -> None:
        if api_source == "liferay-api" and MAX_LIFERAY_API_FILES and self._liferay_api_planned >= MAX_LIFERAY_API_FILES:
            logger.info(
                "Skipping %s: already reached MAX_LIFERAY_API_FILES (%d).",
                json_path.name, MAX_LIFERAY_API_FILES,
            )
            return

        raw = load_and_check_json_file(json_path)
        if not isinstance(raw, dict):
            raise ValueError(f"{json_path} must contain a JSON object.")
        raw.setdefault("portal", portal or DEFAULT_API_PORTAL)
        raw.setdefault("api_source", api_source)
        bundle = BackendApiBundle.model_validate(raw)

        dev_docs_dir = self._ensure_admin_developer_docs_dir()
        apis_dir = dev_docs_dir / "apis"
        write_docusaurus_category_json(apis_dir, "APIs and Services", 1, "Liferay and Spring Boot REST API specifications.")

        sub_dir = apis_dir / bundle.api_source
        write_docusaurus_category_json(
            sub_dir,
            "Liferay API" if bundle.api_source == "liferay-api" else "Spring Boot API",
            1 if bundle.api_source == "liferay-api" else 2,
            f"{bundle.api_source} reference.",
        )

        grouped: dict[str, list[Any]] = {}
        for ep in bundle.endpoints:
            grouped.setdefault(ep.controller_or_tag.strip() or "General", []).append(ep)

        position = 1
        for tag, endpoints in grouped.items():
            if self._limit_reached():
                return
            max_parts: int | None = None
            if bundle.api_source == "liferay-api" and MAX_LIFERAY_API_FILES:
                remaining = MAX_LIFERAY_API_FILES - self._liferay_api_planned
                if remaining <= 0:
                    logger.info(
                        "Reached MAX_LIFERAY_API_FILES (%d) in %s; stopping Liferay API generation and continuing with the rest of the pipeline.",
                        MAX_LIFERAY_API_FILES, json_path.name,
                    )
                    return
                max_parts = remaining

            slug = slugify(tag)
            payload = {
                "service_name": bundle.service_name,
                "base_path": bundle.base_path,
                "deployed_as": bundle.deployed_as,
                "structure": bundle.structure,
                "endpoints": [e.model_dump() for e in endpoints],
            }
            # Up to MAX_ENDPOINTS_PER_MDX endpoints per file AND within the token budget; extra files are "-part-N".
            used = self._plan_document(
                key=f"api::{bundle.api_source}::{slug}",
                target_dir=sub_dir,
                slug=slug,
                title=tag,
                position=position,
                payload=payload,
                doc_kind="liferay_api" if bundle.api_source == "liferay-api" else "springboot_api",
                label=f"api/{bundle.api_source}/{tag} ({json_path.name})",
                max_list_items={"endpoints": MAX_ENDPOINTS_PER_MDX},
                max_parts=max_parts,
            )
            position += used
            if bundle.api_source == "liferay-api":
                self._liferay_api_planned += used

    # ---------------------------------------------------------------- Full Project
    def process_project_dir(self, project_dir: Path, portal_filter: str | None = None) -> None:
        frontend_root = project_dir / "frontend"
        if frontend_root.is_dir():
            for pdir in sorted((p for p in frontend_root.iterdir() if p.is_dir()), key=_natural_sort_key):
                pname = pdir.name.lower()
                if portal_filter and pname != portal_filter:
                    continue
                if pname in ALLOWED_PORTALS:
                    try:
                        self.process_frontend_folder(pdir, portal=pname)
                    except FileNotFoundError:
                        logger.warning("No JSONs in %s", pdir)

        # Backend APIs & Technical docs belong under admin-documentation/developer-docs/
        if not portal_filter or portal_filter == "admin":
            for api_source, rel in (("liferay-api", Path("backend/api/liferay")), ("springboot-api", Path("backend/api/springboot"))):
                api_dir = project_dir / rel
                if api_dir.is_dir():
                    for jf in sorted((p for p in api_dir.rglob("*.json") if not _is_ignored_json(p)), key=_natural_sort_key):
                        if self._limit_reached():
                            return
                        try:
                            self.process_backend_api_json(jf, api_source, "admin")
                        except FatalLLMError:
                            raise
                        except Exception as exc:
                            self._record_failure(f"api/{api_source}/{jf.name}", exc)

            tech_root = project_dir / "backend" / "technical"
            if tech_root.is_dir():
                self.process_technical_dir(tech_root)