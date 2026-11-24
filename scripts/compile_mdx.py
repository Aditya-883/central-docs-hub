#!/usr/bin/env python3
"""CLI entry point for the modular GovCon360 Docusaurus v3 MDX compiler."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mdx_generator.compiler import GeminiMdxCompiler
from mdx_generator.config import (
    ALLOWED_PORTALS,
    DEFAULT_MODEL,
    GEMINI_CONCURRENCY,
    REDACT_SCREENSHOTS,
    FatalLLMError,
    logger,
)
from mdx_generator.chunker import split_payload
from mdx_generator.pipeline import DocumentationPipeline
from mdx_generator.redactor import extract_pii_values, ocr_available


async def amain(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compile frontend/API/technical JSON into Docusaurus v3 MDX.")
    p.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    p.add_argument("--portal", choices=ALLOWED_PORTALS)
    p.add_argument("--docs-root", type=Path)
    p.add_argument("--static-root", type=Path)
    p.add_argument("--manifest", type=Path, help="default: <project-dir>/.sync-manifest.json")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--self-test", action="store_true", help="run offline smoke tests (chunker + PII) and exit")
    p.add_argument("--concurrency", type=int, default=GEMINI_CONCURRENCY,
                   help="Gemini calls at the same time (1 = one after another).")
    args = p.parse_args(argv)

    if args.self_test:
        chunks = split_payload({"title": "Test", "endpoints": [{"id": i, "desc": "x" * 200} for i in range(20)]}, 2000, max_list_items={"endpoints": 5})
        assert len(chunks) >= 4, f"expected >=4 chunks, got {len(chunks)}"
        pii = extract_pii_values({"cageCode": "1A2B3", "uei": "J9K8L7M6N5P4", "note": "Email a@b.com"})
        assert {"1A2B3", "J9K8L7M6N5P4", "a@b.com"}.issubset(pii), f"missing PII: {pii}"
        logger.info("[PASS] offline self-test OK")
        return 0

    docs_root = args.docs_root or args.project_dir / "docusaurus" / "docs"
    static_root = args.static_root or args.project_dir / "docusaurus" / "static"
    manifest_path = args.manifest or args.project_dir / ".sync-manifest.json"

    if REDACT_SCREENSHOTS and not ocr_available():
        logger.error(
            "Screenshot blurring is on but Tesseract OCR was not found. Install Tesseract and run "
            "'pip install -r requirements.txt' (or set REDACT_SCREENSHOTS=0 to skip blurring)."
        )
        return 2

    try:
        compiler = GeminiMdxCompiler(model_name=args.model, mock_mode=args.mock, concurrency=args.concurrency)
    except (RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 2

    pipeline = DocumentationPipeline(
        docs_root=docs_root,
        static_root=static_root,
        manifest_path=manifest_path,
        compiler=compiler,
        force_rebuild=args.force,
        limit=args.limit,
    )

    try:
        await pipeline.run(args.project_dir, args.portal)
    except FatalLLMError as exc:
        logger.error("STOPPED: %s", exc)
        pipeline.save_manifest()
        return 2
    finally:
        await compiler.aclose()

    pipeline.save_manifest()
    logger.info(
        "Done | generated: %d | skipped: %d | failed: %d | tokens: %s",
        len(pipeline.generated_files), len(pipeline.skipped_files), len(pipeline.failures), compiler.token_stats,
    )
    return 1 if pipeline.failures else 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        logger.warning("Interrupted. Finished files are saved; run again to continue with the rest.")
        return 130


if __name__ == "__main__":
    sys.exit(main())