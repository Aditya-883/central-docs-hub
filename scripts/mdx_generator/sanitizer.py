"""Post-LLM sanitizer and validator for strict Docusaurus v3 / MDX v3 compilation."""

from __future__ import annotations

import re
import textwrap

FENCE_SPLIT_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")
OUTER_MDX_FENCE_RE = re.compile(r"^```(?:mdx|markdown|md)?[ \t]*\r?\n([\s\S]*?)\r?\n```[ \t]*$", re.IGNORECASE)
FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)")
# /api/v1/sites/{siteId} -> must live in inline code; bare braces crash MDX v3
BARE_PATH_RE = re.compile(r"(?<![`\w\\])(/[A-Za-z0-9_.\-/]*\{[A-Za-z0-9_.\-]+\}[A-Za-z0-9_.\-/{}]*)")
# A "<" that does NOT start one of the standard HTML tags we allow (details, summary, br)
BAD_LT_RE = re.compile(r"<(?!/?(?:details|summary)\b[^<>]*>|br\s*/?>)")
BRACE_RE = re.compile(r"(?<!\\)[{}]")
THEME_IMPORT_RE = re.compile(
    r"^[ \t]*import\s+.+?from\s+['\"]@theme/(?:Tabs|TabItem)['\"];?[ \t]*\r?\n?",
    re.MULTILINE,
)
TAB_ITEM_RE = re.compile(
    r"[ \t]*<TabItem\b([^>]*)>[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*</TabItem>",
    re.IGNORECASE,
)
TABS_WRAPPER_RE = re.compile(r"^[ \t]*</?Tabs\b[^>]*>[ \t]*\r?\n?", re.MULTILINE | re.IGNORECASE)
IMPORT_LINE_RE = re.compile(r"^[ \t]*import\s+.+?from\s+['\"][^'\"]+['\"];?[ \t]*$", re.MULTILINE)

# Docusaurus v3 / v4 strict admonition syntax: `:::warning Title` -> `:::warning[Title]`
ADMONITION_TITLE_RE = re.compile(
    r"^([ \t]*:::(?:note|tip|info|warning|danger|caution))[ \t]+([^\[\r\n]+?)[ \t]*$",
    re.MULTILINE,
)

# Mermaid flowchart sanitizer helpers
MERMAID_SUBGRAPH_RE = re.compile(r"^([ \t]*subgraph[ \t]+)(.+?)[ \t]*$")
# Match node labels like `BE[Backend Services (Python FastAPI & MCP)]` where content is unquoted
MERMAID_RECT_NODE_RE = re.compile(r'(\b[A-Za-z_][A-Za-z0-9_]*)\[([^"\[\]\n][^\[\]\n]*)\]')
# Match cylinder labels like `DB[(PostgreSQL Database)]` where content is unquoted
MERMAID_CYL_NODE_RE = re.compile(r'(\b[A-Za-z_][A-Za-z0-9_]*)\[\(([^"()\[\]\n][^()\[\]\n]*)\)\]')


def yaml_quote(value: str) -> str:
    one_line = " ".join(value.split())
    return '"' + one_line.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fix_admonition_titles(prose: str) -> str:
    """Convert Docusaurus v2 space-separated admonition titles to v3/v4 bracket syntax."""
    return ADMONITION_TITLE_RE.sub(r"\1[\2]", prose)


def _html_prose_to_markdown(prose: str) -> str:
    """Convert common inline/block HTML tags in prose to Markdown while preserving Java generics."""
    text = re.sub(r"<code\b[^>]*>([\s\S]*?)</code>", r"`\1`", prose, flags=re.IGNORECASE)
    text = re.sub(r"<(?:strong|b)\b[^>]*>([\s\S]*?)</(?:strong|b)>", r"**\1**", text, flags=re.IGNORECASE)
    text = re.sub(r"<(?:em|i)\b[^>]*>([\s\S]*?)</(?:em|i)>", r"*\1*", text, flags=re.IGNORECASE)
    text = re.sub(r"<li\b[^>]*>([\s\S]*?)</li>", r"- \1\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:ul|ol)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p\b[^>]*>([\s\S]*?)</p>", r"\1\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _sanitize_mermaid_block(fence: str) -> str:
    """Quote Mermaid subgraph titles and node labels containing parens/ampersands/spaces so Mermaid v11/v12 never crashes."""
    lines = fence.splitlines()
    if len(lines) < 3 or not lines[0].strip().lower().startswith("```mermaid"):
        return fence

    out_lines = [lines[0]]
    sg_counter = 0
    for line in lines[1:-1]:
        # 1. Fix `subgraph Portal (Liferay)` -> `subgraph sg_1["Portal (Liferay)"]`
        sg_match = MERMAID_SUBGRAPH_RE.match(line)
        if sg_match:
            prefix, rest = sg_match.group(1), sg_match.group(2).strip()
            # Already has explicit id["Title"] or quoted "Title"
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*\[".*"\]$', rest) or (rest.startswith('"') and rest.endswith('"')):
                out_lines.append(line)
                continue
            # Single clean identifier with no special chars
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rest):
                out_lines.append(line)
                continue
            sg_counter += 1
            safe_id = re.sub(r"[^A-Za-z0-9_]+", "_", rest).strip("_")[:24] or f"sg_{sg_counter}"
            if safe_id[0].isdigit():
                safe_id = f"sg_{safe_id}"
            safe_title = rest.replace('"', "'")
            out_lines.append(f'{prefix}{safe_id}["{safe_title}"]')
            continue

        # 2. Fix cylinder nodes `DB[(PostgreSQL Database)]` -> `DB[("PostgreSQL Database")]`
        def _cyl_repl(m: re.Match[str]) -> str:
            node_id, label = m.group(1), m.group(2).strip()
            if any(ch in label for ch in " ()&/:,"):
                return f'{node_id}[("{label.replace(chr(34), chr(39))}")]'
            return m.group(0)

        fixed = MERMAID_CYL_NODE_RE.sub(_cyl_repl, line)

        # 3. Fix rectangular nodes `BE[Backend Services (Python FastAPI & MCP)]` -> `BE["..."]`
        def _rect_repl(m: re.Match[str]) -> str:
            node_id, label = m.group(1), m.group(2).strip()
            # Skip cylinder `[(...)]` or stadium `([...)` shapes already handled
            if label.startswith("(") and label.endswith(")"):
                return m.group(0)
            if any(ch in label for ch in "()&/:"):
                return f'{node_id}["{label.replace(chr(34), chr(39))}"]'
            return m.group(0)

        fixed = MERMAID_RECT_NODE_RE.sub(_rect_repl, fixed)
        out_lines.append(fixed)

    out_lines.append(lines[-1])
    return "\n".join(out_lines)


