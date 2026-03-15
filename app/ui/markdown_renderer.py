"""Markdown to HTML for chat messages. Uses markdown lib; fenced code gets language class for highlight.js."""
from __future__ import annotations

import re


def markdown_to_html(md: str) -> str:
    """Convert markdown to HTML. Fenced code becomes <pre><code class="language-xxx"> for highlight.js. Sanitizes."""
    if not (md or "").strip():
        return ""
    try:
        import markdown
    except ImportError:
        return _plain_to_html(md)
    html = markdown.markdown(
        md,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        extension_configs={},
    )
    return _sanitize_html(html)


def _plain_to_html(text: str) -> str:
    """Fallback when markdown is not installed: escape and newlines to br."""
    import html as html_module
    return html_module.escape(text).replace("\n", "<br>\n")


# Allow only safe tags from markdown output
_ALLOWED_TAGS = {
    "p", "br", "div", "span", "strong", "em", "b", "i", "u", "s",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_ATTRS = {"href", "src", "alt", "title", "class"}


# Pattern: < that is NOT the start of an allowed tag (or closing tag) -> escape so parser doesn't break
_ALLOWED_TAG_PATTERN = re.compile(
    r"<(?!/?("
    r"p|br|div|span|strong|em|b|i|u|s|"
    r"h[1-6]|ul|ol|li|blockquote|pre|code|a|img|"
    r"table|thead|tbody|tr|th|td"
    r")(?=[\s>/]))",
    re.IGNORECASE,
)


def _sanitize_html(html: str) -> str:
    """Remove script/iframe, allow only safe tags, escape stray < so model output can't break XML/HTML parser."""
    # Remove script and style tags and their content
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<iframe[^>]*>[\s\S]*?</iframe>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"on\w+\s*=", "", html, flags=re.IGNORECASE)
    # Escape any < that isn't start of an allowed tag (avoids "element closed by (status code: 500)" etc.)
    html = _ALLOWED_TAG_PATTERN.sub("&lt;", html)
    return html.strip()
