#!/usr/bin/env python3
"""Watch inbox/ and turn PDF/EPUB files into searchable Markdown.

No network access is used. Original files are copied to original/ after a
successful or partially successful run; generated files go to output/.
"""
from __future__ import annotations

import argparse
import html
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
import posixpath
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parent
INBOX, OUT, ORIGINAL, LOGS = (ROOT / n for n in ("inbox", "output", "original", "logs"))
SUPPORTED = {".pdf", ".epub"}
PDF_ARTICLE_MARKER = "@@PDF_ARTICLE@@"
PDF_AUDIO_SUFFIX_RE = re.compile(r"\.(?:mp3|m4a|aac|wav)\s*[\]】)）]?$", re.I)
PDF_PAGE_LINE_RE = re.compile(r"^第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?$")


def safe_name(s: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", "-", s).strip(" .") or "未命名"


def book_metadata(filename_stem: str) -> tuple[str, str, str]:
    """Return author, clean book name and optional column name."""
    author = "万维钢" if ("万维钢" in filename_stem or "万维刚" in filename_stem) else "未知"
    book = re.split(r"\s+\(", filename_stem, maxsplit=1)[0].strip()
    column = book if "精英日课" in book else ""
    return author, book, column


def clean_and_validate_markdown(content: str, strict_pdf: bool = False) -> str:
    """Normalize generated Markdown and reject common extraction artifacts."""
    content = content.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "")
    lines = [line.rstrip() for line in content.split("\n")]
    cleaned: list[str] = []
    for line in lines:
        if not line and cleaned and not cleaned[-1]:
            continue
        cleaned.append(line)
    result = "\n".join(cleaned).strip() + "\n"
    issues = []
    if re.search(r"[ \t]+$", result, re.M): issues.append("行尾空白")
    if "\f" in result: issues.append("分页符")
    if "\n\n\n" in result: issues.append("连续空行")
    if strict_pdf and re.search(r"(?m)^ {2,}\S", result): issues.append("异常段首缩进")
    if strict_pdf and re.search(r"(?m)[^ ] {3,}[^ ]", result): issues.append("行内大段空白")
    if issues:
        raise RuntimeError("Markdown 排版检查未通过：" + "、".join(issues))
    return result


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.in_heading = False
        self.heading: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.in_heading = True
            self.heading = []
        elif tag.lower() in {"p", "li", "br", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            title = re.sub(r"\s+", " ", "".join(self.heading)).strip()
            if title:
                self.parts.append("\n# " + title + "\n")
            self.in_heading = False
        elif tag.lower() in {"p", "li", "div"}:
            self.parts.append("\n")

    def handle_data(self, data):
        data = html.unescape(data)
        if self.in_heading:
            self.heading.append(data)
        else:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


class EpubBlockParser(HTMLParser):
    """Extract paragraph-like blocks while retaining EPUB anchor ids."""
    BLOCKS = {"p", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "li"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_body = False
        self.block_tag: str | None = None
        self.parts: list[str] = []
        self.ids: list[str] = []
        self.pending_ids: list[str] = []
        self.blocks: list[tuple[list[str], str]] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag == "body":
            self.in_body = True
            return
        if not self.in_body:
            return
        anchor = attrs.get("id") or attrs.get("name")
        if tag in self.BLOCKS and self.block_tag is None:
            self.block_tag, self.parts = tag, []
            self.ids = self.pending_ids[:]
            self.pending_ids.clear()
            if anchor:
                self.ids.append(anchor)
        elif anchor:
            if self.block_tag:
                self.ids.append(anchor)
            else:
                self.pending_ids.append(anchor)
        if tag == "br" and self.block_tag:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "body":
            self.in_body = False
        if self.block_tag == tag:
            value = re.sub(r"[ \t\u00a0]+", " ", "".join(self.parts))
            value = re.sub(r"\n{2,}", "\n", value).strip()
            if value:
                self.blocks.append((list(dict.fromkeys(self.ids)), value))
                self.pending_ids.clear()
            else:
                # EPUBs often place a chapter anchor before a cover/image-only
                # paragraph. Carry that anchor to the next text paragraph.
                self.pending_ids.extend(self.ids)
            self.block_tag, self.parts, self.ids = None, [], []

    def handle_data(self, data):
        if self.in_body and self.block_tag:
            self.parts.append(data)


def xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def epub_chapters(path: Path) -> list[tuple[str, str]]:
    """Split an EPUB by its authoritative navigation map and anchor targets."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = next(e for e in container.iter() if xml_local(e.tag) == "rootfile")
        opf_name = unquote(rootfile.attrib["full-path"])
        opf_base = posixpath.dirname(opf_name)
        opf = ET.fromstring(z.read(opf_name))

        manifest: dict[str, tuple[str, str]] = {}
        for item in opf.iter():
            if xml_local(item.tag) != "item":
                continue
            href = posixpath.normpath(posixpath.join(opf_base, unquote(item.attrib.get("href", ""))))
            manifest[item.attrib.get("id", "")] = (href, item.attrib.get("media-type", ""))
        spine_ids = [e.attrib.get("idref", "") for e in opf.iter() if xml_local(e.tag) == "itemref"]
        spine = [manifest[i][0] for i in spine_ids if i in manifest and manifest[i][1] in {"application/xhtml+xml", "text/html"}]
        excluded_files: set[str] = set()
        for ref in opf.iter():
            if xml_local(ref.tag) == "reference" and ref.attrib.get("type", "").lower() in {"toc", "cover"}:
                href = unquote(ref.attrib.get("href", "")).partition("#")[0]
                excluded_files.add(posixpath.normpath(posixpath.join(opf_base, href)))
        for item_id, (href, _) in manifest.items():
            if item_id.lower() in {"titlepage", "cover"}:
                excluded_files.add(href)

        ncx_candidates = [v[0] for v in manifest.values() if v[1] == "application/x-dtbncx+xml"]
        if not ncx_candidates:
            raise RuntimeError("EPUB 没有可用的 NCX 目录，无法可靠按章节拆分")
        ncx_name = ncx_candidates[0]
        ncx_base = posixpath.dirname(ncx_name)
        ncx = ET.fromstring(z.read(ncx_name))
        targets: list[tuple[str, str | None, str, int]] = []

        def walk(node, depth=0):
            for child in node:
                if xml_local(child.tag) != "navPoint":
                    continue
                label = ""
                src = ""
                for desc in child.iter():
                    if xml_local(desc.tag) == "text" and not label:
                        label = "".join(desc.itertext()).strip()
                    elif xml_local(desc.tag) == "content" and not src:
                        src = desc.attrib.get("src", "")
                if label and src:
                    href, _, fragment = unquote(src).partition("#")
                    href = posixpath.normpath(posixpath.join(ncx_base, href))
                    targets.append((href, fragment or None, label, depth))
                walk(child, depth + 1)

        nav_map = next((e for e in ncx.iter() if xml_local(e.tag) == "navMap"), None)
        if nav_map is None:
            raise RuntimeError("EPUB 的 NCX 目录缺少 navMap")
        walk(nav_map)

        by_file: dict[str, list[tuple[str | None, str, int]]] = {}
        for href, fragment, label, depth in targets:
            by_file.setdefault(href, []).append((fragment, label, depth))

        chapters: list[tuple[str, str]] = []
        current_title: str | None = None
        current_depth = 0
        current_parts: list[str] = []

        def emit():
            nonlocal current_parts
            body = "\n\n".join(p for p in current_parts if p).strip()
            if current_title and body:
                chapters.append((current_title, body))
            current_parts = []

        for href in spine:
            if href not in names or href in excluded_files:
                continue
            parser = EpubBlockParser()
            parser.feed(z.read(href).decode("utf-8", "ignore"))
            file_targets = by_file.get(href, [])
            start_targets = [(label, depth) for fragment, label, depth in file_targets if fragment is None]
            anchor_targets = {fragment: (label, depth) for fragment, label, depth in file_targets if fragment}
            # Some EPUBs reuse the same fragment id (for example every file
            # uses CHP2). In that case the file itself is the reliable article
            # boundary, and the NCX label is the title.
            file_title = (start_targets or [(label, depth) for _, label, depth in file_targets])
            if file_title:
                emit()
                current_title, current_depth = file_title[0]
            matched_anchor = False
            for ids, block in parser.blocks:
                marker = next((anchor_targets[i] for i in ids if i in anchor_targets), None)
                if marker:
                    emit()
                    current_title, current_depth = marker
                    matched_anchor = True
                # Navigation titles are often repeated as the first visible block.
                if current_title and re.sub(r"\s+", "", block) == re.sub(r"\s+", "", current_title):
                    continue
                current_parts.append(block)
        emit()
        if not chapters:
            # Some Calibre-generated EPUBs contain a valid NCX shell whose
            # navMap only points to the cover. Their real table of contents is
            # printed in the first XHTML file. Recover those entries and use
            # the matching article headings in the spine as boundaries.
            all_blocks: list[str] = []
            for href in spine:
                if href not in names or href in excluded_files:
                    continue
                parser = EpubBlockParser()
                parser.feed(z.read(href).decode("utf-8", "ignore"))
                all_blocks.extend(block for _, block in parser.blocks if block.strip())

            toc_entries: list[str] = []
            for block in all_blocks[:1000]:
                match = re.match(r"^(?:(?:前言|跋)\s*[:：]|\d{1,2}\s+)(.+?)/(?:\d+)$", block.strip())
                if match:
                    title = match.group(1).strip()
                    prefix = block.strip().split("/", 1)[0]
                    if prefix.startswith(("前言", "跋")):
                        title = prefix
                    elif title:
                        title = title
                    if title and title not in toc_entries:
                        toc_entries.append(title)

            def heading_key(value: str) -> str:
                value = re.sub(r"^\d{1,2}\s+", "", value)
                value = re.sub(r"\s+", "", value)
                value = re.sub(r"[^\w\u4e00-\u9fff]", "", value).casefold()
                # A common OCR/Calibre typo in this title is Latin “Al” for
                # “AI”; normalize it for heading matching only.
                return value.replace("al", "ai")

            recovered: list[tuple[int, str]] = []
            cursor = 30  # Skip cover, metadata and the printed TOC itself.
            for title in toc_entries:
                key = heading_key(title)
                if not key:
                    continue
                found = None
                for index in range(cursor, len(all_blocks)):
                    candidate = heading_key(all_blocks[index])
                    if len(key) >= 8 and (key[:8] in candidate or candidate.startswith(key[:8])):
                        found = index
                        break
                    if len(key) < 8 and (key in candidate or candidate in key):
                        found = index
                        break
                if found is not None:
                    recovered.append((found, title))
                    cursor = found + 1

            for index, (start, title) in enumerate(recovered):
                end = recovered[index + 1][0] if index + 1 < len(recovered) else len(all_blocks)
                body = "\n\n".join(all_blocks[start:end]).strip()
                if body:
                    chapters.append((title, body))
            if not chapters:
                raise RuntimeError("已读取 EPUB 目录，但没有提取到章节正文")
        return chapters


def epub_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        html_names = [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))]
        # Prefer the OPF spine order when available.
        opfs = [n for n in names if n.lower().endswith(".opf")]
        if opfs:
            opf = opfs[0]
            raw = z.read(opf).decode("utf-8", "ignore")
            ids = re.findall(r"<itemref[^>]+idref=[\"']([^\"']+)", raw, re.I)
            items = dict(re.findall(r"<item[^>]+id=[\"']([^\"']+)[\"'][^>]+href=[\"']([^\"']+)", raw, re.I))
            base = Path(opf).parent
            ordered = []
            for i in ids:
                if i in items:
                    candidate = str((base / items[i]).as_posix())
                    if candidate in names: ordered.append(candidate)
            html_names = ordered or html_names
        chunks = []
        for name in html_names:
            p = TextExtractor(); p.feed(z.read(name).decode("utf-8", "ignore"))
            text = re.sub(r"\n{3,}", "\n\n", p.text()).strip()
            if text: chunks.append(text)
        return "\n\n".join(chunks)


def join_wrapped_line(left: str, right: str) -> str:
    """Join a PDF's visual lines without inserting spaces inside Chinese words."""
    if not left:
        return right
    if not right:
        return left
    a, b = left[-1], right[0]
    # English words/numbers need a space; Chinese characters normally do not.
    add_space = a.isascii() and b.isascii() and (a.isalnum() or a in ",.;:!?)]") and (b.isalnum() or b in "([")
    return left + (" " if add_space else "") + right


def pdf_article_boundaries(raw_lines: list[str]) -> tuple[dict[int, str], set[int], set[int]]:
    """Find article starts from standalone audio filenames in annual-column PDFs."""
    lines = [re.sub(r"[ \t]{2,}", " ", line.strip()) for line in raw_lines]

    def title_key(value: str) -> str:
        return re.sub(r"[^\w]", "", value).casefold()

    audio_indexes: set[int] = set()
    starts: dict[int, str] = {}
    title_indexes: set[int] = set()
    audio_count = 0
    for end, line in enumerate(lines):
        suffix = PDF_AUDIO_SUFFIX_RE.search(line)
        if not suffix:
            continue
        audio_count += 1
        filename = line[:suffix.start()].strip().lstrip("[【(（")
        filename = re.sub(r"\s+", "", filename)
        audio_filename = filename
        piece_indexes = {end}
        j = end - 1
        found_start: int | None = None
        candidate_parts: list[str] = []
        while j >= 0 and len(piece_indexes) < 5:
            line = lines[j]
            if not line or PDF_PAGE_LINE_RE.fullmatch(line):
                j -= 1
                continue
            if j in audio_indexes:
                break
            candidate_parts.insert(0, line.lstrip("[【(（"))
            candidate = "".join(candidate_parts)
            if title_key(candidate) == title_key(audio_filename):
                found_start = j
                break
            piece_indexes.add(j)
            if len(candidate) > 180:
                break
            j -= 1
        if found_start is None:
            # A damaged filename must not cause preceding prose to be consumed
            # as part of the title. Keep the boundary at the extension line.
            starts[end] = audio_filename
            audio_indexes.add(end)
        else:
            starts[found_start] = audio_filename
            title_indexes.add(found_start)
            audio_indexes.update(piece_indexes)

    # Isolated audio references in ordinary books should not switch their
    # chapter splitter into annual-column mode.
    if audio_count < 3:
        return {}, set(), set()
    return starts, title_indexes, audio_indexes


def normalize_pdf_layout(text: str) -> str:
    """Turn fixed-layout PDF lines into clean Markdown paragraphs."""
    paragraphs: list[str] = []
    current = ""
    raw_lines = text.replace("\r", "").replace("\f", "\n").split("\n")
    toc_titles: set[str] = set()
    article_starts, article_title_indexes, audio_indexes = pdf_article_boundaries(raw_lines)
    for raw in raw_lines:
        line = re.sub(r"[ \t]{2,}", " ", raw.strip())
        match = re.match(r"^(.*?)(?:\.{5,})\s*\d+\s*$", line)
        if match:
            toc_titles.add(match.group(1).strip())

    def flush():
        nonlocal current
        if current.strip():
            paragraphs.append(current.strip())
        current = ""

    for raw_index, raw in enumerate(raw_lines):
        raw = raw.rstrip()
        if raw_index in article_starts:
            flush()
            paragraphs.append(f"{PDF_ARTICLE_MARKER}{article_starts[raw_index]}")
            continue
        if raw_index in article_title_indexes or raw_index in audio_indexes:
            continue
        if not raw.strip():
            # PDF files often put a blank extraction line between every printed
            # line, so indentation—not blank lines—determines paragraphs.
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        line = raw.strip()
        line = re.sub(r"[ \t]{2,}", " ", line)
        if PDF_PAGE_LINE_RE.fullmatch(line):
            continue
        separator = bool(re.fullmatch(r"(?:\*\s*){3,}|[-—–]{3,}", line))
        toc_entry = bool(re.search(r"\.{5,}\s*\d+\s*$", line))
        body_title = line in toc_titles and not toc_entry
        numbered_heading = bool(re.fullmatch(r"\d+[.．、]\s*\S.{0,40}", line))

        # In layout output, paragraph first lines retain an x-offset while
        # wrapped continuation lines start at the left text boundary.
        if indent >= 1 or separator or toc_entry or body_title or numbered_heading:
            flush()
        if separator:
            paragraphs.append("***")
            continue
        if toc_entry:
            paragraphs.append(line)
            continue
        if body_title or numbered_heading:
            paragraphs.append(line)
            continue
        current = join_wrapped_line(current, line)
    flush()
    return "\n\n".join(paragraphs).strip()


def pdf_text(path: Path, work: Path) -> tuple[str, str]:
    txt = work / (path.stem + ".txt")
    result = subprocess.run(["pdftotext", "-layout", str(path), str(txt)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "pdftotext 处理失败")
    text = txt.read_text(errors="ignore")
    # A scanned PDF normally yields almost no text. Use OCRmyPDF if installed.
    method = "PDF text"
    chars = len(re.sub(r"\s", "", text))
    pages = 1
    if shutil.which("pdfinfo"):
        info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True)
        match = re.search(r"(?mi)^Pages:\s*(\d+)", info.stdout)
        if match:
            pages = max(1, int(match.group(1)))
    # 扫描件通常提取不到文字；按页判断也能发现图文混合型 PDF。
    needs_ocr = chars < max(100, pages * 40)
    if needs_ocr:
        if not shutil.which("ocrmypdf"):
            raise RuntimeError(
                f"检测为扫描件（{pages} 页，仅提取到 {chars} 字符），"
                "但尚未安装 OCR：brew install ocrmypdf tesseract-lang"
            )
        ocr = work / (path.stem + "-ocr.pdf")
        ocr.unlink(missing_ok=True)
        subprocess.run(
            ["ocrmypdf", "-l", "chi_sim+eng", "--skip-text", "--deskew", "--rotate-pages", str(path), str(ocr)],
            check=True,
        )
        subprocess.run(["pdftotext", "-layout", str(ocr), str(txt)], check=True)
        text, method = txt.read_text(errors="ignore"), f"OCR（自动检测：{pages} 页/{chars} 原始字符）"
    return normalize_pdf_layout(text), method


def split_chapters(text: str) -> list[tuple[str, str]]:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    # Prefer confirmed article boundaries for annual-column PDFs. Do not split
    # on Markdown headings or citations within an article in this mode.
    article_markers = list(re.finditer(rf"(?m)^{re.escape(PDF_ARTICLE_MARKER)}(?P<title>.+)$", text))
    if article_markers:
        return [
            (
                match.group("title").strip(),
                text[match.end():article_markers[i + 1].start() if i + 1 < len(article_markers) else len(text)].strip(),
            )
            for i, match in enumerate(article_markers)
        ]

    # Common Chinese chapter headings; retain content if no headings are found.
    pat = re.compile(
        r"(?m)^(?:#{1,6}\s+(?P<md>.{1,100})|"
        r"(?P<cn>(?:第[一二三四五六七八九十百千万0-9]+[章节讲课篇部].{0,80}|序言|前言|发刊词：.{1,100}|结语|附录|问答：.{1,100}|总结：.{1,100}|特别放送：.{1,100}|日课\d{3}\s*[｜|：:].{1,100}|《[^》]{1,70}》\s*(?:\d+|特别放送|完结篇|（完）)[：:｜|].{0,90}).*))$"
    )
    markers: list[tuple[int, int, str]] = []
    # A PDF table of contents is useful metadata: locate those same titles as
    # standalone lines later in the body, instead of treating TOC entries as
    # real chapter starts.
    toc_titles = []
    for line in text.splitlines():
        match = re.match(r"^(.*?)(?:\.{5,})\s*\d+\s*$", line.strip())
        if match and len(match.group(1).strip()) >= 2:
            toc_titles.append(match.group(1).strip())
    for title in toc_titles:
        for match in re.finditer(rf"(?m)^{re.escape(title)}$", text):
            markers.append((match.start(), match.end(), title))
    for match in pat.finditer(text):
        if not re.search(r"\.{5,}", match.group(0)):
            markers.append((match.start(), match.end(), (match.group("md") or match.group("cn")).strip()))
    markers = sorted({(start, end, title) for start, end, title in markers}, key=lambda x: x[0])
    if not markers: return [("全文", text.strip())]
    result = []
    if markers[0][0] > 0 and text[:markers[0][0]].strip(): result.append(("原书目录", text[:markers[0][0]].strip()))
    for i, (_, end, title) in enumerate(markers):
        body = text[end: markers[i + 1][0] if i + 1 < len(markers) else len(text)].strip()
        result.append((title, body))
    return result


def process(path: Path) -> dict:
    stem = safe_name(path.stem)
    author, book, column = book_metadata(path.stem)
    tmp = ROOT / ".tmp"; tmp.mkdir(exist_ok=True)
    if path.suffix.lower() == ".epub":
        chapters, method = epub_chapters(path), "EPUB NCX/spine"
    else:
        text, method = pdf_text(path, tmp)
        chapters = split_chapters(text)
    dest = OUT / stem; dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.md"): old.unlink()
    index = [f"# {stem}", "", f"来源文件：`{path.name}`", f"处理方式：{method}", "", "## 章节", ""]
    for n, (title, body) in enumerate(chapters, 1):
        fn = f"{n:03d}-{safe_name(title)[:80]}.md"
        document_id = hashlib.sha1(f"{path.name}\0{n}\0{title}".encode()).hexdigest()[:16]
        content = (
            "---\n"
            f"author: {json.dumps(author, ensure_ascii=False)}\n"
            f"book: {json.dumps(book, ensure_ascii=False)}\n"
            f"column: {json.dumps(column, ensure_ascii=False)}\n"
            f"title: {json.dumps(title, ensure_ascii=False)}\n"
            f"document_id: {json.dumps(document_id)}\n"
            f"source: {json.dumps(stem, ensure_ascii=False)}\n"
            f"chapter: {json.dumps(f'{n:03d}')}\n"
            f"original_file: {json.dumps(path.name, ensure_ascii=False)}\n"
            f"format: {json.dumps(path.suffix[1:].upper())}\n"
            f"---\n\n# {title}\n\n{body}\n"
        )
        content = clean_and_validate_markdown(content, strict_pdf=path.suffix.lower() == ".pdf")
        (dest / fn).write_text(content, encoding="utf-8")
        label = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        index.append(f"- [{label}](<{fn}>)")
    index_content = clean_and_validate_markdown("\n".join(index) + "\n")
    (dest / "index.md").write_text(index_content, encoding="utf-8")
    ORIGINAL.mkdir(exist_ok=True)
    if path.resolve().parent != ORIGINAL.resolve():
        target = ORIGINAL / path.name
        if target.exists():
            target = ORIGINAL / f"{path.stem}-{int(time.time())}{path.suffix}"
        shutil.move(str(path), str(target))
    return {"file": path.name, "chapters": len(chapters), "method": method, "format_checked": True, "output": str(dest)}


def main():
    ap = argparse.ArgumentParser(description="自动把 inbox 中的 PDF/EPUB 转为分章节 Markdown")
    ap.add_argument("--once", action="store_true", help="处理现有文件后退出")
    ap.add_argument("--interval", type=int, default=3, help="轮询间隔（秒）")
    args = ap.parse_args()
    for d in (INBOX, OUT, ORIGINAL, LOGS): d.mkdir(exist_ok=True)
    seen: dict[str, tuple[int, int]] = {}
    print(f"监听目录：{INBOX}")
    while True:
        for p in sorted(INBOX.iterdir()):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED: continue
            sig = (p.stat().st_size, p.stat().st_mtime_ns)
            if seen.get(str(p)) == sig: continue
            seen[str(p)] = sig
            time.sleep(0.5)  # 等待复制完成
            try:
                report = process(p)
                indexer = ROOT / "indexer.py"
                if indexer.exists():
                    indexed = subprocess.run(
                        [sys.executable, str(indexer)], capture_output=True, text=True, timeout=3600
                    )
                    report["index_update"] = (
                        json.loads(indexed.stdout) if indexed.returncode == 0 and indexed.stdout.strip()
                        else {"error": indexed.stderr.strip() or f"索引进程退出码 {indexed.returncode}"}
                    )
                print(json.dumps({"ok": True, **report}, ensure_ascii=False))
            except Exception as e:
                print(json.dumps({"ok": False, "file": p.name, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        if args.once: break
        time.sleep(max(1, args.interval))


if __name__ == "__main__": main()
