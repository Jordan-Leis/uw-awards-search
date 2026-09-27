"""
A very small HTML tree, built on stdlib html.parser.

This exists because the static sources need to be parsed and BeautifulSoup is
not worth adding as a runtime dependency for them: the UW scraper already
carries bs4 + lxml + Playwright only because PeopleSoft will not render without
a browser, and none of that applies to a plain HTML table. Keeping the static
adapters stdlib-only also means they can be tested anywhere Python runs.

It is not a general HTML library. It handles the subset these sources use:
find elements by tag and class, read their text, read attributes.
"""
from html import unescape
from html.parser import HTMLParser

# Elements that never have a closing tag; without this the tree nests forever.
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

# Tags whose content is not text and must not end up in get_text().
OPAQUE_ELEMENTS = {"script", "style", "noscript", "template"}

# Tags that imply a line break when flattening to text.
BLOCK_ELEMENTS = {
    "p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "section", "article", "td", "th", "dt", "dd",
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []      # Node | str
        self.parent = parent

    # -- attributes ------------------------------------------------------
    def get(self, name, default=None):
        return self.attrs.get(name, default)

    @property
    def classes(self):
        return set((self.attrs.get("class") or "").split())

    # -- traversal -------------------------------------------------------
    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def find_all(self, tag=None, class_=None, limit=None):
        """Descendants matching tag and/or class. class_ may be a str or set;
        a node matches when it has ALL the given classes."""
        wanted = {class_} if isinstance(class_, str) else (set(class_) if class_ else None)
        out = []
        for node in self.walk():
            if node is self:
                continue
            if tag and node.tag != tag:
                continue
            if wanted and not wanted <= node.classes:
                continue
            out.append(node)
            if limit and len(out) >= limit:
                break
        return out

    def find(self, tag=None, class_=None):
        found = self.find_all(tag=tag, class_=class_, limit=1)
        return found[0] if found else None

    # -- text ------------------------------------------------------------
    def get_text(self, separator=" "):
        parts = []

        def visit(node):
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                elif child.tag in OPAQUE_ELEMENTS:
                    continue
                else:
                    if child.tag in BLOCK_ELEMENTS:
                        parts.append("\n")
                    visit(child)
                    if child.tag in BLOCK_ELEMENTS:
                        parts.append("\n")

        visit(self)
        text = separator.join(p for p in parts if p)
        lines = [" ".join(line.split()) for line in text.split("\n")]
        return "\n".join(line for line in lines if line)

    def __repr__(self):
        cls = " ".join(sorted(self.classes))
        return f"<{self.tag}{(' .' + cls) if cls else ''}>"


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = Node("[document]")
        self.current = self.root
        self._opaque_depth = 0

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.current)
        self.current.children.append(node)
        if tag in OPAQUE_ELEMENTS:
            self._opaque_depth += 1
        if tag not in VOID_ELEMENTS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, {k: (v or "") for k, v in attrs}, self.current))

    def handle_endtag(self, tag):
        if tag in OPAQUE_ELEMENTS and self._opaque_depth:
            self._opaque_depth -= 1
        if tag in VOID_ELEMENTS:
            return
        # Walk up to the nearest matching open tag. Unmatched closing tags are
        # common in real pages and must not detach the cursor from the tree.
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent or self.root

    def handle_data(self, data):
        if self._opaque_depth:
            return
        if data.strip():
            self.current.children.append(unescape(data))

    def handle_entityref(self, name):
        if not self._opaque_depth:
            self.current.children.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        if not self._opaque_depth:
            self.current.children.append(unescape(f"&#{name};"))


def parse(html_text):
    """Parse HTML into a Node tree. Never raises on malformed markup."""
    builder = _TreeBuilder()
    builder.feed(html_text)
    builder.close()
    return builder.root
