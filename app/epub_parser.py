"""
Epub TOC parser for omnibus books.

Supports EPUB2 (toc.ncx) and EPUB3 (nav document).
Handles two NCX structures found in real-world omnibus epubs:
  - Nested: top-level navPoints with child navPoints = child books
  - Flat:   all navPoints at same depth, book titles mixed with chapter entries
"""
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from lxml import etree

# ---------------------------------------------------------------------------
# Patterns used to identify front/back matter and chapter entries
# ---------------------------------------------------------------------------

# Entries to exclude entirely when scanning for child books
_FRONT_BACK_MATTER = re.compile(
    r"^\s*(copyright|title\s*page|cover|contents|table\s*of\s*contents"
    r"|foreword|preface|introduction|acknowledgements?|dedication"
    r"|about\s*the\s*author|also\s*by|newsletter|mailing\s*list"
    r"|free\s*book|free\s*harem|patreon|discord|facebook|twitter"
    r"|instagram|support|connect\s*with|join\s*(the|my)|follow"
    r"|sign\s*up|bonus|excerpt|sample|preview"
    r"|want\s*more|check\s*this\s*out|more\s*from|more\s*stories"
    r"|get\s*(it|them|your)|claim\s*your"
    r"|(final\s+)?status\s*sheet|character\s*sheet|stat\s*sheet"
    r"|links?|resources|connect|social\s*media)\b",
    re.IGNORECASE,
)

# Entries that look like chapter/section entries (not book titles)
_CHAPTER_LIKE = re.compile(
    r"^\s*(chapter|ch\.|prologue|epilogue|interlude|part|section|volume"
    r"|appendix|glossary|afterword|author.s\s*note)\b",
    re.IGNORECASE,
)

# Entries whose title is just a bare number ("1", "2", ...) are chapter entries
# in omnibuses that don't prefix chapters with the word "Chapter".
_BARE_NUMBER = re.compile(r"^\s*\d+\s*$")

# NCX namespace
_NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"

# EPUB3 nav namespace
_OPS_NS = "http://www.idpf.org/2007/ops"


@dataclass
class NavPoint:
    title: str
    href: str          # path within the ZIP (fragment stripped)
    full_href: str     # original href including fragment
    play_order: int    # doubles as page number in calibre-generated epubs
    children: list = field(default_factory=list)


