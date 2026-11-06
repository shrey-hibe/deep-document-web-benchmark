"""A small HTML element tree for the portal adapters that read pages by class and attribute (stdlib only).

`parse(html)` returns the root Node; `find_all` walks it in document order. Tolerant of the markup portals serve
(unclosed <p>/<li>/<td>, stray end tags): an end tag closes the nearest open element of that name, if any.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "noscript", "svg", "template"}
_SPACE = re.compile(r"\s+")


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: Node | None = None) -> None:
        self.tag, self.attrs, self.children, self.parent = tag, attrs or {}, [], parent

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def iter(self):
        """This node and every element under it, in document order."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed([c for c in node.children if isinstance(c, Node)]))

    def find_all(self, tag: str | None = None, cls: str | None = None, attr: str | None = None, pred=None) -> list[Node]:
        return [n for n in self.iter() if n is not self and (tag is None or n.tag == tag)
                and (cls is None or cls in n.classes) and (attr is None or attr in n.attrs) and (pred is None or pred(n))]

    def find(self, tag: str | None = None, cls: str | None = None, attr: str | None = None, pred=None) -> Node | None:
        found = self.find_all(tag, cls, attr, pred)
        return found[0] if found else None

    def text(self, skip=None) -> str:
        """The text under this node, whitespace collapsed; `skip(node)` leaves a subtree out."""
        parts: list[str] = []

        def walk(node: Node) -> None:
            for c in node.children:
                if isinstance(c, str):
                    parts.append(c)
                elif not (skip and skip(c)):
                    if c.tag in ("br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"):
                        parts.append(" ")
                    walk(c)

        walk(self)
        return _SPACE.sub(" ", "".join(parts)).strip()

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag in SKIP:
                self.skip += 1
            return
        if tag in SKIP:
            self.skip = 1
            return
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        if not self.skip and tag not in SKIP:
            self.cur.children.append(Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        if self.skip:
            if tag in SKIP:
                self.skip -= 1
            return
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        if not self.skip:
            self.cur.children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    try:
        builder.feed(html or "")
        builder.close()
    except Exception:  # a parse error keeps what was read so far
        pass
    return builder.root
