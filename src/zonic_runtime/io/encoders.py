"""Encoders of template values embedded in a port's text (``PortBinding.encode``; cases X-03, B-11).

``resolve(value, encode=...)`` hands each value embedded in a longer string to the encoder with
the template's offset; a template that is the whole value is never encoded.

* ``uriComponent``: values after the URL's authority (its path and query) are percent-encoded,
  so a value holding ``../``, ``/``, ``?`` or ``&`` stays inside its segment or parameter. A
  template that is the scheme and host, or the whole base URL, is used as it is.
* ``html``: every embedded value is HTML-escaped, so a value cannot add markup to a builder
  ``htmlTemplate``; the template's own markup is sanitized later by the view kind.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from urllib.parse import quote

#: ``(offset of the template in the string, its text) -> the text to embed``.
Encoder = Callable[[int, str], str]
URI_COMPONENT, HTML = "uriComponent", "html"


def path_start(template: str) -> int:
    """Where a URL template's path begins: values embedded from here on are encoded."""
    scheme = template.find("://")
    first = template.find("{{")
    if scheme != -1 and (first == -1 or scheme < first):
        marks = [i for i in (template.find("/", scheme + 3), template.find("?", scheme + 3)) if i != -1]
        return min(marks) if marks else len(template)
    if first == 0:  # the base URL itself is a variable
        end = template.find("}}")
        return end + 2 if end != -1 else len(template)
    return 0


def encoder(kind: str | None, template: object) -> Encoder | None:
    if kind == URI_COMPONENT and isinstance(template, str):
        start = path_start(template)
        return lambda offset, text: quote(text, safe="") if offset >= start else text
    if kind == HTML:
        return lambda _offset, text: html.escape(text, quote=True)
    return None