def _escape_text(text: str) -> str:
    text = BRACE_RE.sub(lambda m: "\\" + m.group(0), text)
    return BAD_LT_RE.sub("&lt;", text)


def _escape_prose(prose: str) -> str:
    """Escape { } < in prose while preserving top-level ESM import lines and wrapping bare API paths."""
    prose = _fix_admonition_titles(prose)
    prose = _html_prose_to_markdown(prose)
    lines = prose.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        if IMPORT_LINE_RE.match(line):
            out.append(line)
            continue
        pos = 0
        for match in BARE_PATH_RE.finditer(line):
            out.append(_escape_text(line[pos:match.start()]))
            out.append(f"`{match.group(1)}`")
            pos = match.end()
        out.append(_escape_text(line[pos:]))
    return "".join(out)


def _unwrap_tabs_to_markdown(body: str) -> str:
    """Convert <Tabs>/<TabItem> JSX into pure Markdown blocks so builds never fail on @theme/TabItem."""
    def _repl(match: re.Match[str]) -> str:
        attrs, inner = match.group(1), match.group(2)
        label_match = re.search(r'label\s*=\s*["\']([^"\']+)["\']', attrs)
        value_match = re.search(r'value\s*=\s*["\']([^"\']+)["\']', attrs)
        heading = (label_match.group(1) if label_match else (value_match.group(1) if value_match else "Response")).strip()
        dedented = textwrap.dedent(inner).strip()
        return f"**{heading}**\n\n{dedented}\n"

    body = THEME_IMPORT_RE.sub("", body)
    body = TAB_ITEM_RE.sub(_repl, body)
    body = TABS_WRAPPER_RE.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body


def sanitize_mdx_for_docusaurus_v3(
    raw_mdx: str,
    doc_id: str,
    title: str,
    position: int,
    with_frontmatter: bool = True,
) -> str:
    """Make model output safe for Docusaurus v3: fixed front matter, pure Markdown responses, escaped { } <."""
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
    elif cleaned.startswith("---"):
        body = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
    else:
        body = cleaned

    # 1. Convert <Tabs>/<TabItem> into pure Markdown (avoids @theme/TabItem & JSX brace crashes)
    body = _unwrap_tabs_to_markdown(body)
    # 2. Clean accidental double-backtick wrappers like `` `https://...` ``
    body = re.sub(r"``\s*`([^`\n]+)`\s*``", r"`\1`", body)

    # 3. Escape prose segments and sanitize Mermaid code fences
    parts = FENCE_SPLIT_RE.split(body)
    for idx in range(len(parts)):
        if idx % 2 == 0:
            parts[idx] = _escape_prose(parts[idx])
        elif parts[idx].lstrip().lower().startswith("```mermaid"):
            parts[idx] = _sanitize_mermaid_block(parts[idx])
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

    fences = len(re.findall(r"^[ \t]*```", body, flags=re.MULTILINE))
    if fences % 2:
        errors.append(f"Unbalanced code fences (count={fences}).")
    if re.search(r"^[ \t]{4,}```", body, flags=re.MULTILINE):
        errors.append("Indented code fence (4+ spaces) would break MDX v3.")
    admonitions = len(re.findall(r"^:::", body, flags=re.MULTILINE))
    if admonitions % 2:
        errors.append(f"Unbalanced ':::' admonition blocks (count={admonitions}).")

    for idx, prose in enumerate(FENCE_SPLIT_RE.split(body)):
        if idx % 2:
            continue
        prose_no_imports = IMPORT_LINE_RE.sub("", prose)
        if re.search(r"(?<!\\)[{}]", prose_no_imports) or BAD_LT_RE.search(prose_no_imports):
            errors.append("Unescaped '{', '}' or '<' outside code (would break the MDX build).")
            break
    return errors