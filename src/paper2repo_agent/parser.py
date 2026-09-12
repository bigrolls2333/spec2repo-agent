from __future__ import annotations

import re
from pathlib import Path

from paper2repo_agent.schemas import ParsedPaper, PaperSection
from paper2repo_agent.utils import read_text, stable_id, sha256_text

HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
DEFAULT_MAX_SECTION_CHARS = 8000


def infer_source_type(path: Path, explicit: str | None = None) -> str:
    if explicit and explicit != "auto":
        return explicit
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".txt", ".text"}:
        return "text"
    raise ValueError(f"Cannot infer source type for: {path}")


def parse_local_text_file(path: Path, source_type: str, paper_id: str | None = None) -> ParsedPaper:
    text = read_text(path)
    title = path.stem
    resolved_paper_id = paper_id or stable_id("paper", path.name, sha256_text(text))
    sections = split_markdown_sections(text, resolved_paper_id, title)
    return ParsedPaper(
        paper_id=resolved_paper_id,
        title=title,
        raw_text=text,
        sections=sections,
        metadata={"source_type": source_type, "source_path": str(path)},
    )


def parse_markdown_text(
    text: str,
    *,
    paper_id: str,
    title: str | None = None,
    source_path: str | None = None,
) -> ParsedPaper:
    resolved_title = title or "Untitled Paper"
    sections = split_markdown_sections(text, paper_id, resolved_title)
    return ParsedPaper(
        paper_id=paper_id,
        title=resolved_title,
        raw_text=text,
        sections=sections,
        metadata={"source_type": "markdown", "source_path": source_path},
    )


def split_markdown_sections(
    content: str,
    paper_id: str,
    file_title: str,
    max_section_chars: int = DEFAULT_MAX_SECTION_CHARS,
) -> list[PaperSection]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    sections: list[PaperSection] = []
    current_title = file_title
    current_level = 1
    current_lines: list[str] = []
    current_parent: str | None = None
    title_stack: list[tuple[int, str, str]] = []

    def flush() -> None:
        nonlocal current_lines, current_title, current_level, current_parent
        text = "\n".join(current_lines).strip()
        if not text:
            return
        section_id = stable_id("section", paper_id, len(sections), current_title, text[:120])
        section = PaperSection(
            id=section_id,
            title=current_title or file_title,
            level=current_level,
            text=text,
            parent_id=current_parent,
        )
        sections.extend(split_long_section(section, max_section_chars))
        current_lines = []

    for line in lines:
        match = HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            while title_stack and title_stack[-1][0] >= level:
                title_stack.pop()
            current_parent = title_stack[-1][2] if title_stack else None
            current_title = title
            current_level = level
            parent_key = stable_id("section_title", paper_id, level, title, len(sections))
            title_stack.append((level, title, parent_key))
            continue
        current_lines.append(line)

    flush()
    if sections:
        return sections

    fallback = PaperSection(
        id=stable_id("section", paper_id, file_title, normalized[:120]),
        title=file_title,
        level=1,
        text=normalized.strip(),
    )
    return split_long_section(fallback, max_section_chars)


def split_long_section(section: PaperSection, max_chars: int) -> list[PaperSection]:
    if len(section.text) <= max_chars:
        return [section]

    paragraphs = [part.strip() for part in section.text.split("\n\n") if part.strip()]
    chunks: list[PaperSection] = []
    buffer: list[str] = []
    for paragraph in paragraphs:
        candidate = "\n\n".join([*buffer, paragraph]).strip()
        if len(candidate) > max_chars and buffer:
            chunks.append(_make_subsection(section, len(chunks), "\n\n".join(buffer)))
            buffer = [paragraph]
        else:
            buffer.append(paragraph)
    if buffer:
        chunks.append(_make_subsection(section, len(chunks), "\n\n".join(buffer)))
    return chunks or [section]


def _make_subsection(section: PaperSection, index: int, text: str) -> PaperSection:
    return PaperSection(
        id=stable_id("section", section.id, index, text[:120]),
        title=f"{section.title} (part {index + 1})",
        level=section.level,
        text=text,
        parent_id=section.parent_id or section.id,
    )
