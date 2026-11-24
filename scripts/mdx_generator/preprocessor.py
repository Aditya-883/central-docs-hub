from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import logger

# --- PII & Sensitive Patterns ---
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<![\w/.\-])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}(?![\w\-])"
)
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# No trailing \b so concatenated strings like "Cage Code: 63FU2UEI: HYPCPMX1HCR9" are caught
CAGE_LABELED_RE = re.compile(r"(\bCAGE(?:\s+Code)?\s*[:#=\-]\s*)([A-Za-z0-9]{5})", re.IGNORECASE)
UEI_LABELED_RE = re.compile(
    r"(\b(?:UEI|SAM\s+UEI|Unique\s+Entity\s+(?:ID|Identifier))(?:\s+Code)?\s*[:#=\-]\s*)([A-Za-z0-9]{12})",
    re.IGNORECASE,
)
PRIMARY_NAICS_LABELED_RE = re.compile(
    r"(\bPrimary\s+NAICS(?:\s+Code)?\s*[:#=\-]\s*)(\d{6}(?:\s*[-–—]\s*[^,;\n|]+)?)",
    re.IGNORECASE,
)
CLIENT_NAME_LABELED_RE = re.compile(r"(\bClient\s+Name\s*[:=\-]\s*)([^,;\n|]+)", re.IGNORECASE)

SENSITIVE_KEY_TOKENS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cagecode", "cage_code", "cage"), "[REDACTED_CAGE]"),
    (("ueicode", "uei_code", "samuei", "uniqueentityid", "uei"), "[REDACTED_UEI]"),
    (("primarynaics", "primary_naics", "primarynaicscode"), "[REDACTED_PRIMARY_NAICS]"),
    (("clientname", "client_name"), "[REDACTED_CLIENT_NAME]"),
)

# --- Liferay & Cookie Banner Noise ---
NOISE_TEXT_EXACT = frozenset({
    "skip to main content", "open accessibility menu", "open applications menu",
    "open product menu", "ctrl+alt+a", "edit", "configure page", "options",
    "simulation", "page audit", "toggle navigation", "close", "hidden",
    "customize", "reject all", "accept all", "export / import", "configuration",
    "configuration templates", "look and feel configuration", "permissions",
    "portal admin", "‹", "›",
})

NOISE_TEXT_SUBSTRINGS = (
    "we use cookies to deliver personalized content",
    "accept all cookies for the best possible experience",
    "g-recaptcha-response",
)

TITLE_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*(?:GovCon\s*360\s*(?:Internal|Public\s*Site)?|Liferay|Asponte\s+Technology\s+Inc\.?).*$",
    re.IGNORECASE,
)

TITLE_NORMALIZATIONS: dict[str, str] = {
    "login": "Signing in",
    "sign in": "Signing in",
    "home": "Welcome",
}

# Top-level technical JSON keys that add frontend/styling noise and should be excluded
IGNORED_TECHNICAL_KEYS = frozenset({
    "cssAnalysis",
    "scssAnalysis",
    "gradleAnalysis",
    "schemaVersion",
})

ARCHITECTURAL_JAVA_ANNOTATIONS = frozenset({
    "Component",
    "Path",
    "javax.ws.rs.Path",
    "Meta.OCD",
    "ExtendedObjectClassDefinition",
    "RestController",
    "Controller",
    "Service",
    "Configuration",
    "Provider",
    "OpenAPIDefinition",
})


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-") or "index"


def clean_page_title(raw_title: str, fallback_stem: str = "") -> str:
    """Strip Liferay/tenant suffixes and normalize common page titles."""
    cleaned = TITLE_SUFFIX_RE.sub("", (raw_title or "").strip()).strip(" -–—|")
    if not cleaned:
        cleaned = fallback_stem.replace("-", " ").title() or "Overview"
    return TITLE_NORMALIZATIONS.get(cleaned.lower(), cleaned)


def _redaction_token_for_key(key: str) -> str | None:
    norm = re.sub(r"[^a-z0-9]", "", key.lower())
    for aliases, token in SENSITIVE_KEY_TOKENS:
        if norm in aliases:
            return token
    return None


