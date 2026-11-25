from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Literal

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

from .config import (
    DEFAULT_MODEL,
    FALLBACK_MODEL,
    FatalLLMError,
    GEMINI_CONCURRENCY,
    LLM_TIMEOUT_SECONDS,
    MAX_LLM_ATTEMPTS,
    MIN_SECONDS_BETWEEN_CALLS,
    logger,
)
from .prompts import API_SYSTEM_PROMPT, FRONTEND_SYSTEM_PROMPT, TECHNICAL_SYSTEM_PROMPT
from .sanitizer import sanitize_mdx_for_docusaurus_v3, validate_mdx_structure


def _prune_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _prune_empty(v) for k, v in value.items() if v not in ("", [], {}, None)}
    if isinstance(value, list):
        return [_prune_empty(v) for v in value]
    return value


def _part_one_slug(doc_id: str) -> str:
    """Return the base Part 1 slug from a `-part-N` doc_id."""
    return re.sub(r"-part-\d+$", "", doc_id)


def build_mock_mdx(
    payload: dict[str, Any],
    doc_kind: str,
    part: tuple[int, int] | None = None,
    doc_id: str = "",
) -> str:
    """Offline deterministic stand-in for --mock and --self-test."""
    is_continuation = bool(part and part[0] > 1)
    base_slug = _part_one_slug(doc_id) if doc_id else ""

    if doc_kind == "technical":
        exts = payload.get("client_extensions", [])
        lines = [
            "---", 'description: "Mock technical architecture doc."', "---", "",
        ]
        if is_continuation and part:
            lines += [
                f"Continuation of [{base_slug or 'Part 1'}](./{base_slug}.mdx) (Part {part[0]} of {part[1]}).",
                "",
            ]
        else:
            lines += [
                ":::warning[INTERNAL]",
                "Internal engineering documentation.",
                ":::", "",
                "## System overview", "",
                "```mermaid",
                "flowchart TD",
                "    subgraph Portal",
                "        A[Client Extensions] --> B[Liferay DXP]",
                "    end",
                "```", "",
            ]
        lines += [
            "## Components and extensions", "",
            "| Extension | Configurations |",
            "| :--- | :--- |",
        ]
        for ext in exts[:10]:
            lines.append(f"| `{ext.get('extensionName')}` | {len(ext.get('configurations', []))} |")
        return "\n".join(lines)

    if doc_kind in ("liferay_api", "springboot_api"):
        eps = payload.get("endpoints", [])
        lines = ["---", 'description: "Mock API reference."', "---", ""]
        if is_continuation and part:
            lines += [
                f"Continuation of [{base_slug or 'Part 1'}](./{base_slug}.mdx) (Part {part[0]} of {part[1]} — {len(eps)} endpoints).",
                "",
                "## Endpoints",
                "",
            ]
        else:
            lines += [
                "| Property | Value |",
                "| :--- | :--- |",
                f"| Endpoints | {len(eps)} |",
                f"| Base path | `{payload.get('base_path', '/')}` |", "",
                "## Architecture", "",
                "```mermaid",
                "flowchart TD",
                "    A[Caller] --> B[API Service] --> C[(Database)]",
                "```", "",
                "## Endpoints", "",
            ]
        for ep in eps:
            lines += [f"### {ep.get('method')} `{ep.get('path')}`", "", ep.get("summary") or "Endpoint.", ""]
        return "\n".join(lines)

    # Frontend mock
    lines = ["---", 'description: "Mock user guide."', "---", ""]
    intro = payload.get("summary_text") or f"Use **{payload.get('title', 'this page')}** to manage your workflow."
    if payload.get("links"):
        refs = ", ".join(f"[{lnk['label']}]({lnk['target']})" for lnk in payload["links"][:3])
        intro = f"{intro} See {refs}."
    lines += [intro, ""]
    for shot in payload.get("screenshots", []):
        lines += [f"![{payload.get('title', 'Screenshot')}]({shot})", ""]
    if payload.get("form_fields"):
        lines += ["## Form fields", "", "| Field | Type | Required |", "| :--- | :--- | :--- |"]
        for f in payload["form_fields"]:
            lines.append(f"| **{f['label']}** | `{f.get('field_type', 'text')}` | `{f.get('required', False)}` |")
        lines.append("")
    return "\n".join(lines)


class RequestRejectedError(RuntimeError):
    """Gemini refused THIS request (for example it is too large). Only this file fails; the run continues."""


class OutputTruncatedError(RuntimeError):
    """The reply was cut off at the model's output limit. Lower CHUNK_TOKEN_BUDGET so the document is split smaller."""


def _classify_error(exc: Exception) -> str:
    """'fatal' = stop the whole run, 'file' = fail only this file, 'retry' = try again."""
    code = getattr(exc, "code", None)
    msg = str(exc).lower()
    if code in (401, 403, 404):
        return "fatal"                      # bad key / no permission / unknown model: nothing will work
    if code == 400:
        if "api key" in msg or "api_key" in msg or "permission" in msg:
            return "fatal"
        return "file"                       # e.g. request too large: only this file is affected
    return "retry"                          # 429, 5xx, timeouts, network errors


