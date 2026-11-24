from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator
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


class BackendApiBundle(BaseModel):
    portal: Literal["admin", "client", "govcon360", "public", "asponte"] = "admin"
    api_source: Literal["liferay-api", "springboot-api"]
    service_name: str = "Core Service"
    version: str = "v1.0"
    base_path: str = "/"
    deployed_as: list[str] = Field(default_factory=list)
    structure: list[dict[str, str]] = Field(default_factory=list)
    endpoints: list[ApiEndpointItem] = Field(..., min_length=1)


class TechnicalDocItem(BaseModel):
    """Normalized payload for backend/technical/<category>/*.json."""
    category: str
    title: str
    summary: dict[str, Any] = Field(default_factory=dict)
    client_extensions: list[dict[str, Any]] = Field(default_factory=list)
    java_classes: list[dict[str, Any]] = Field(default_factory=list)
    raw_content: dict[str, Any] = Field(default_factory=dict)