def mask_pii(text: str) -> str:
    if not text:
        return ""
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = SSN_RE.sub("[REDACTED_SSN]", text)
    text = PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = CAGE_LABELED_RE.sub(r"\1[REDACTED_CAGE]", text)
    text = UEI_LABELED_RE.sub(r"\1[REDACTED_UEI]", text)
    text = PRIMARY_NAICS_LABELED_RE.sub(r"\1[REDACTED_PRIMARY_NAICS]", text)
    text = CLIENT_NAME_LABELED_RE.sub(r"\1[REDACTED_CLIENT_NAME]", text)
    return text


def sanitize_json_tree(obj: Any, parent_key: str = "") -> Any:
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
    if not file_path.exists():
        raise FileNotFoundError(f"Input JSON file does not exist: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"Input path is not a file: {file_path}")
    raw_bytes = file_path.read_bytes()
    if not raw_bytes.strip():
        raise ValueError(f"Input JSON file is empty: {file_path}")
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
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


def _is_noise_text(text: str) -> bool:
    low = " ".join((text or "").split()).lower()
    if not low or low in NOISE_TEXT_EXACT:
        return True
    if any(sub in low for sub in NOISE_TEXT_SUBSTRINGS):
        return True
    return False


def extract_structured_frontend_fields(rec: dict[str, Any]) -> dict[str, Any]:
    """Extract clean headings, paragraphs, buttons, and form fields from raw crawler JSON."""
    headings_out: list[str] = []
    raw_headings = rec.get("headings")
    if isinstance(raw_headings, dict):
        for level in ("h1", "h2", "h3", "h4", "h5", "h6"):
            for h in raw_headings.get(level) or []:
                if isinstance(h, str) and not _is_noise_text(h) and h not in headings_out:
                    headings_out.append(h.strip())
    elif isinstance(raw_headings, list):
        for h in raw_headings:
            if isinstance(h, str) and not _is_noise_text(h) and h not in headings_out:
                headings_out.append(h.strip())

    paragraphs_out: list[str] = []
    for p in rec.get("paragraphs") or []:
        if isinstance(p, str) and not _is_noise_text(p) and p not in paragraphs_out:
            paragraphs_out.append(p.strip())

    buttons_out: list[str] = []
    for el in rec.get("clickableElements") or []:
        if not isinstance(el, dict):
            continue
        label = (el.get("text") or el.get("ariaLabel") or el.get("title") or "").strip()
        if label and not _is_noise_text(label) and not label.isdigit() and len(label) > 2:
            if label not in buttons_out:
                buttons_out.append(label)

    form_fields_out: list[dict[str, Any]] = []
    seen_fields: set[str] = set()
    raw_inputs = list(rec.get("inputs") or [])
    for form in rec.get("forms") or []:
        if isinstance(form, dict):
            raw_inputs.extend(form.get("fields") or [])

    for field in raw_inputs:
        if not isinstance(field, dict):
            continue
        ftype = (field.get("type") or "text").lower()
        name = (field.get("name") or "").strip()
        if ftype in ("hidden", "submit", "button") or "recaptcha" in name.lower():
            continue
        label = (field.get("ariaLabel") or field.get("placeholder") or name or field.get("id") or "").strip()
        if not label or _is_noise_text(label):
            continue
        key = label.lower()
        if key in seen_fields:
            continue
        seen_fields.add(key)
        form_fields_out.append({
            "label": label,
            "field_type": ftype,
            "required": bool(field.get("required", False)),
            "placeholder": (field.get("placeholder") or "").strip(),
        })

    # Build a clean summary from pageDescription (stripped of Liferay/cookie noise)
    desc_lines = [
        line.strip()
        for line in (rec.get("pageDescription") or "").splitlines()
        if line.strip() and not _is_noise_text(line)
    ]
    summary_text = " | ".join(dict.fromkeys(desc_lines))[:1500]

    return {
        "headings": headings_out,
        "paragraphs": paragraphs_out,
        "buttons": buttons_out,
        "form_fields": form_fields_out,
        "summary_text": summary_text,
    }


# --- Liferay OpenAPI v2.0 Detection & Normalization ---

def is_liferay_openapi_json(raw: Any) -> bool:
    """Detect Liferay Global REST OpenAPI v2.0 JSON (api + resources + schemas/parameters)."""
    if not isinstance(raw, dict):
        return False
    # Never misclassify Spring Boot / standard OpenAPI payloads that already use standard keys
    if any(k in raw for k in ("endpoints", "apis", "modules", "paths")):
        return False
    return isinstance(raw.get("api"), dict) and isinstance(raw.get("resources"), list)


def compact_schema_object(schema_obj: dict[str, Any], max_props: int = 30) -> dict[str, Any]:
    """Compact an OpenAPI schema definition so it stays token-safe for LLM MDX generation."""
    if not isinstance(schema_obj, dict):
        return {}
    out: dict[str, Any] = {}
    if schema_obj.get("type"):
        out["type"] = schema_obj["type"]
    if schema_obj.get("description"):
        out["description"] = str(schema_obj["description"])[:200]
    if schema_obj.get("required"):
        out["required"] = list(schema_obj["required"])[:max_props]

    props = schema_obj.get("properties")
    if isinstance(props, dict) and props:
        compact_props: dict[str, Any] = {}
        for idx, (pname, pval) in enumerate(props.items()):
            if idx >= max_props:
                compact_props["_truncated"] = f"+{len(props) - max_props} more properties"
                break
            if not isinstance(pval, dict):
                continue
            p_entry: dict[str, Any] = {}
            if "$ref" in pval:
                p_entry["ref"] = pval["$ref"]
            elif pval.get("type"):
                p_entry["type"] = pval["type"]
            if pval.get("format"):
                p_entry["format"] = pval["format"]
            if isinstance(pval.get("items"), dict):
                items = pval["items"]
                if "$ref" in items:
                    p_entry["itemsRef"] = items["$ref"]
                elif items.get("type"):
                    p_entry["itemsType"] = items["type"]
            if pval.get("readOnly"):
                p_entry["readOnly"] = True
            if pval.get("description"):
                p_entry["description"] = str(pval["description"])[:120]
            compact_props[pname] = p_entry
        out["properties"] = compact_props
    return out


def _compact_liferay_openapi_json(raw: dict[str, Any], category: str, stem: str) -> dict[str, Any]:
    """Summarize Liferay Global REST OpenAPI v2.0 JSON without blowing token limits."""
    api_meta = raw.get("api") or {}
    resources = raw.get("resources") or []

    method_counts: Counter[str] = Counter()
    group_stats: dict[tuple[str, str], dict[str, Any]] = {}

    for res in resources:
        if not isinstance(res, dict):
            continue
        api_name = (res.get("apiName") or "Unclassified").strip() or "Unclassified"
        api_ver = (res.get("apiVersion") or "Unknown").strip() or "Unknown"
        key = (api_name, api_ver)
        if key not in group_stats:
            group_stats[key] = {
                "apiName": api_name,
                "apiVersion": api_ver,
                "resourceCount": 0,
                "operationCount": 0,
                "resources": [],
                "samplePaths": [],
            }
        entry = group_stats[key]
        entry["resourceCount"] += 1
        res_name = (res.get("resourceName") or "").strip()
        if res_name and len(entry["resources"]) < 15:
            entry["resources"].append(res_name)

        for op in res.get("operations") or []:
            if not isinstance(op, dict):
                continue
            entry["operationCount"] += 1
            method = (op.get("method") or "GET").upper()
            method_counts[method] += 1
            path = op.get("path", "")
            if path and len(entry["samplePaths"]) < 5 and path not in entry["samplePaths"]:
                entry["samplePaths"].append(path)

    api_groups = sorted(
        group_stats.values(),
        key=lambda g: (-g["operationCount"], g["apiName"], g["apiVersion"]),
    )

    summary = {
        "format": "liferay-openapi-v2",
        "documentVersion": raw.get("documentVersion", ""),
        "generatedAt": raw.get("generatedAt", ""),
        "openApiVersion": api_meta.get("openApiVersion", ""),
        "servers": api_meta.get("servers", []),
        "apiGroupCount": len(api_groups),
        "resourceCount": raw.get("resourceCount", len(resources)),
        "operationCount": raw.get("operationCount", sum(method_counts.values())),
        "schemaCount": raw.get("schemaCount", len(raw.get("schemas") or {})),
        "relationshipCount": raw.get("relationshipCount", len(raw.get("relationships") or {})),
        "parameterCount": len(raw.get("parameters") or {}),
        "operationsByMethod": dict(method_counts.most_common()),
    }

    title = (api_meta.get("title") or "").strip() or stem.replace("-", " ").replace("_", " ").title()

    return {
        "category": category,
        "title": title,
        "summary": summary,
        "client_extensions": [],
        "java_classes": [],
        "raw_content": {
            "api": api_meta,
            "apiGroups": api_groups,
        },
    }


# --- Technical Source-Code AST Compaction Helpers ---

def _extract_repo_module(file_path: str) -> str:
    """Normalize a source path to a concise module identifier (e.g. modules/common/nl-keycloak)."""
    clean = re.sub(r"^file://", "", (file_path or "").strip()).replace("\\", "/")
    for marker in ("/modules/", "/client-extensions/", "/packages/", "/configs/", "/scripts/", "/groovy/"):
        if marker in clean:
            rel = marker.lstrip("/") + clean.split(marker, 1)[1]
            parts = rel.split("/")
            return "/".join(parts[:3]) if parts[0] in ("modules", "client-extensions") and len(parts) >= 3 else "/".join(parts[:2])
    parts = [p for p in clean.split("/") if p]
    return "/".join(parts[-3:-1]) if len(parts) >= 3 else (parts[0] if parts else "root")


def _compact_java_analysis(java_analysis: dict[str, Any], max_classes: int = 120) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Compact javaAnalysis by stripping massive `dependencies` / `imports` / `annotationDetails`
    blobs and rolling up `@Generated` Liferay REST Builder classes into module-level summaries.
    """
    classes = java_analysis.get("classes") or []
    total_java_files = java_analysis.get("totalJavaFiles", len(classes))
    total_classes = java_analysis.get("totalClasses", len(classes))

    if not isinstance(classes, list) or not classes:
        return (
            {"totalJavaFiles": total_java_files, "totalClasses": total_classes},
            [],
        )

    class_type_counts: Counter[str] = Counter()
    annotation_counts: Counter[str] = Counter()
    module_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "custom": 0, "generated": 0, "components": 0, "restResources": 0, "osgiConfigs": 0}
    )

    priority_classes: list[tuple[int, dict[str, Any]]] = []
    generated_count = 0

    for cls in classes:
        if not isinstance(cls, dict):
            continue
        anns = [str(a) for a in (cls.get("annotations") or []) if a]
        ann_set = set(anns)
        is_generated = "Generated" in ann_set
        if is_generated:
            generated_count += 1

        ctype = str(cls.get("classType") or "CLASS")
        class_type_counts[ctype] += 1
        for a in anns:
            annotation_counts[a] += 1

        mod = _extract_repo_module(str(cls.get("filePath") or ""))
        mstat = module_stats[mod]
        mstat["total"] += 1
        mstat["generated" if is_generated else "custom"] += 1
        if "Component" in ann_set:
            mstat["components"] += 1
        if "Path" in ann_set or "javax.ws.rs.Path" in ann_set:
            mstat["restResources"] += 1
        if "Meta.OCD" in ann_set:
            mstat["osgiConfigs"] += 1

        # Skip @Generated REST Builder boilerplate from per-class payload (already summarized per module)
        if is_generated:
            continue

        # Prioritize REST endpoints, OSGi configs, and @Component services
        if "Path" in ann_set or "javax.ws.rs.Path" in ann_set or "RestController" in ann_set:
            priority = 0
        elif "Meta.OCD" in ann_set or "ExtendedObjectClassDefinition" in ann_set:
            priority = 1
        elif "Component" in ann_set or "Service" in ann_set or "Configuration" in ann_set:
            priority = 2
        else:
            priority = 3

        methods = cls.get("methods") or []
        key_methods: list[str] = []
        for m in methods:
            if not isinstance(m, dict):
                continue
            mname = m.get("methodName") or ""
            if not mname:
                continue
            ret = m.get("returnType") or "void"
            key_methods.append(f"{mname}() -> {ret}")
            if len(key_methods) >= 6:
                break

        compact_cls = {
            "className": cls.get("className", ""),
            "classType": ctype,
            "module": mod,
            "packageName": cls.get("packageName", ""),
            "annotations": [a for a in anns if a in ARCHITECTURAL_JAVA_ANNOTATIONS][:8],
            "methodCount": len(methods),
            "keyMethods": key_methods,
            "fieldCount": len(cls.get("fields") or []),
        }
        priority_classes.append((priority, compact_cls))

    priority_classes.sort(key=lambda item: (item[0], item[1]["module"], item[1]["className"]))
    selected_classes = [item[1] for item in priority_classes[:max_classes]]

    sorted_modules = dict(
        sorted(module_stats.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    )

    java_summary = {
        "totalJavaFiles": total_java_files,
        "totalClasses": total_classes,
        "customClasses": len(classes) - generated_count,
        "generatedClasses": generated_count,
        "classTypes": dict(class_type_counts),
        "topAnnotations": dict(annotation_counts.most_common(15)),
        "modules": sorted_modules,
        "includedCustomClassCount": len(selected_classes),
    }
    return java_summary, selected_classes


def _compact_python_analysis(items: list[Any], max_files: int = 80) -> list[dict[str, Any]]:
    """Keep high-signal Python module structure (functions, env vars, docstrings) and drop AST noise."""
    out: list[dict[str, Any]] = []
    for item in items[:max_files]:
        if not isinstance(item, dict):
            continue
        funcs_raw = item.get("functions") or []
        func_names: list[str] = []
        for fn in funcs_raw[:12]:
            if isinstance(fn, dict):
                name = fn.get("name") or fn.get("functionName") or ""
                if name:
                    func_names.append(str(name))
            elif isinstance(fn, str) and fn:
                func_names.append(fn)

        env_vars = item.get("environmentVariables") or []
        docs = item.get("docstrings") or []
        first_doc = ""
        if isinstance(docs, list) and docs:
            first_doc = str(docs[0])[:180]
        elif isinstance(docs, str):
            first_doc = docs[:180]

        entry: dict[str, Any] = {
            "module": _extract_repo_module(str(item.get("filePath") or "")),
            "moduleName": item.get("moduleName", ""),
            "lineCount": item.get("lineCount", 0),
            "functionCount": len(funcs_raw),
            "functions": func_names,
        }
        if env_vars:
            entry["environmentVariables"] = env_vars[:20]
        if first_doc:
            entry["summary"] = first_doc
        out.append(entry)
    return out


def _compact_configuration_analysis(items: list[Any], max_files: int = 80) -> list[dict[str, Any]]:
    """Keep environment, file path, type, and key names (omitting verbose keyDetails / secret values)."""
    out: list[dict[str, Any]] = []
    for item in items[:max_files]:
        if not isinstance(item, dict):
            continue
        keys = item.get("keys") or []
        out.append({
            "environment": item.get("environment", ""),
            "filePath": _extract_repo_module(str(item.get("filePath") or "")) + "/" + Path(str(item.get("filePath") or "")).name,
            "fileType": item.get("fileType", ""),
            "status": item.get("status", ""),
            "keyCount": len(keys) if isinstance(keys, list) else 0,
            "keys": keys[:25] if isinstance(keys, list) else [],
        })
    return out


def _compact_env_var_references(items: list[Any]) -> dict[str, list[str]]:
    """Group environmentVariableReferences by reference name -> deduplicated module/file list."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("reference") or "").strip()
        fp = str(item.get("filePath") or "").strip()
        if not ref:
            continue
        short_fp = _extract_repo_module(fp) + "/" + Path(fp).name if fp else ""
        if short_fp and short_fp not in grouped[ref] and len(grouped[ref]) < 10:
            grouped[ref].append(short_fp)
    return dict(sorted(grouped.items()))


def compact_technical_json(raw: dict[str, Any], category: str, stem: str) -> dict[str, Any]:
    """Summarize large repo-scan technical JSONs (response 1 + response 2 / merged) and Liferay OpenAPI JSONs without blowing token limits."""
    if is_liferay_openapi_json(raw):
        return _compact_liferay_openapi_json(raw, category, stem)

    summary: dict[str, Any] = {}
    if isinstance(raw.get("fileDiscovery"), dict):
        fd = raw["fileDiscovery"]
        cat_files = fd.get("categorizedFiles") or {}
        file_counts_by_type = fd.get("fileCountsByType") or {}

        if isinstance(file_counts_by_type, dict) and file_counts_by_type:
            lang_counts = {str(k): int(v) for k, v in file_counts_by_type.items() if v}
        else:
            lang_counts = {k: len(v) for k, v in cat_files.items() if isinstance(v, list) and v}

        # Extract top-level module & client-extension folders when categorizedFiles paths are present
        module_counter: Counter[str] = Counter()
        for paths in cat_files.values():
            if not isinstance(paths, list):
                continue
            for p in paths:
                if not isinstance(p, str):
                    continue
                if "client-extensions/" in p:
                    rel = p.split("client-extensions/", 1)[1]
                    parts = rel.split("/")
                    mod = "client-extensions/" + ("/".join(parts[:2]) if len(parts) >= 2 else parts[0])
                    module_counter[mod] += 1
                elif "modules/" in p:
                    rel = p.split("modules/", 1)[1]
                    parts = rel.split("/")
                    mod = "modules/" + ("/".join(parts[:2]) if len(parts) >= 2 else parts[0])
                    module_counter[mod] += 1

        summary = {
            "totalFiles": fd.get("totalFiles", sum(lang_counts.values())),
            "filesByLanguage": lang_counts,
        }
        if module_counter:
            summary["modulesDiscovered"] = dict(module_counter.most_common(50))

    # Client Extensions (keep high-level YAML config + frontend file count; drop low-level UI/CSS noise)
    client_extensions: list[dict[str, Any]] = []
    for ext in raw.get("clientExtensionAnalysis") or []:
        if not isinstance(ext, dict):
            continue
        configs = []
        for cfg in ext.get("configurations") or []:
            if not isinstance(cfg, dict):
                continue
            props = cfg.get("properties") or {}
            configs.append({
                "key": cfg.get("configurationKey", ""),
                "name": cfg.get("name", ""),
                "type": cfg.get("type", ""),
                "friendlyURLMapping": props.get("friendlyURLMapping", ""),
                "scopes": props.get("scopes", []),
                "pid": props.get("pid", ""),
            })
        frontend_files = ext.get("frontendFiles") or []
        entry: dict[str, Any] = {
            "extensionName": ext.get("extensionName", ""),
            "configurations": configs,
            "assemble": ext.get("assemble", []),
        }
        if isinstance(frontend_files, list) and frontend_files:
            entry["frontendFileCount"] = len(frontend_files)
        client_extensions.append(entry)

    # Java Analysis (strip 65MB+ dependencies/imports and roll up @Generated classes)
    java_classes: list[dict[str, Any]] = []
    if isinstance(raw.get("javaAnalysis"), dict):
        java_summary, java_classes = _compact_java_analysis(raw["javaAnalysis"])
        summary["javaSummary"] = java_summary

    # Compact high-signal non-Java technical sections into raw_content; drop CSS/SCSS/Gradle noise
    extra_raw: dict[str, Any] = {}
    if isinstance(raw.get("pythonAnalysis"), list) and raw["pythonAnalysis"]:
        extra_raw["pythonServices"] = _compact_python_analysis(raw["pythonAnalysis"])
    if isinstance(raw.get("configurationAnalysis"), list) and raw["configurationAnalysis"]:
        extra_raw["configurations"] = _compact_configuration_analysis(raw["configurationAnalysis"])
    if isinstance(raw.get("environmentVariableReferences"), list) and raw["environmentVariableReferences"]:
        extra_raw["environmentVariables"] = _compact_env_var_references(raw["environmentVariableReferences"])

    handled_keys = {
        "fileDiscovery",
        "javaAnalysis",
        "clientExtensionAnalysis",
        "pythonAnalysis",
        "configurationAnalysis",
        "environmentVariableReferences",
    } | IGNORED_TECHNICAL_KEYS

    for k, v in raw.items():
        if k not in handled_keys and v:
            extra_raw[k] = v

    title = stem.replace("-", " ").replace("_", " ").title()
    if re.fullmatch(r"(?:response|technical)(?:\s*\d+)?", stem.strip(), re.IGNORECASE):
        title = f"{category.title()} Overview"

    return {
        "category": category,
        "title": title,
        "summary": summary,
        "client_extensions": client_extensions,
        "java_classes": java_classes,
        "raw_content": extra_raw,
    }