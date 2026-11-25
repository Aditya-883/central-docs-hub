from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from .config import POISON_PAGE_TITLES


class FormFieldItem(BaseModel):
    label: str = Field(..., min_length=1)
    field_type: str = "text"
    required: bool = False
    placeholder: str = ""
    validation_note: str = ""


class LinkItem(BaseModel):
    """One verified internal Docusaurus documentation link (relative .mdx path)."""
    label: str = Field(..., min_length=1)
    target: str = Field(..., min_length=1)


class SectionItem(BaseModel):
    heading: str = ""
    paragraphs: list[str] = Field(default_factory=list)
    list_items: list[str] = Field(default_factory=list)
    children: list[SectionItem] = Field(default_factory=list)


SectionItem.model_rebuild()


def _section_has_text(section: SectionItem) -> bool:
    return bool(section.paragraphs or section.list_items) or any(_section_has_text(c) for c in section.children)


class FrontendPageItem(BaseModel):
    """One cleaned, deduplicated portal screen."""
    route: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    category_slug: str = "general"
    category_label: str = "General"
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
    raw_content: dict[str, Any] = Field(default_factory=dict)

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


# --- API Bundle Format Adapters (Liferay OpenAPI v2.0, Spring Boot Aggregator, OpenAPI 3.x) ---

VALID_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})


def _extract_schema_ref_or_summary(obj: Any) -> dict[str, Any] | str:
    """Pull a concise schema reference or summary from OpenAPI/Liferay/Spring requestBody or response blocks."""
    if not obj:
        return {}
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return {}

    if "$ref" in obj:
        return str(obj["$ref"])
    if obj.get("type") and isinstance(obj["type"], str) and len(obj) <= 3:
        return obj["type"]

    # Liferay content list: [{"mediaTypes": [...], "schema": {"$ref": "..."}}]
    content = obj.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("schema"):
            return _extract_schema_ref_or_summary(first["schema"])
    elif isinstance(content, dict) and content:
        first = next(iter(content.values()))
        if isinstance(first, dict) and first.get("schema"):
            return _extract_schema_ref_or_summary(first["schema"])

    if isinstance(obj.get("schema"), (dict, str)):
        return _extract_schema_ref_or_summary(obj["schema"])
    return obj


def _extract_responses_summary(responses: Any) -> tuple[dict[str, Any] | str, list[str]]:
    """Return (primary_response_schema, error_codes) from a responses dict."""
    if not isinstance(responses, dict):
        return {}, []
    primary: dict[str, Any] | str = {}
    errors: list[str] = []
    for code, rval in responses.items():
        code_str = str(code).strip()
        if code_str.isdigit() and int(code_str) >= 400:
            desc = rval.get("description", "") if isinstance(rval, dict) else ""
            errors.append(f"{code_str}: {desc}".strip(": ") if desc else code_str)
        elif not primary and (code_str.startswith("2") or code_str.lower() == "default"):
            primary = _extract_schema_ref_or_summary(rval)
    return primary, errors


