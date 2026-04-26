import re


REQUIRED_HEADING_PATTERNS = [
    re.compile(r"^\d+\s+.+$"),
    re.compile(r"^\d+\.\d+\s+.+$"),
    re.compile(r"^\d+\.\d+\.\d+\s+.+$"),
    re.compile(r"^\d+(\.\d+){1,5}\s+.+$"),
    re.compile(r"^(Chapter|Section|Part|Appendix)\s+[A-Z0-9]+"),
    re.compile(r"^[A-Z][A-Z\s\-\/]{5,}$"),
]

NUMBERING_PREFIX_RE = re.compile(r"^(?P<number>\d+(?:\.\d+){0,5})\s+(?P<title>.+)$")
KEYWORD_PREFIX_RE = re.compile(r"^(Chapter|Section|Part|Appendix)\s+[A-Z0-9]+")
UPPERCASE_HEADING_RE = re.compile(r"^[A-Z][A-Z\s\-\/]{5,}$")
PAGE_NUMBER_RE = re.compile(
    r"^(?:page\s*)?\d{1,5}(?:\s*(?:/|of)\s*\d{1,5})?$",
    re.IGNORECASE,
)
COPYRIGHT_RE = re.compile(r"(copyright|all rights reserved|\(c\)|©)", re.IGNORECASE)
URL_OR_EMAIL_RE = re.compile(r"(https?://|www\.|\S+@\S+\.\S+)", re.IGNORECASE)
FIGURE_TABLE_RE = re.compile(r"^(figure|fig\.|table)\s+\d+", re.IGNORECASE)


def matches_required_heading_pattern(text: str) -> bool:
    return any(pattern.search(text.strip()) for pattern in REQUIRED_HEADING_PATTERNS)
