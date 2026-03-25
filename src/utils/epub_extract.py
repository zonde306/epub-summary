import os.path
import zipfile

import lxml.etree
import lxml.html

from epub2yaml.domain.models import Chapter
from epub2yaml.utils.hashing import sha256_text

HTML_EXTENSION = {".html", ".xhtml", ".htm"}
STRIP_TEXT = " \r\n\t\u3000　"
TOKEN_ESTIMATE_DIVISOR = 4


def extract_epub(epub_path: str) -> list[Chapter]:
    """
    Extract text content from an EPUB file.

    Args:
        epub_path: Path to the EPUB file.

    Returns:
        Ordered chapter models extracted from XHTML/HTML resources.
    """

    file_data: dict[str, bytes] = {}

    with zipfile.ZipFile(epub_path, "r") as archive:
        for file_name in archive.namelist():
            file_data[file_name] = archive.read(file_name)

    results: list[Chapter] = []

    for file_name, file_content in file_data.items():
        _, ext = os.path.splitext(file_name)
        if ext.lower() not in HTML_EXTENSION:
            continue

        title, content = extract_html(file_content)
        normalized_content = normalize_text(content)
        if not normalized_content:
            continue

        chapter_index = len(results)
        results.append(
            Chapter(
                index=chapter_index,
                title=normalize_title(title, chapter_index),
                source_href=file_name,
                content_text=normalized_content,
                content_hash=sha256_text(normalized_content),
                estimated_tokens=estimate_tokens(normalized_content),
            )
        )

    return results


def extract_html(html_content: bytes) -> tuple[str, str]:
    """
    Extract text content from HTML bytes.

    Args:
        html_content: HTML bytes.

    Returns:
        Tuple of (title, content).
    """

    doc = lxml.html.fromstring(html_content)

    for p in doc.xpath(r"//p[@style='opacity:0.4;']"):
        if isinstance(p, lxml.etree._Element) and p.getparent() is not None:
            p.getparent().remove(p)

    for dom in doc.xpath(r"//*[contains(@style,'writing-mode:vertical-rl;')]"):
        if isinstance(dom, lxml.etree._Element):
            dom.attrib["style"] = ""

    title = "Unnamed Chapter"
    if h1 := doc.xpath("//h1"):
        first_title = h1[0].text_content().strip(STRIP_TEXT)
        title = first_title or "Unnamed Chapter"

    contents: list[str] = []

    for p in doc.xpath(r"//p"):
        if not isinstance(p, lxml.etree._Element):
            continue

        if p.attrib.get("style") == "opacity:0.4;":
            continue

        text = p.text_content().strip(STRIP_TEXT)
        if text:
            contents.append(text)

    return title, "\n".join(contents)


def normalize_text(value: str) -> str:
    lines = [line.strip(STRIP_TEXT) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)



def normalize_title(value: str, chapter_index: int) -> str:
    normalized = value.strip(STRIP_TEXT)
    if normalized:
        return normalized
    return f"Chapter {chapter_index + 1}"



def estimate_tokens(value: str) -> int:
    return max(1, (len(value) + TOKEN_ESTIMATE_DIVISOR - 1) // TOKEN_ESTIMATE_DIVISOR)
