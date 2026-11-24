"""Split a document payload into chunks that fit the model's context window.

Used for frontend pages, technical docs and API endpoint groups alike. A payload that fits in
the budget comes back unchanged (one chunk). A bigger one is cut at natural boundaries:

  * lists  -> split between items (e.g. endpoints, sections, form fields)
  * dicts  -> split between keys
  * long strings -> split between paragraphs / lines
  * a single item that is still too big is split inside (recursively)

Small "meta" fields (title, route, category, base path, ...) are repeated in every chunk so each
part is understandable on its own. Fields listed in `first_only` (e.g. screenshots) go to chunk 1.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .config import logger

CHARS_PER_TOKEN = 3          # conservative for JSON (typical English prose is ~4)
LONG_STRING_CHARS = 2000     # strings longer than this may be split between paragraphs
SMALL_CONTAINER_TOKENS = 300 # lists/dicts smaller than this are repeated as "meta" in every chunk


def estimate_tokens(value: Any) -> int:
    # measured the way the prompt is built (indent=2), so the estimate is not too optimistic
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return len(text) // CHARS_PER_TOKEN + 1


def _prune(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if v not in ("", [], {}, None)}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def _split_text(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        for line in para.split("\n"):
            if len(line) <= max_chars:
                pieces.append(line)
            else:
                pieces.extend(line[i:i + max_chars] for i in range(0, len(line), max_chars))
    return [p for p in pieces if p.strip()]


def _fragments(value: Any, max_tokens: int) -> list[Any]:
    """Cut ONE oversized value into smaller values of the same type, each within max_tokens."""
    if estimate_tokens(value) <= max_tokens:
        return [value]
    if isinstance(value, str):
        return _split_text(value, max_tokens * CHARS_PER_TOKEN)
    if isinstance(value, list):
        out: list[Any] = []
        group: list[Any] = []
        size = 2
        for item in value:
            t = estimate_tokens(item)
            if t > max_tokens:
                if group:
                    out.append(group)
                    group, size = [], 2
                out.extend([frag] for frag in _fragments(item, max_tokens))
                continue
            if group and size + t > max_tokens:
                out.append(group)
                group, size = [], 2
            group.append(item)
            size += t
        if group:
            out.append(group)
        return out
    if isinstance(value, dict):
        out = []
        group_d: dict[str, Any] = {}
        size = 2
        for k, v in value.items():
            t = estimate_tokens({k: v})
            if t > max_tokens:
                if group_d:
                    out.append(group_d)
                    group_d, size = {}, 2
                if isinstance(v, (str, list, dict)):
                    out.extend({k: frag} for frag in _fragments(v, max_tokens - 20))
                else:
                    out.append({k: v})
                continue
            if group_d and size + t > max_tokens:
                out.append(group_d)
                group_d, size = {}, 2
            group_d[k] = v
            size += t
        if group_d:
            out.append(group_d)
        return out
    return [value]


def split_payload(
    payload: dict[str, Any],
    budget_tokens: int,
    *,
    max_list_items: dict[str, int] | None = None,
    first_only: tuple[str, ...] = ("screenshots",),
) -> list[dict[str, Any]]:
    """Return one or more payloads, each estimated to fit in `budget_tokens`."""
    payload = _prune(payload)
    caps = max_list_items or {}
    over_cap = any(isinstance(payload.get(k), list) and len(payload[k]) > n for k, n in caps.items())
    if estimate_tokens(payload) <= budget_tokens and not over_cap:
        return [payload]

    meta: dict[str, Any] = {}
    first_meta: dict[str, Any] = {}
    # (field, kind, key-or-None, value)
    atoms: list[tuple[str, str, str | None, Any]] = []
    for key, val in payload.items():
        if key in first_only:
            first_meta[key] = val
        elif isinstance(val, list) and (key in caps or estimate_tokens(val) > SMALL_CONTAINER_TOKENS):
            atoms += [(key, "list", None, item) for item in val]
        elif isinstance(val, dict) and estimate_tokens(val) > SMALL_CONTAINER_TOKENS:
            atoms += [(key, "dict", k, v) for k, v in val.items()]
        elif isinstance(val, str) and len(val) > LONG_STRING_CHARS:
            atoms += [(key, "text", None, p) for p in _split_text(val, LONG_STRING_CHARS)]
        else:
            meta[key] = val

    room = budget_tokens - estimate_tokens(meta)
    floor = max(budget_tokens // 3, 500)
    if room < floor:
        logger.warning("Fixed fields use most of the chunk budget (%d tokens); chunks may exceed it.", estimate_tokens(meta))
        room = floor

    # Break any single atom that is itself too big
    sized: list[tuple[str, str, str | None, Any]] = []
    for field, kind, k, v in atoms:
        if estimate_tokens(v) <= room:
            sized.append((field, kind, k, v))
        elif kind == "dict":
            if isinstance(v, (str, list, dict)):
                frags = _fragments(v, room)
                if len(frags) == 1:
                    sized.append((field, kind, k, frags[0]))
                else:
                    sized += [(field, kind, f"{k} (part {i})", frag) for i, frag in enumerate(frags, 1)]
            else:
                sized.append((field, kind, k, v))
        else:
            sized += [(field, kind, k, frag) for frag in _fragments(v, room)]

    # Greedy, order-preserving packing (also honours per-list item caps)
    chunks: list[list[tuple[str, str, str | None, Any]]] = []
    cur: list[tuple[str, str, str | None, Any]] = []
    used = 0
    counts: dict[str, int] = {}
    for atom in sized:
        field, _, _, v = atom
        t = estimate_tokens(v)
        cap = caps.get(field)
        if cur and (used + t > room or (cap and counts.get(field, 0) >= cap)):
            chunks.append(cur)
            cur, used, counts = [], 0, {}
        cur.append(atom)
        used += t
        counts[field] = counts.get(field, 0) + 1
    if cur:
        chunks.append(cur)
    if not chunks:
        return [payload]

    result: list[dict[str, Any]] = []
    for idx, chunk_atoms in enumerate(chunks):
        out: dict[str, Any] = {}
        for key in payload.keys():
            if key in meta:
                out[key] = meta[key]
            elif key in first_meta:
                if idx == 0:
                    out[key] = first_meta[key]
            else:
                mine = [a for a in chunk_atoms if a[0] == key]
                if not mine:
                    continue
                kind = mine[0][1]
                if kind == "list":
                    out[key] = [a[3] for a in mine]
                elif kind == "dict":
                    out[key] = {a[2]: a[3] for a in mine}
                else:
                    out[key] = "\n\n".join(a[3] for a in mine)
        result.append(out)
    logger.info("Split a large payload into %d chunk(s) (budget %d tokens).", len(result), budget_tokens)
    return result