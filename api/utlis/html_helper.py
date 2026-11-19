from html.parser import HTMLParser
from typing import List, Optional, Tuple

class HTMLTextExtractor(HTMLParser):
    """Strips HTML tags, styles, and scripts while preserving text and line breaks."""

    def __init__(self) -> None:
        super().__init__()
        self._pieces: List[str] = []
        self._ignore: bool = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in ("script", "style", "head", "meta", "noscript"):
            self._ignore = True
        elif tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "td"):
            self._pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head", "meta", "noscript"):
            self._ignore = False
        elif tag in ("p", "div", "tr"):
            self._pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignore:
            self._pieces.append(data)

    def get_text(self) -> str:
        return "".join(self._pieces)


def strip_html(html_str: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(html_str)
    return parser.get_text()