def _is_rate_or_overload(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    msg = str(exc).lower()
    return code in (429, 503) or "503" in msg or "unavailable" in msg or "high demand" in msg or "resource_exhausted" in msg


def _build_gen_config(system_instruction: str, model_name: str) -> Any:
    """Build GenerateContentConfig, keeping thinking low on Gemini 4 models for fast MDX generation."""
    kwargs: dict[str, Any] = {"system_instruction": system_instruction, "temperature": 0.1}
    if model_name.startswith("gemini-4-") and hasattr(types, "ThinkingConfig"):
        try:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        except Exception:
            pass
    return types.GenerateContentConfig(**kwargs)


class _Spacer:
    """Makes every Gemini request START at least `interval` seconds after the previous one (all tasks share it)."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            await asyncio.sleep(start - now)

    def pause(self, seconds: float) -> None:
        """After a rate-limit or 503 overload error, hold back every task for a while."""
        self._next = max(self._next, time.monotonic() + seconds)


class GeminiMdxCompiler:
    def __init__(self, api_key: str | None = None, model_name: str = DEFAULT_MODEL, mock_mode: bool = False,
                 concurrency: int = GEMINI_CONCURRENCY, fallback_model: str = FALLBACK_MODEL) -> None:
        self.model_name = model_name
        self.fallback_model = fallback_model.strip() if fallback_model else ""
        self.mock_mode = mock_mode
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.token_stats = {"total_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                            "total_tokens": 0, "failed_attempts": 0}
        self.concurrency = max(1, concurrency)
        self._sem = asyncio.Semaphore(self.concurrency)         # Gemini calls in flight
        self._spacer = _Spacer(MIN_SECONDS_BETWEEN_CALLS)       # spacing between request starts
        self.client = None
        if not mock_mode:
            if genai is None:
                raise RuntimeError("google-genai is not installed. Run: pip install google-genai")
            if not self.api_key:
                raise ValueError("GEMINI_API_KEY is missing. Set it in your .env file.")
            self.client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=int(LLM_TIMEOUT_SECONDS * 1000)),
            )

    @property
    def mode_label(self) -> str:
        return "mock" if self.mock_mode else self.model_name

    async def aclose(self) -> None:
        closer = getattr(getattr(self.client, "aio", None), "aclose", None)
        if closer:
            try:
                await closer()
            except Exception:
                pass

    async def _call_gemini(self, system_instruction: str, user_prompt: str, image_path: Path | None = None) -> str:
        contents: Any = user_prompt
        if image_path and image_path.is_file():
            mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
            contents = [types.Part.from_bytes(data=image_path.read_bytes(), mime_type=mime), user_prompt]

        last_err: Exception | None = None
        saw_overload = False
        for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
            use_fallback = (
                attempt == MAX_LLM_ATTEMPTS
                and saw_overload
                and self.fallback_model
                and self.fallback_model != self.model_name
            )
            active_model = self.fallback_model if use_fallback else self.model_name
            if use_fallback:
                logger.info("Primary model '%s' overloaded; trying fallback '%s' on attempt %d/%d.",
                            self.model_name, active_model, attempt, MAX_LLM_ATTEMPTS)
            try:
                async with self._sem:                    # at most N requests at the same time
                    await self._spacer.wait()            # and never faster than the interval
                    response = await self.client.aio.models.generate_content(
                        model=active_model,
                        contents=contents,
                        config=_build_gen_config(system_instruction, active_model),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.token_stats["failed_attempts"] += 1
                kind = _classify_error(exc)
                if kind == "fatal":
                    # Never abort the whole run just because the optional fallback model wasn't found/allowed
                    if use_fallback:
                        last_err = exc
                        logger.warning("Fallback model '%s' unavailable (%s).", active_model, str(exc)[:120])
                        continue
                    raise FatalLLMError(f"Gemini rejected the request (HTTP {getattr(exc, 'code', '?')}): {str(exc)[:300]}") from exc
                if kind == "file":
                    raise RequestRejectedError(f"Gemini rejected this request: {str(exc)[:300]}") from exc
                last_err = exc
                overloaded = _is_rate_or_overload(exc)
                if overloaded:
                    saw_overload = True
                # 10s after attempt 1, 20s after attempt 2 (max 30s total wait per file on 3 attempts)
                wait = (10 * attempt) if overloaded else (2 ** attempt)
                if overloaded and attempt < MAX_LLM_ATTEMPTS:
                    self._spacer.pause(wait)
                if attempt < MAX_LLM_ATTEMPTS:
                    logger.warning("Gemini attempt %d/%d failed (%s). Retrying in %ds.", attempt, MAX_LLM_ATTEMPTS, str(exc)[:120], wait)
                    await asyncio.sleep(wait + random.uniform(0, 1))
                else:
                    logger.warning("Gemini attempt %d/%d failed (%s).", attempt, MAX_LLM_ATTEMPTS, str(exc)[:120])
                continue

            self.token_stats["total_calls"] += 1
            usage = getattr(response, "usage_metadata", None)
            if usage:
                prompt = getattr(usage, "prompt_token_count", 0) or 0
                output = (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
                self.token_stats["prompt_tokens"] += prompt
                self.token_stats["completion_tokens"] += output
                self.token_stats["total_tokens"] += prompt + output
            candidates = getattr(response, "candidates", None) or []
            finish = str(getattr(candidates[0], "finish_reason", "")) if candidates else ""
            if "MAX_TOKENS" in finish.upper():
                raise OutputTruncatedError("Gemini stopped at its output limit; the document was cut off.")
            text = (response.text or "").strip()
            if text:
                return text
            last_err = ValueError("Gemini returned an empty response")
            self.token_stats["failed_attempts"] += 1
            if attempt < MAX_LLM_ATTEMPTS:
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"Gemini failed {MAX_LLM_ATTEMPTS} times. Last error: {last_err}")

    async def compile_to_validated_mdx(
        self,
        payload: dict[str, Any],
        doc_kind: Literal["frontend", "liferay_api", "springboot_api", "technical"],
        doc_id: str,
        title: str,
        position: int,
        with_frontmatter: bool = True,
        image_path: Path | None = None,
        part: tuple[int, int] | None = None,
    ) -> str:
        payload = _prune_empty(payload)
        is_api = doc_kind in ("liferay_api", "springboot_api")
        is_continuation = bool(part and part[0] > 1)

        # Prevent Part 2+ from seeing architecture/structure metadata and inject verified endpoint count
        prompt_payload = dict(payload)
        endpoint_count: int | None = None
        if is_api and isinstance(prompt_payload.get("endpoints"), list):
            endpoint_count = len(prompt_payload["endpoints"])
            prompt_payload["endpoint_count_in_this_part"] = endpoint_count
            if is_continuation:
                prompt_payload.pop("structure", None)

        if doc_kind == "frontend":
            system_prompt = FRONTEND_SYSTEM_PROMPT
        elif doc_kind == "technical":
            system_prompt = TECHNICAL_SYSTEM_PROMPT
        else:
            system_prompt = API_SYSTEM_PROMPT

        if self.mock_mode:
            raw_mdx = build_mock_mdx(prompt_payload, doc_kind, part=part, doc_id=doc_id)
        else:
            notes: list[str] = []
            if endpoint_count is not None:
                notes.append(
                    f"VERIFIED COUNT: This JSON contains exactly {endpoint_count} endpoint(s). "
                    f"If you display an endpoint count, you MUST write {endpoint_count} (never guess or recount)."
                )
            if part and part[1] > 1:
                i, n = part
                base_slug = _part_one_slug(doc_id)
                if i == 1:
                    notes.append(
                        f"PART {i} of {n}: Start the document normally (overview and architecture diagram first), "
                        "document ONLY the items in this JSON, and mention that additional items continue in Part 2."
                    )
                else:
                    notes.append(
                        f"CRITICAL CONTINUATION RULE (OVERRIDES SYSTEM PROMPT): This is PART {i} of {n}. "
                        f"Do NOT include an Overview section, summary properties table, introduction, or Mermaid architecture diagram — "
                        f"those are already in Part 1. Start immediately with a single line linking back to "
                        f"[Part 1](./{base_slug}.mdx), followed directly by the `## Endpoints` (or topic) section for ONLY the items in this JSON."
                    )
            note_block = ("\n\n" + "\n".join(notes)) if notes else ""
            user_prompt = f"Page title: {title}{note_block}\n\nJSON:\n{json.dumps(prompt_payload, indent=2, ensure_ascii=False)}"
            raw_mdx = await self._call_gemini(system_prompt, user_prompt, image_path=image_path)

        shots = payload.get("screenshots") or []
        if doc_kind == "frontend" and shots:
            # The model sometimes copies the prompt's placeholder instead of the real path.
            real = shots[0]
            raw_mdx = raw_mdx.replace("](screenshot_path)", f"]({real})")
            # Also repair images that use the right file name under a wrong folder
            # (e.g. `_markdown_images/05-pricing-screenshot.png`).
            base = real.rsplit("/", 1)[-1]
            raw_mdx = re.sub(r"\]\((?!/img/)(?:[^)\s]*/)?" + re.escape(base) + r"\)", lambda _m: f"]({real})", raw_mdx)

        mdx = sanitize_mdx_for_docusaurus_v3(raw_mdx, doc_id, title, position, with_frontmatter)
        problems = validate_mdx_structure(mdx, with_frontmatter)
        if problems and not self.mock_mode:
            logger.warning("Repairing %s due to: %s", doc_id, problems)
            repair = f"Fix these Docusaurus v3 MDX issues: {problems}\nReturn ONLY raw MDX:\n\n{mdx}"
            mdx = sanitize_mdx_for_docusaurus_v3(
                await self._call_gemini(system_prompt, repair), doc_id, title, position, with_frontmatter
            )
            problems = validate_mdx_structure(mdx, with_frontmatter)
        if problems:
            raise ValueError(f"Invalid MDX for {doc_id}: {problems}")
        return mdx