def _normalize_parameters(raw_params: Any, param_registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Normalize parameter dicts or `$ref` strings into ApiParameterItem dicts."""
    if not isinstance(raw_params, list):
        return []
    reg = param_registry or {}
    out: list[dict[str, Any]] = []
    for p in raw_params:
        if isinstance(p, str):
            p = reg.get(p) or {"name": p}
        elif isinstance(p, dict) and "$ref" in p and not p.get("name"):
            ref_key = str(p["$ref"]).split("/")[-1]
            p = reg.get(p["$ref"]) or reg.get(ref_key) or {"name": ref_key}
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        dtype = p.get("data_type") or p.get("type")
        if not dtype and isinstance(p.get("schema"), dict):
            dtype = p["schema"].get("type") or p["schema"].get("$ref")
        out.append({
            "name": name,
            "location": str(p.get("location") or p.get("in") or "query"),
            "data_type": str(dtype or "string"),
            "required": bool(p.get("required", False)),
            "description": str(p.get("description") or "")[:300],
        })
    return out


def _adapt_liferay_openapi_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """Unwrap Liferay Global REST OpenAPI v2.0 (`api` + `resources` + `parameters`) into BackendApiBundle fields."""
    api_meta = raw.get("api") or {}
    param_registry = raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {}
    servers = api_meta.get("servers") or []
    base_path = str(servers[0]) if servers else "/o"

    endpoints: list[dict[str, Any]] = []
    for res in raw.get("resources") or []:
        if not isinstance(res, dict):
            continue
        api_name = str(res.get("apiName") or "General").strip() or "General"
        res_name = str(res.get("resourceName") or "").strip()
        for op in res.get("operations") or []:
            if not isinstance(op, dict):
                continue
            method = str(op.get("method") or "").strip().upper()
            path = str(op.get("path") or "").strip()
            if method not in VALID_HTTP_METHODS or not path:
                continue
            op_id = str(op.get("operationId") or "").strip()
            desc = str(op.get("description") or "").strip()
            summary = str(op.get("summary") or "").strip() or (f"[{res_name}] {op_id}".strip() if op_id else desc[:160])
            resp_schema, err_codes = _extract_responses_summary(op.get("responses"))
            endpoints.append({
                "method": method,
                "path": path,
                "summary": summary,
                "description": desc or summary,
                "controller_or_tag": api_name,
                "auth_required": True,
                "parameters": _normalize_parameters(op.get("parameters"), param_registry),
                "request_body_schema": _extract_schema_ref_or_summary(op.get("requestBody")),
                "response_schema": resp_schema,
                "error_codes": err_codes,
            })

    return {
        "portal": raw.get("portal", "admin"),
        "api_source": raw.get("api_source", "liferay-api"),
        "service_name": str(api_meta.get("title") or "Liferay Global REST API"),
        "version": str(api_meta.get("openApiVersion") or raw.get("documentVersion") or "v1.0"),
        "base_path": base_path,
        "endpoints": endpoints,
    }


def _extract_modules_endpoints(modules: list[Any], fallback_tag: str = "General") -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []
    for mod in modules:
        if not isinstance(mod, dict):
            continue
        tag = str(mod.get("name") or fallback_tag).strip() or fallback_tag
        for ep in mod.get("endpoints") or []:
            if not isinstance(ep, dict):
                continue
            method = str(ep.get("method") or "").strip().upper()
            path = str(ep.get("path") or "").strip()
            if method not in VALID_HTTP_METHODS or not path:
                continue
            resp_schema, err_codes = _extract_responses_summary(ep.get("responses"))
            endpoints.append({
                "method": method,
                "path": path,
                "summary": str(ep.get("summary") or ep.get("operationId") or "").strip(),
                "description": str(ep.get("description") or "").strip(),
                "controller_or_tag": str(ep.get("controller_or_tag") or tag),
                "auth_required": bool(ep.get("auth_required", True)),
                "parameters": _normalize_parameters(ep.get("parameters")),
                "request_body_schema": _extract_schema_ref_or_summary(ep.get("requestBody") or ep.get("request_body_schema")),
                "response_schema": resp_schema or _extract_schema_ref_or_summary(ep.get("response_schema")),
                "error_codes": err_codes or list(ep.get("error_codes") or []),
            })
    return endpoints


def _adapt_springboot_apis(raw: dict[str, Any]) -> dict[str, Any]:
    """Unwrap Spring Boot aggregator (`apis` -> `documentation` -> `modules`) or top-level `modules`."""
    endpoints: list[dict[str, Any]] = []
    service_name = "Spring Boot API"
    version = "v1.0"
    base_path = "/api"

    if isinstance(raw.get("apis"), list):
        for api_entry in raw["apis"]:
            if not isinstance(api_entry, dict):
                continue
            api_name = str(api_entry.get("name") or "General").strip()
            doc = api_entry.get("documentation") if isinstance(api_entry.get("documentation"), dict) else api_entry
            meta = doc.get("documentMetadata") if isinstance(doc.get("documentMetadata"), dict) else {}
            if service_name == "Spring Boot API" and meta.get("title"):
                service_name = str(meta["title"])
            if version == "v1.0" and meta.get("apiVersion"):
                version = str(meta["apiVersion"])
            servers = meta.get("servers") or []
            if base_path == "/api" and servers:
                base_path = str(servers[0]).split(" ")[0]
            if isinstance(doc.get("modules"), list):
                endpoints.extend(_extract_modules_endpoints(doc["modules"], fallback_tag=api_name))
            elif isinstance(doc.get("endpoints"), list):
                endpoints.extend(_extract_modules_endpoints([{"name": api_name, "endpoints": doc["endpoints"]}], fallback_tag=api_name))
    elif isinstance(raw.get("modules"), list):
        meta = raw.get("documentMetadata") if isinstance(raw.get("documentMetadata"), dict) else {}
        service_name = str(meta.get("title") or raw.get("service_name") or service_name)
        version = str(meta.get("apiVersion") or raw.get("version") or version)
        endpoints.extend(_extract_modules_endpoints(raw["modules"]))

    return {
        "portal": raw.get("portal", "admin"),
        "api_source": raw.get("api_source", "springboot-api"),
        "service_name": service_name,
        "version": version,
        "base_path": base_path,
        "endpoints": endpoints,
    }


def _adapt_openapi_paths(raw: dict[str, Any]) -> dict[str, Any]:
    """Unwrap standard OpenAPI 3.x `paths` object into BackendApiBundle fields."""
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    servers = raw.get("servers") or []
    base_path = "/"
    if servers and isinstance(servers[0], dict) and servers[0].get("url"):
        base_path = str(servers[0]["url"])
    elif servers and isinstance(servers[0], str):
        base_path = servers[0]

    endpoints: list[dict[str, Any]] = []
    for path_str, methods_obj in (raw.get("paths") or {}).items():
        if not isinstance(methods_obj, dict):
            continue
        for method_key, op in methods_obj.items():
            method = str(method_key).strip().upper()
            if method not in VALID_HTTP_METHODS or not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            tag = str(tags[0]) if tags else "General"
            resp_schema, err_codes = _extract_responses_summary(op.get("responses"))
            endpoints.append({
                "method": method,
                "path": str(path_str),
                "summary": str(op.get("summary") or op.get("operationId") or "").strip(),
                "description": str(op.get("description") or "").strip(),
                "controller_or_tag": tag,
                "auth_required": bool(op.get("security", True)),
                "parameters": _normalize_parameters(op.get("parameters")),
                "request_body_schema": _extract_schema_ref_or_summary(op.get("requestBody")),
                "response_schema": resp_schema,
                "error_codes": err_codes,
            })

    return {
        "portal": raw.get("portal", "admin"),
        "api_source": raw.get("api_source", "springboot-api"),
        "service_name": str(info.get("title") or raw.get("service_name") or "Core Service"),
        "version": str(info.get("version") or raw.get("version") or "v1.0"),
        "base_path": base_path,
        "endpoints": endpoints,
    }


class BackendApiBundle(BaseModel):
    portal: Literal["admin", "client", "govcon360", "public", "asponte"] = "admin"
    api_source: Literal["liferay-api", "springboot-api"]
    service_name: str = "Core Service"
    version: str = "v1.0"
    base_path: str = "/"
    deployed_as: list[str] = Field(default_factory=list)
    structure: list[dict[str, str]] = Field(default_factory=list)
    endpoints: list[ApiEndpointItem] = Field(..., min_length=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_api_bundle(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if isinstance(data.get("endpoints"), list) and data["endpoints"]:
            return data
        if isinstance(data.get("api"), dict) and isinstance(data.get("resources"), list):
            return _adapt_liferay_openapi_v2(data)
        if isinstance(data.get("apis"), list) or isinstance(data.get("modules"), list):
            return _adapt_springboot_apis(data)
        if isinstance(data.get("paths"), dict):
            return _adapt_openapi_paths(data)
        return data


class TechnicalDocItem(BaseModel):
    """Normalized payload for backend/technical/<category>/*.json."""
    category: str
    title: str
    summary: dict[str, Any] = Field(default_factory=dict)
    client_extensions: list[dict[str, Any]] = Field(default_factory=list)
    java_classes: list[dict[str, Any]] = Field(default_factory=list)
    raw_content: dict[str, Any] = Field(default_factory=dict)