class EpubParser:
    def __init__(self, file_bytes: bytes):
        self._zip = zipfile.ZipFile(io.BytesIO(file_bytes))

    def parse(self) -> dict:  # noqa: C901
        opf_path = self._find_opf_path()
        opf_dir = posixpath.dirname(opf_path)

        title, epub_version, toc_href, spine = self._parse_opf(opf_path, opf_dir)

        # Resolve toc path
        if opf_dir:
            toc_path = posixpath.join(opf_dir, toc_href) if toc_href else None
        else:
            toc_path = toc_href

        # Parse TOC
        if epub_version.startswith("3") and toc_path:
            nav_points, total_pages_ncx = self._parse_nav(toc_path, opf_dir)
        elif toc_path and toc_path in self._zip.namelist():
            nav_points, total_pages_ncx = self._parse_ncx(toc_path)
        else:
            # Try to find toc.ncx anywhere
            candidates = [n for n in self._zip.namelist() if n.endswith("toc.ncx")]
            if candidates:
                nav_points, total_pages_ncx = self._parse_ncx(candidates[0])
            else:
                return {
                    "omnibus_title": title,
                    "total_pages": 0,
                    "page_count_method": "none",
                    "child_books": [],
                    "error": "No TOC file found in epub",
                }

        if total_pages_ncx > 0:
            # NCX has real page metadata — playOrder values are page numbers
            page_count_method = "ncx-metadata"
            total_pages = total_pages_ncx
            child_books = self._extract_child_books(nav_points, title, total_pages)
            toc_entries = self._build_toc_entries(
                nav_points, lambda np: np.play_order, page_cutoff=total_pages
            )
        else:
            # No page metadata — estimate via word count across the spine
            page_count_method = "word-count-estimate"
            total_pages, child_books, toc_entries = self._estimate_pages_from_spine(
                nav_points, title, spine
            )

        return {
            "omnibus_title": title,
            "total_pages": total_pages,
            "page_count_method": page_count_method,
            "child_books": child_books,
            # Flat list of every TOC entry, each flagged with the parser's
            # best guess at whether it starts a child book. The UI lets the
            # user override these flags when auto-detection gets it wrong.
            "toc_entries": toc_entries,
        }

    # ------------------------------------------------------------------
    # OPF / container parsing
    # ------------------------------------------------------------------

    def _find_opf_path(self) -> str:
        container = self._zip.read("META-INF/container.xml")
        root = etree.fromstring(container)
        # namespace-agnostic search
        for el in root.iter():
            if el.tag.endswith("}rootfile") or el.tag == "rootfile":
                path = el.get("full-path")
                if path:
                    return path
        raise ValueError("Cannot find rootfile in META-INF/container.xml")

    def _parse_opf(self, opf_path: str, opf_dir: str) -> tuple:
        """Returns (title, epub_version, toc_href, spine_paths)."""
        raw = self._zip.read(opf_path)
        root = etree.fromstring(raw)

        # Detect namespace
        ns = {}
        tag = root.tag
        if "{" in tag:
            ns_uri = tag[1:tag.index("}")]
            ns["opf"] = ns_uri
            ns["dc"] = "http://purl.org/dc/elements/1.1/"

        def find_text(xpath):
            els = root.xpath(xpath, namespaces=ns) if ns else root.findall(xpath)
            if els:
                el = els[0]
                return (el.text or "").strip()
            return ""

        # Title
        if ns:
            title = find_text("//dc:title")
        else:
            title = find_text(".//{http://purl.org/dc/elements/1.1/}title")
        if not title:
            title = "Unknown Title"

        # EPUB version
        epub_version = root.get("version", "2.0")

        # TOC href from manifest/spine
        toc_href = self._find_toc_href(root, ns, epub_version)

        # Build manifest id -> href map, then walk spine itemrefs
        manifest = {}
        for el in root.iter():
            if el.tag.endswith("}item") or el.tag == "item":
                item_id = el.get("id")
                href = el.get("href")
                if item_id and href:
                    # Resolve href relative to OPF dir
                    if opf_dir:
                        resolved = posixpath.normpath(posixpath.join(opf_dir, href))
                    else:
                        resolved = href
                    manifest[item_id] = resolved

        spine = []
        for el in root.iter():
            if el.tag.endswith("}spine") or el.tag == "spine":
                for itemref in el:
                    idref = itemref.get("idref")
                    if idref and idref in manifest:
                        spine.append(manifest[idref])

        return title, epub_version, toc_href, spine

    def _find_toc_href(self, root, ns: dict, epub_version: str) -> Optional[str]:
        """Find the path to toc.ncx (EPUB2) or nav document (EPUB3)."""
        if epub_version.startswith("3"):
            # EPUB3: look for <item properties="nav"> in manifest
            for el in root.iter():
                if el.tag.endswith("}item") or el.tag == "item":
                    props = el.get("properties", "")
                    if "nav" in props.split():
                        return el.get("href")

        # EPUB2: look for toc attribute on <spine>
        for el in root.iter():
            if el.tag.endswith("}spine") or el.tag == "spine":
                toc_id = el.get("toc")
                if toc_id:
                    # Find item with this id in manifest
                    for item in root.iter():
                        if (item.tag.endswith("}item") or item.tag == "item"):
                            if item.get("id") == toc_id:
                                return item.get("href")

        # Fallback: find toc.ncx in zip
        for name in self._zip.namelist():
            if name.endswith("toc.ncx"):
                return name
        return None

    # ------------------------------------------------------------------
    # EPUB2 NCX parsing
    # ------------------------------------------------------------------

    def _parse_ncx(self, ncx_path: str) -> tuple:
        """Returns (list[NavPoint], total_pages)."""
        raw = self._zip.read(ncx_path)
        root = etree.fromstring(raw)
        ncx_dir = posixpath.dirname(ncx_path)

        def ncx(tag):
            return f"{{{_NCX_NS}}}{tag}"

        # Total pages from metadata
        total_pages = 0
        for meta in root.iter(ncx("meta")):
            name = meta.get("name", "")
            if name in ("dtb:totalPageCount", "dtb:maxPageNumber"):
                val = meta.get("content", "0")
                try:
                    n = int(val)
                    if n > total_pages:
                        total_pages = n
                except ValueError:
                    pass

        # Parse navMap
        nav_map = root.find(ncx("navMap"))
        if nav_map is None:
            return [], total_pages

        def parse_nav_point(el) -> NavPoint:
            title_el = el.find(f"{ncx('navLabel')}/{ncx('text')}")
            title = (title_el.text or "").strip() if title_el is not None else ""

            content_el = el.find(ncx("content"))
            raw_href = content_el.get("src", "") if content_el is not None else ""

            # Resolve href relative to NCX dir
            if ncx_dir and not raw_href.startswith("/"):
                resolved = posixpath.normpath(posixpath.join(ncx_dir, raw_href))
            else:
                resolved = raw_href.lstrip("/")

            # Strip fragment for ZIP lookup
            href_no_frag = resolved.split("#")[0]

            try:
                play_order = int(el.get("playOrder", "0"))
            except ValueError:
                play_order = 0

            children = [
                parse_nav_point(child)
                for child in el.iterchildren(ncx("navPoint"))
            ]

            return NavPoint(
                title=title,
                href=href_no_frag,
                full_href=resolved,
                play_order=play_order,
                children=children,
            )

        nav_points = [
            parse_nav_point(el)
            for el in nav_map.iterchildren(ncx("navPoint"))
        ]

        return nav_points, total_pages

    # ------------------------------------------------------------------
    # EPUB3 nav parsing
    # ------------------------------------------------------------------

    def _parse_nav(self, nav_path: str, opf_dir: str) -> tuple:
        """Returns (list[NavPoint], total_pages)."""
        raw = self._zip.read(nav_path)
        root = etree.fromstring(raw)
        nav_dir = posixpath.dirname(nav_path)

        def ops_type(el):
            # Check both namespaced and prefixed forms
            t = el.get(f"{{{_OPS_NS}}}type") or el.get("epub:type") or ""
            return t

        toc_nav = None
        for el in root.iter():
            if el.tag.endswith("}nav") or el.tag == "nav":
                if "toc" in ops_type(el):
                    toc_nav = el
                    break

        if toc_nav is None:
            return [], 0

        def href_to_path(href: str) -> str:
            if nav_dir and not href.startswith("/"):
                resolved = posixpath.normpath(posixpath.join(nav_dir, href))
            else:
                resolved = href.lstrip("/")
            return resolved

        def parse_li(li_el, play_order_counter: list) -> NavPoint:
            title = ""
            href = ""
            full_href = ""
            children = []

            for child in li_el:
                local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if local == "a":
                    # Extract text content (may include nested spans)
                    title = "".join(child.itertext()).strip()
                    raw_href = child.get("href", "")
                    full_href = href_to_path(raw_href)
                    href = full_href.split("#")[0]
                elif local == "span":
                    title = "".join(child.itertext()).strip()
                elif local == "ol":
                    play_order_counter[0] += 1
                    for sub_li in child:
                        sub_local = sub_li.tag.split("}")[-1] if "}" in sub_li.tag else sub_li.tag
                        if sub_local == "li":
                            play_order_counter[0] += 1
                            children.append(parse_li(sub_li, play_order_counter))

            play_order_counter[0] += 1
            return NavPoint(
                title=title,
                href=href,
                full_href=full_href,
                play_order=play_order_counter[0],
                children=children,
            )

        nav_points = []
        counter = [0]
        top_ol = toc_nav.find(".//{*}ol")
        if top_ol is not None:
            for li in top_ol:
                local = li.tag.split("}")[-1] if "}" in li.tag else li.tag
                if local == "li":
                    counter[0] += 1
                    nav_points.append(parse_li(li, counter))

        # EPUB3 doesn't have dtb:totalPageCount; check page-list nav
        total_pages = self._count_pages_from_nav(root)

        return nav_points, total_pages

    def _count_pages_from_nav(self, root) -> int:
        """Count pages from EPUB3 page-list nav."""
        for el in root.iter():
            if el.tag.endswith("}nav") or el.tag == "nav":
                t = el.get(f"{{{_OPS_NS}}}type") or el.get("epub:type") or ""
                if "page-list" in t:
                    count = sum(
                        1 for li in el.iter()
                        if (li.tag.split("}")[-1] if "}" in li.tag else li.tag) == "li"
                    )
                    return count
        return 0

    # ------------------------------------------------------------------
    # Child book detection
    # ------------------------------------------------------------------

    @classmethod
    def _is_book_candidate(cls, title: str) -> bool:
        """True if a flat-TOC entry title could be a child-book boundary.

        Excludes blank titles, front/back matter, chapter-like headings, and
        bare-number chapter entries ("1", "2", ...).
        """
        t = title.strip()
        if not t:
            return False
        if _BARE_NUMBER.match(t):
            return False
        if _FRONT_BACK_MATTER.match(t):
            return False
        if _CHAPTER_LIKE.match(t):
            return False
        return True

    def _build_toc_entries(
        self, nav_points: list, start_page_for, page_cutoff: int = 0
    ) -> list:
        """Flatten nav_points into a list of {title, start_page, is_book_start}.

        ``start_page_for`` maps a NavPoint to its start page (playOrder for
        page-metadata epubs, or a word-count estimate otherwise).

        ``page_cutoff`` (when > 0) suppresses book-start flags for entries that
        start beyond the real page count — these are back-matter promos rather
        than child books.

        ``is_book_start`` is the parser's best guess and seeds the checkboxes
        in the manual book-start editor; the user can override any of them.
        """
        nested = any(np.children for np in nav_points)
        entries = []

        if nested:
            def walk(np, is_top):
                entries.append({
                    "title": np.title,
                    "start_page": start_page_for(np),
                    "is_book_start": bool(is_top and np.children),
                })
                for child in np.children:
                    walk(child, False)

            for np in nav_points:
                walk(np, True)
        else:
            for np in nav_points:
                start_page = start_page_for(np)
                is_book_start = self._is_book_candidate(np.title)
                if page_cutoff and start_page > page_cutoff:
                    is_book_start = False
                entries.append({
                    "title": np.title,
                    "start_page": start_page,
                    "is_book_start": is_book_start,
                })

        return entries

    def _extract_child_books(
        self, nav_points: list, omnibus_title: str, total_pages: int
    ) -> list:
        """
        Detect child books from parsed nav_points.

        Two strategies:
        1. Nested structure: top-level navPoints that HAVE children are child books.
        2. Flat structure: all navPoints at same depth; detect book-boundary entries
           by filtering out front/back matter and chapter-like entries.
        """
        # Strategy 1: nested
        books_with_children = [np for np in nav_points if np.children]
        if books_with_children:
            books = [
                {"title": np.title, "start_page": np.play_order}
                for np in books_with_children
            ]
            return self._number_duplicate_titles(books)

        # Strategy 2: flat — find book-boundary entries
        return self._detect_flat_books(nav_points, omnibus_title, total_pages)

    @staticmethod
    def _number_duplicate_titles(books: list) -> list:
        """When multiple child books share the same title, append Book N suffixes."""
        from collections import Counter
        counts = Counter(b["title"] for b in books)
        duplicates = {t for t, n in counts.items() if n > 1}
        if not duplicates:
            return books
        indices = {t: 0 for t in duplicates}
        result = []
        for book in books:
            if book["title"] in duplicates:
                indices[book["title"]] += 1
                result.append({
                    "title": f"{book['title']} {indices[book['title']]}",
                    "start_page": book["start_page"],
                })
            else:
                result.append(book)
        return result

    def _detect_flat_books(
        self, nav_points: list, omnibus_title: str, total_pages: int
    ) -> list:
        """
        For flat TOC structures, identify entries that represent child book titles.

        A child book entry is one that:
        - Does NOT match front/back matter patterns
        - Does NOT match chapter-like patterns
        - Appears to be a title (possibly containing series name or number)
        """
        # Collect entries that could be book boundaries.
        # Exclude entries beyond total_pages (back-matter promotional content).
        book_entries = []
        for np in nav_points:
            if not self._is_book_candidate(np.title):
                continue
            if total_pages and np.play_order > total_pages:
                continue
            book_entries.append(np)

        if not book_entries:
            return []

        # If only one unique non-chapter entry was found, it's probably the
        # omnibus itself (title page). Return empty — we can't distinguish books.
        if len(book_entries) == 1:
            title = book_entries[0].title
            # Check if it looks like a book title (not just a chapter heading)
            if not re.search(r"\d", title):
                return []

        # Check if the first chapter entry (Chapter 1 / Prologue) appears BEFORE
        # the first book_entry — that means Book 1 has no explicit title entry.
        first_chapter = next(
            (
                np for np in nav_points
                if re.match(r"^\s*(chapter\s*(1|one)|prologue)\b", np.title, re.IGNORECASE)
            ),
            None,
        )
        first_book_entry = book_entries[0] if book_entries else None

        results = []

        if (
            first_chapter is not None
            and first_book_entry is not None
            and first_chapter.play_order < first_book_entry.play_order
        ):
            # Book 1 has no explicit title entry; derive its name from siblings
            book1_title = self._infer_book1_title_from_siblings(
                [e.title for e in book_entries], omnibus_title
            )
            results.append({"title": book1_title, "start_page": first_chapter.play_order})

        for np in book_entries:
            results.append({"title": np.title, "start_page": np.play_order})

        return results

    @staticmethod
    def _infer_book1_title_from_siblings(
        sibling_titles: list, omnibus_title: str
    ) -> str:
        """
        Derive Book 1 title from the sibling book entries.

        Strategy: find the longest common prefix shared by all sibling titles
        (e.g. "Battle Mage 2", "Battle Mage 3" -> prefix "Battle Mage").
        Falls back to stripping qualifier words from the omnibus title.
        """
        if sibling_titles:
            # Strip trailing numbers/words from each sibling to find base name
            bases = []
            for t in sibling_titles:
                base = re.sub(r"\s+\d+\s*$", "", t).strip()
                if base and base != t:
                    bases.append(base)
            if bases and len(set(bases)) == 1:
                return bases[0]

            # Fallback: common prefix of all sibling titles (word-level)
            words_list = [t.split() for t in sibling_titles]
            if words_list:
                prefix_words = words_list[0]
                for words in words_list[1:]:
                    prefix_words = [
                        w for w, v in zip(prefix_words, words) if w == v
                    ]
                if prefix_words:
                    return " ".join(prefix_words)

        # Last resort: strip omnibus qualifier from the omnibus title
        cleaned = re.sub(
            r"\s*[:\-–—]\s*(omnibus|books?\s*\d|complete\s*series|the\s*complete).*$",
            "",
            omnibus_title,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"\s*\((books?\s*\d.*|omnibus.*|complete.*)\)\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        return cleaned if cleaned else "Book 1"

    # ------------------------------------------------------------------
    # Word count fallback (epubs with no page metadata)
    # ------------------------------------------------------------------

    _TAG_RE = re.compile(r"<[^>]+>")
    _WORDS_PER_PAGE = 250

    def _estimate_pages_from_spine(
        self, nav_points: list, omnibus_title: str, spine: list
    ) -> tuple:
        """
        Estimate total pages and child book start pages via word count.

        Returns (total_pages, child_books, toc_entries).
        """
        zip_names = set(self._zip.namelist())

        # Word count per spine file (in order)
        spine_word_counts = []
        for path in spine:
            if path in zip_names:
                try:
                    raw = self._zip.read(path).decode("utf-8", errors="replace")
                    text = self._TAG_RE.sub(" ", raw)
                    spine_word_counts.append((path, len(text.split())))
                except Exception:
                    spine_word_counts.append((path, 0))

        total_words = sum(wc for _, wc in spine_word_counts)
        total_pages = max(1, round(total_words / self._WORDS_PER_PAGE))

        # Build a lookup: spine file path -> cumulative word count before that file
        cumulative = 0
        words_before = {}
        for path, wc in spine_word_counts:
            words_before[path] = cumulative
            cumulative += wc

        def words_to_page(words: int) -> int:
            return max(1, round(words / self._WORDS_PER_PAGE) + 1)

        def find_words_before(href: str) -> int:
            # Try exact match first, then basename match
            if href in words_before:
                return words_before[href]
            base = posixpath.basename(href)
            for path, wc in words_before.items():
                if posixpath.basename(path) == base:
                    return wc
            return 0

        # Auto-detected child books (href -> page via word count)
        child_books = self._map_child_books_to_pages(
            nav_points, omnibus_title, words_before, total_words, total_pages
        )

        # Full flat TOC for the manual book-start editor
        toc_entries = self._build_toc_entries(
            nav_points,
            lambda np: words_to_page(find_words_before(np.href)),
        )

        return total_pages, child_books, toc_entries

    def _map_child_books_to_pages(
        self,
        nav_points: list,
        omnibus_title: str,
        words_before: dict,
        total_words: int,
        total_pages: int,
    ) -> list:
        """
        Like _extract_child_books but uses word count to assign start pages
        instead of playOrder.
        """
        def words_to_page(words: int) -> int:
            return max(1, round(words / self._WORDS_PER_PAGE) + 1)

        def find_words_before(href: str) -> int:
            # Try exact match first, then basename match
            if href in words_before:
                return words_before[href]
            base = posixpath.basename(href)
            for path, wc in words_before.items():
                if posixpath.basename(path) == base:
                    return wc
            return 0

        books_with_children = [np for np in nav_points if np.children]
        if books_with_children:
            books = [
                {
                    "title": np.title,
                    "start_page": words_to_page(find_words_before(np.href)),
                }
                for np in books_with_children
            ]
            return self._number_duplicate_titles(books)

        # Flat structure — filter same as _detect_flat_books
        book_entries = [np for np in nav_points if self._is_book_candidate(np.title)]

        if not book_entries:
            return []

        first_chapter = next(
            (
                np for np in nav_points
                if re.match(r"^\s*(chapter\s*(1|one)|prologue)\b", np.title, re.IGNORECASE)
            ),
            None,
        )
        first_book_entry = book_entries[0]

        results = []
        if (
            first_chapter is not None
            and first_chapter.play_order < first_book_entry.play_order
        ):
            book1_title = self._infer_book1_title_from_siblings(
                [e.title for e in book_entries], omnibus_title
            )
            results.append({
                "title": book1_title,
                "start_page": words_to_page(find_words_before(first_chapter.href)),
            })

        for np in book_entries:
            results.append({
                "title": np.title,
                "start_page": words_to_page(find_words_before(np.href)),
            })

        return results
