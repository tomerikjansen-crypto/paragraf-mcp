"""
Lovdata Sync - Download and cache Norwegian laws and regulations.

Downloads bulk datasets from Lovdata's free Public Data API,
extracts XML files, and builds a SQLite FTS search index.

API: https://api.lovdata.no/v1/publicData/get/gjeldende-lover.tar.bz2
License: NLOD 2.0
"""

import logging
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Direkte-barn-klasser som bevisst IKKE er innhold (metadata/struktur).
# Brukes kun til aa unngaa stoey i synlighets-loggen i _parse_sections.
_IGNORED_DIRECT_CHILD_CLASSES = {
    "legalArticleHeader",
    "changesToParent",
    "footnote",
    "footnotes",
}

# Innholdsbaerende klasser utenfor legal_p_classes som skal suppleres inn naar
# de ligger som direkte barn (ellers droppes de stille naar ledd allerede er
# fanget). Bekreftet mot korpus 2026-05-30: marginIdArticle (marg-nummererte
# paragrafledd), defaultList (substansielle listeledd), indent (definisjoner),
# centeredP. futureLegalArticle bevisst utelatt (ikke-ikrafttraadt tekst).
_SUPPLEMENT_CONTENT_CLASSES = {
    "marginIdArticle",
    "defaultList",
    "indent",
    "centeredP",
}


# =============================================================================
# Configuration
# =============================================================================

LOVDATA_API_BASE = "https://api.lovdata.no/v1/publicData/get"
LOVDATA_LIST_URL = "https://api.lovdata.no/v1/publicData/list"

DATASETS = {
    "lover": "gjeldende-lover.tar.bz2",
    "forskrifter": "gjeldende-sentrale-forskrifter.tar.bz2",
}

# Default cache directory
DEFAULT_CACHE_DIR = Path(os.getenv("LOVDATA_CACHE_DIR", "/tmp/lovdata-cache"))


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class LawDocument:
    """Parsed law document from XML."""

    dok_id: str  # e.g., "NL/lov/1992-07-03-93"
    ref_id: str  # e.g., "lov/1992-07-03-93"
    title: str
    short_title: str
    date_in_force: str | None
    ministry: str | None
    content: str  # Full text content
    xml_path: Path
    legal_area: str | None = None
    based_on: str | None = None


@dataclass
class LawSection:
    """A specific section (paragraph) of a law."""

    dok_id: str
    section_id: str  # e.g., "3-9"
    title: str | None
    content: str
    address: str | None  # data-absoluteaddress
    char_count: int = 0

    @property
    def estimated_tokens(self) -> int:
        """Estimate token count for this section."""
        return int(len(self.content) / 3.5)


# =============================================================================
# Sync Service
# =============================================================================


class LovdataSyncService:
    """
    Service for syncing Lovdata datasets to local cache.

    Handles downloading, extracting, parsing and indexing of
    Norwegian laws and regulations.
    """

    def __init__(self, cache_dir: Path | None = None):
        """
        Initialize sync service.

        Args:
            cache_dir: Directory for cached data. Defaults to LOVDATA_CACHE_DIR env var.
        """
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.laws_dir = self.cache_dir / "lover"
        self.regulations_dir = self.cache_dir / "forskrifter"
        self.db_path = self.cache_dir / "lovdata.db"
        self.meta_path = self.cache_dir / "sync_meta.json"

        self._ensure_dirs()
        self._init_db()

    def _ensure_dirs(self) -> None:
        """Create cache directories if they don't exist."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.laws_dir.mkdir(exist_ok=True)
        self.regulations_dir.mkdir(exist_ok=True)

    def _init_db(self) -> None:
        """Initialize SQLite database with FTS index.

        Handles both fresh creation and migration of existing databases.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                -- Main documents table
                CREATE TABLE IF NOT EXISTS documents (
                    dok_id TEXT PRIMARY KEY,
                    ref_id TEXT,
                    title TEXT,
                    short_title TEXT,
                    date_in_force TEXT,
                    ministry TEXT,
                    doc_type TEXT,  -- 'lov' or 'forskrift'
                    is_amendment BOOLEAN DEFAULT FALSE,
                    legal_area TEXT,
                    based_on TEXT,
                    is_current INTEGER DEFAULT 1,
                    xml_path TEXT,
                    indexed_at TEXT
                );

                -- Sections table for paragraph lookup
                CREATE TABLE IF NOT EXISTS sections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dok_id TEXT,
                    section_id TEXT,
                    title TEXT,
                    content TEXT,
                    address TEXT,
                    char_count INTEGER DEFAULT 0,
                    FOREIGN KEY (dok_id) REFERENCES documents(dok_id),
                    UNIQUE(dok_id, section_id)
                );

                -- Index for fast section lookup
                CREATE INDEX IF NOT EXISTS idx_sections_dok_section
                ON sections(dok_id, section_id);

                -- Section-level FTS index for full-text search
                CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
                    dok_id,
                    section_id,
                    title,
                    content
                );

                -- Sync metadata
                CREATE TABLE IF NOT EXISTS sync_meta (
                    dataset TEXT PRIMARY KEY,
                    last_modified TEXT,
                    synced_at TEXT,
                    file_count INTEGER
                );
            """)

            # Migration: add char_count column if missing (existing DBs)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sections)").fetchall()}
            if "char_count" not in cols:
                conn.execute("ALTER TABLE sections ADD COLUMN char_count INTEGER DEFAULT 0")

            # Migration: add new document metadata columns if missing
            doc_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
            for col, col_type, default in [
                ("is_amendment", "BOOLEAN", "FALSE"),
                ("legal_area", "TEXT", "NULL"),
                ("based_on", "TEXT", "NULL"),
                ("is_current", "INTEGER", "1"),
            ]:
                if col not in doc_cols:
                    conn.execute(
                        f"ALTER TABLE documents ADD COLUMN {col} {col_type} DEFAULT {default}"
                    )

            # Migration: drop old documents_fts if it exists (replaced by sections_fts)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "documents_fts" in tables:
                conn.execute("DROP TABLE documents_fts")

    # -------------------------------------------------------------------------
    # Download & Extract
    # -------------------------------------------------------------------------

    def sync_all(self, force: bool = False) -> dict[str, dict | int]:
        """
        Sync all datasets (laws and regulations).

        Args:
            force: Force re-download even if up-to-date

        Returns:
            Dict with sync stats per dataset (dict with docs/sections/etc,
            or -1 on failure)
        """
        results = {}

        for dataset_name, filename in DATASETS.items():
            try:
                stats = self.sync_dataset(dataset_name, filename, force=force)
                results[dataset_name] = stats
            except KeyboardInterrupt:
                logger.info(f"Sync interrupted during {dataset_name}")
                break
            except Exception as e:
                logger.error(f"Failed to sync {dataset_name}: {e}")
                results[dataset_name] = -1

        return results

    def sync_dataset(self, dataset_name: str, filename: str, force: bool = False) -> dict:
        """
        Sync a single dataset with streaming download.

        Args:
            dataset_name: Name of dataset ('lover' or 'forskrifter')
            filename: Filename on API server
            force: Force re-download

        Returns:
            Dict with sync stats (docs, sections, elapsed, etc.)
        """
        url = f"{LOVDATA_API_BASE}/{filename}"
        target_dir = self.laws_dir if dataset_name == "lover" else self.regulations_dir

        logger.info(f"Syncing dataset: {dataset_name} from {url}")

        # Check if we need to download
        remote_modified = self._get_remote_last_modified(filename)
        if not force:
            local_modified = self._get_local_last_modified(dataset_name)

            if remote_modified and local_modified and remote_modified <= local_modified:
                logger.info(f"Dataset {dataset_name} is up-to-date")
                count = self._get_indexed_count(dataset_name)
                return {"docs": count, "up_to_date": True}

        # Streaming download to temp file
        dl_start = time.time()
        is_tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()

        with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=True) as tmp:
            logger.info(f"Downloading {filename}...")
            dl_bytes = 0
            with httpx.Client(timeout=300.0) as client:
                with client.stream("GET", url, follow_redirects=True) as response:
                    response.raise_for_status()
                    content_length = int(response.headers.get("content-length", 0))
                    for chunk in response.iter_bytes(chunk_size=65536):
                        tmp.write(chunk)
                        dl_bytes += len(chunk)
                        if is_tty and content_length:
                            pct = dl_bytes * 100 // content_length
                            mb = dl_bytes / 1_048_576
                            print(f"\r  {mb:.1f} MB ({pct}%)", end="", file=sys.stderr)
            if is_tty and content_length:
                print(file=sys.stderr)

            dl_elapsed = time.time() - dl_start
            dl_mb = dl_bytes / 1_048_576
            logger.info(f"Downloaded {dl_mb:.1f} MB in {dl_elapsed:.0f}s")

            tmp.flush()
            tmp.seek(0)

            # Extract XML files
            logger.info(f"Extracting to {target_dir}...")
            file_count = 0
            with tarfile.open(fileobj=tmp, mode="r:bz2") as tar:
                for member in tar:
                    if member.isfile() and member.name.endswith(".xml"):
                        member.name = Path(member.name).name
                        tar.extract(member, target_dir)
                        file_count += 1

        logger.info(f"Extracted {file_count} files, indexing...")
        indexed_count, section_count = self._index_directory(target_dir, dataset_name)

        # Update sync metadata
        self._update_sync_meta(dataset_name, remote_modified, file_count)

        total_elapsed = time.time() - dl_start
        logger.info(
            f"Sync complete: {indexed_count} documents, {section_count} sections in {total_elapsed:.0f}s"
        )
        return {
            "docs": indexed_count,
            "sections": section_count,
            "elapsed": total_elapsed,
        }

    def _get_remote_last_modified(self, filename: str) -> datetime | None:
        """Get lastModified for a dataset file via the list endpoint.

        The Lovdata API does not return Last-Modified on HEAD requests,
        so we use /v1/publicData/list which returns lastModified per file.
        """
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(LOVDATA_LIST_URL)
                response.raise_for_status()
                for entry in response.json():
                    if entry.get("filename") == filename:
                        return datetime.fromisoformat(entry["lastModified"].replace("Z", "+00:00"))
        except Exception as e:
            logger.warning(f"Could not get lastModified for {filename}: {e}")
        return None

    def _get_local_last_modified(self, dataset_name: str) -> datetime | None:
        """Get last sync time for dataset."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT last_modified FROM sync_meta WHERE dataset = ?", (dataset_name,)
            ).fetchone()
            if row and row[0]:
                return datetime.fromisoformat(row[0])
        return None

    def _update_sync_meta(
        self, dataset_name: str, last_modified: datetime | None, file_count: int
    ) -> None:
        """Update sync metadata in database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_meta
                (dataset, last_modified, synced_at, file_count)
                VALUES (?, ?, ?, ?)
            """,
                (
                    dataset_name,
                    last_modified.isoformat() if last_modified else None,
                    datetime.now().isoformat(),
                    file_count,
                ),
            )

    def _get_indexed_count(self, dataset_name: str) -> int:
        """Get count of indexed documents for dataset."""
        doc_type = "lov" if dataset_name == "lover" else "forskrift"
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE doc_type = ?", (doc_type,)
            ).fetchone()
            return row[0] if row else 0

    # -------------------------------------------------------------------------
    # XML Parsing & Indexing
    # -------------------------------------------------------------------------

    def _index_directory(self, directory: Path, dataset_name: str) -> tuple[int, int]:
        """
        Index all XML files in directory using upsert.

        Returns (documents_indexed, sections_indexed) tuple.
        """
        doc_type = "lov" if dataset_name == "lover" else "forskrift"
        indexed = 0
        total_sections = 0
        seen_dok_ids: set[str] = set()
        is_tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        idx_start = time.time()

        xml_files = list(directory.glob("*.xml"))

        with sqlite3.connect(self.db_path) as conn:
            for i, xml_path in enumerate(xml_files):
                try:
                    doc = self._parse_xml(xml_path)
                    if doc:
                        seen_dok_ids.add(doc.dok_id)
                        section_count = self._insert_document(conn, doc, doc_type)
                        indexed += 1
                        total_sections += section_count
                except Exception as e:
                    logger.warning(f"Failed to parse {xml_path.name}: {e}")

                if is_tty and (i + 1) % 100 == 0:
                    elapsed = time.time() - idx_start
                    rate = (i + 1) / elapsed if elapsed > 0 else 0
                    remaining = (len(xml_files) - i - 1) / rate if rate > 0 else 0
                    print(
                        f"\r  {i + 1}/{len(xml_files)} docs ({rate:.0f}/s, ~{remaining:.0f}s left)",
                        end="",
                        file=sys.stderr,
                    )

            if is_tty and len(xml_files) >= 100:
                print(file=sys.stderr)

            # Mark documents not in the latest file as non-current
            self._mark_non_current(conn, doc_type, seen_dok_ids)

            conn.commit()

            # Rebuild FTS index
            self._rebuild_fts_index(conn)

        return indexed, total_sections

    def _mark_non_current(
        self, conn: sqlite3.Connection, doc_type: str, current_dok_ids: set[str]
    ) -> None:
        """Mark documents not in the latest sync file as non-current."""
        if not current_dok_ids:
            return

        placeholders = ",".join("?" * len(current_dok_ids))
        # Mark missing docs as non-current
        result = conn.execute(
            f"UPDATE documents SET is_current = 0 WHERE doc_type = ? AND is_current = 1 AND dok_id NOT IN ({placeholders})",
            [doc_type, *current_dok_ids],
        )
        if result.rowcount:
            logger.info(f"Marked {result.rowcount} {doc_type} documents as non-current")

        # Mark present docs as current (handles "resurrected")
        conn.execute(
            f"UPDATE documents SET is_current = 1 WHERE doc_type = ? AND is_current = 0 AND dok_id IN ({placeholders})",
            [doc_type, *current_dok_ids],
        )

    def _parse_xml(self, xml_path: Path) -> LawDocument | None:
        """
        Parse Lovdata XML/HTML file.

        Uses BeautifulSoup for HTML5-compatible parsing.
        """
        try:
            with open(xml_path, encoding="utf-8") as f:
                content = f.read()

            soup = BeautifulSoup(content, "html.parser")

            # Extract metadata from header
            header = soup.find("header", class_="documentHeader")
            if not header:
                header = soup.find("header")

            dok_id = self._extract_meta(header, "dokid") or xml_path.stem
            ref_id = self._extract_meta(header, "refid") or dok_id
            title = self._extract_meta(header, "title") or ""
            short_title = self._extract_meta(header, "titleShort") or ""
            date_in_force = self._extract_meta(header, "dateInForce")
            ministry = self._extract_ministry(header)

            # Extract main content
            main = soup.find("main", class_="documentBody")
            if not main:
                main = soup.find("main") or soup.find("body")

            full_content = main.get_text(separator="\n", strip=True) if main else ""

            return LawDocument(
                dok_id=dok_id,
                ref_id=ref_id,
                title=title,
                short_title=short_title,
                date_in_force=date_in_force,
                ministry=ministry,
                content=full_content,
                xml_path=xml_path,
                legal_area=self._extract_meta(header, "legalArea"),
                based_on=self._extract_meta(header, "basedOn"),
            )

        except Exception as e:
            logger.error(f"Parse error for {xml_path}: {e}")
            return None

    def _extract_ministry(self, header) -> str | None:
        """
        Extract ministry from header, handling multi-ministry documents.

        Mirrors the logic in supabase_backend.py.
        """
        if not header:
            return None

        import re

        dt = header.find("dt", class_="ministry")
        dd = dt.find_next_sibling("dd") if dt else header.find("dd", class_="ministry")
        if not dd:
            return None

        # Multiple <a> elements → join with "; "
        links = dd.find_all("a")
        if len(links) > 1:
            ministries = [a.get_text(strip=True) for a in links if a.get_text(strip=True)]
            if ministries:
                return "; ".join(ministries)

        # Split concatenated ministries on known pattern
        text = dd.get_text(strip=True)
        if text and "departementet" in text.lower():
            parts = re.split(r"(departementet)(?=[A-ZÆØÅ])", text)
            if len(parts) > 2:
                ministries = []
                i = 0
                while i < len(parts):
                    if i + 1 < len(parts) and parts[i + 1] == "departementet":
                        ministries.append(parts[i] + "departementet")
                        i += 2
                    else:
                        if parts[i].strip():
                            ministries.append(parts[i].strip())
                        i += 1
                if len(ministries) > 1:
                    return "; ".join(ministries)

        return text if text else None

    def _extract_meta(self, header, class_name: str) -> str | None:
        """Extract metadata value from header by class name.

        Handles multi-value fields where multiple <a> or block-level child
        elements are concatenated by BeautifulSoup's get_text() without
        separator.  Uses "; " as delimiter between distinct child elements.
        """
        if not header:
            return None

        # Find dt/dd pair with matching class
        dt = header.find("dt", class_=class_name)
        dd = dt.find_next_sibling("dd") if dt else header.find("dd", class_=class_name)
        if not dd:
            return None

        # If dd contains multiple <a> elements, join them with delimiter
        links = dd.find_all("a")
        if len(links) > 1:
            values = [a.get_text(strip=True) for a in links if a.get_text(strip=True)]
            if values:
                return "; ".join(values)

        return dd.get_text(strip=True) or None

    @staticmethod
    def _is_amendment_title(title: str) -> bool:
        """Check if a document title indicates an amendment law."""
        if not title:
            return False
        t = title.lower()
        return "endring i " in t or "endringer i " in t or "endringslov" in t or "endr. i " in t

    def _insert_document(self, conn: sqlite3.Connection, doc: LawDocument, doc_type: str) -> int:
        """Insert document and its sections into database.

        Returns number of sections inserted.
        """
        is_amendment = self._is_amendment_title(doc.title)

        # Insert/update main document
        conn.execute(
            """
            INSERT OR REPLACE INTO documents
            (dok_id, ref_id, title, short_title, date_in_force, ministry, doc_type,
             is_amendment, xml_path, indexed_at, legal_area, based_on)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                doc.dok_id,
                doc.ref_id,
                doc.title,
                doc.short_title,
                doc.date_in_force,
                doc.ministry,
                doc_type,
                is_amendment,
                str(doc.xml_path),
                datetime.now().isoformat(),
                doc.legal_area,
                doc.based_on,
            ),
        )

        # Parse and upsert sections
        sections = self._parse_sections(doc.xml_path, doc.dok_id)
        for section in sections:
            conn.execute(
                """
                INSERT OR REPLACE INTO sections (dok_id, section_id, title, content, address, char_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    doc.dok_id,
                    section.section_id,
                    section.title,
                    section.content,
                    section.address,
                    section.char_count,
                ),
            )

        return len(sections)

    def _parse_sections(self, xml_path: Path, dok_id: str) -> list[LawSection]:
        """Parse all sections (paragraphs) from XML file."""
        sections = []

        try:
            with open(xml_path, encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")

            # Find all legalArticle elements (paragraphs)
            for article in soup.find_all("article", class_="legalArticle"):
                # Get section ID from legalArticleValue
                value_span = article.find("span", class_="legalArticleValue")
                if not value_span:
                    continue

                section_id = value_span.get_text(strip=True)
                # Clean up section ID (remove § and whitespace)
                section_id = section_id.replace("§", "").strip()
                section_id = " ".join(section_id.split())

                if not section_id:
                    continue

                # Get optional title
                title_span = article.find("span", class_="legalArticleTitle")
                title = title_span.get_text(strip=True) if title_span else None

                # Get content from direct legal-paragraph children.
                # Lovdata XML wraps each numbered ledd in <article
                # class="numberedLegalP">, which may itself contain a nested
                # letter list (ol > li > listArticle > legalP). Iterating DIRECT
                # children (not find_all, which recurses) captures each ledd's
                # full text in one pass and avoids double-counting the nested
                # legalP items. Matching only "legalP" dropped numbered-ledd
                # prose silently - e.g. § 13-5 (1) "200 Bq" (issue #8).
                # defaultP holds free-form prose and tables (e.g. the
                # energy-measure / U-value tables in § 14-5). changesToParent
                # (amendment notes) is intentionally excluded - it is metadata,
                # not binding regulation text.
                legal_p_classes = {
                    "legalP",
                    "numberedLegalP",
                    "listLegalP",
                    "marginIdLegalP",
                    "defaultP",
                }
                content_parts = []
                for child in article.children:
                    if not (hasattr(child, "get") and child.get("class")):
                        continue
                    classes = child.get("class")
                    if isinstance(classes, str):
                        classes = [classes]
                    class_set = set(classes)
                    if class_set & legal_p_classes and "footnote" not in " ".join(classes).lower():
                        text = child.get_text(strip=True)
                        if text:
                            content_parts.append(text)
                    elif class_set & _SUPPLEMENT_CONTENT_CLASSES:
                        # Innholdsbaerende direkte-barn utenfor legal_p_classes
                        # (bekreftet mot korpus). Suppleres inn med dedupe.
                        text = child.get_text(strip=True)
                        if text and text not in content_parts:
                            content_parts.append(text)
                    elif not (class_set & _IGNORED_DIRECT_CHILD_CLASSES):
                        # Ukjent klasse som ikke er fanget og ikke kjent metadata.
                        # Logg paa DEBUG hvis den baerer tekst - saa stille tap
                        # blir synlig uten aa endre parse-resultatet.
                        skipped_text = child.get_text(strip=True)
                        if skipped_text:
                            logger.debug(
                                "Droppet direkte-barn i %s (klasse=%s, %d tegn): %.60s",
                                section_id, ",".join(classes), len(skipped_text), skipped_text,
                            )

                # Include leddfortsettelse: prose continuing a ledd after an
                # inserted list. It is a sibling <p class="leddfortsettelse">
                # and is binding text, but the direct-child loop above skips it
                # (not a legal_p class) and the fallbacks never fire when other
                # ledd were captured. Mirrors supabase_backend.py:668-672.
                for cont in article.find_all("p", class_="leddfortsettelse"):
                    text = cont.get_text(strip=True)
                    if text and text not in content_parts:
                        content_parts.append(text)

                # Fallback 1: no direct legal-paragraph children (unusual
                # nesting) - recurse for any legalP descendants.
                if not content_parts:
                    for ledd in article.find_all("article", class_="legalP"):
                        text = ledd.get_text(strip=True)
                        if text:
                            content_parts.append(text)

                # Fallback 2: still empty - take the whole article text.
                if not content_parts:
                    content_parts.append(article.get_text(strip=True))

                # Get absolute address (cast to str for type safety)
                raw_addr = article.get("data-absoluteaddress") or article.get("id")
                address = str(raw_addr) if raw_addr else None

                content = "\n\n".join(content_parts)
                sections.append(
                    LawSection(
                        dok_id=dok_id,
                        section_id=section_id,
                        title=title,
                        content=content,
                        address=address,
                        char_count=len(content),
                    )
                )

        except Exception as e:
            logger.warning(f"Failed to parse sections from {xml_path}: {e}")

        return sections

    def _rebuild_fts_index(self, conn: sqlite3.Connection) -> None:
        """Rebuild section-level full-text search index."""
        conn.execute("DELETE FROM sections_fts")
        conn.execute("""
            INSERT INTO sections_fts (dok_id, section_id, title, content)
            SELECT dok_id, section_id, COALESCE(title, ''), content
            FROM sections
            WHERE content IS NOT NULL AND content != ''
        """)

    # -------------------------------------------------------------------------
    # Query Methods
    # -------------------------------------------------------------------------

    def get_document(self, dok_id: str) -> dict | None:
        """
        Get document by ID.

        Args:
            dok_id: Document ID (e.g., "NL/lov/1992-07-03-93" or "LOV-1992-07-03-93")
        """
        # Normalize ID format
        normalized = self._normalize_id(dok_id)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM documents WHERE dok_id = ? OR ref_id = ?", (normalized, normalized)
            ).fetchone()

            if row:
                return dict(row)
        return None

    def get_section(self, dok_id: str, section_id: str) -> LawSection | None:
        """
        Get specific section from a document.

        Args:
            dok_id: Document ID
            section_id: Section number (e.g., "3-9")
        """
        normalized = self._normalize_id(dok_id)
        section_id = section_id.replace("§", "").strip()

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # First find the document
            doc = conn.execute(
                "SELECT dok_id FROM documents WHERE dok_id = ? OR ref_id = ? OR short_title = ?",
                (normalized, normalized, dok_id.lower()),
            ).fetchone()

            if not doc:
                doc = self._find_document_row(conn, dok_id)

            if not doc:
                return None

            # Then find the section
            row = conn.execute(
                "SELECT * FROM sections WHERE dok_id = ? AND section_id = ?",
                (doc["dok_id"], section_id),
            ).fetchone()

            if row:
                content = row["content"]
                return LawSection(
                    dok_id=doc["dok_id"],
                    section_id=row["section_id"],
                    title=row["title"],
                    content=content,
                    address=row["address"],
                    char_count=len(content) if content else 0,
                )
        return None

    def search(
        self,
        query: str,
        limit: int = 20,
        exclude_amendments: bool = True,
        ministry_filter: str | None = None,
        doc_type_filter: str | None = None,
        legal_area_filter: str | None = None,
    ) -> list[dict]:
        """
        Full-text search across all sections.

        Args:
            query: Search query
            limit: Maximum results
            exclude_amendments: Exclude amendment laws from results (default True)
            ministry_filter: Filter by ministry (partial match)
            doc_type_filter: Filter by document type ("lov" or "forskrift")
            legal_area_filter: Filter by legal area (partial match)

        Returns:
            List of matching sections with snippets and document metadata
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Build WHERE clause with optional filters
            conditions = ["sections_fts MATCH ?"]
            params: list = [query]

            if exclude_amendments:
                conditions.append("COALESCE(d.is_amendment, 0) = 0")

            if ministry_filter:
                conditions.append("d.ministry LIKE ?")
                params.append(f"%{ministry_filter}%")

            if doc_type_filter:
                conditions.append("d.doc_type = ?")
                params.append(doc_type_filter)

            if legal_area_filter:
                conditions.append("d.legal_area LIKE ?")
                params.append(f"%{legal_area_filter}%")

            where_clause = " AND ".join(conditions)
            params.append(limit)

            rows = conn.execute(
                f"""
                SELECT
                    d.dok_id,
                    d.title,
                    d.short_title,
                    d.doc_type,
                    d.based_on,
                    sf.section_id,
                    snippet(sections_fts, 3, '<mark>', '</mark>', '...', 32) as snippet
                FROM sections_fts sf
                JOIN documents d ON d.dok_id = sf.dok_id
                WHERE {where_clause}
                ORDER BY rank
                LIMIT ?
            """,
                params,
            ).fetchall()

            return [dict(row) for row in rows]

    def find_related_regulations(self, lov_id: str) -> list[dict]:
        """
        Find regulations (forskrifter) based on a given law.

        Args:
            lov_id: Law identifier (will be resolved via _find_document)

        Returns:
            List of dicts with dok_id, title, short_title, based_on, ministry
        """
        doc = self._find_document(lov_id)
        if not doc:
            return []

        actual_id = doc["dok_id"]

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT dok_id, title, short_title, based_on, ministry
                FROM documents
                WHERE based_on LIKE ? AND doc_type = 'forskrift'
                """,
                (f"%{actual_id}%",),
            ).fetchall()

            return [dict(row) for row in rows]

    def list_ministries(self) -> list[str]:
        """
        List all unique ministries across all documents.

        Returns:
            Sorted list of ministry names
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT ministry FROM documents WHERE ministry IS NOT NULL ORDER BY ministry"
            ).fetchall()

            return [row[0] for row in rows if row[0]]

    def list_documents(self, doc_type: str | None = None) -> list[dict]:
        """
        List all indexed documents.

        Args:
            doc_type: Optional filter ('lov' or 'forskrift')
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if doc_type:
                rows = conn.execute(
                    "SELECT dok_id, title, short_title, doc_type FROM documents WHERE doc_type = ? ORDER BY short_title",
                    (doc_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT dok_id, title, short_title, doc_type FROM documents ORDER BY doc_type, short_title"
                ).fetchall()

            return [dict(row) for row in rows]

    def get_sync_status(self) -> dict:
        """Get sync status for all datasets."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM sync_meta").fetchall()

            status = {}
            for row in rows:
                status[row["dataset"]] = {
                    "last_modified": row["last_modified"],
                    "synced_at": row["synced_at"],
                    "file_count": row["file_count"],
                }
            return status

    def is_synced(self) -> bool:
        """Check if any data has been synced."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM sync_meta WHERE file_count > 0").fetchone()
            return row[0] > 0 if row else False

    def list_sections(self, dok_id: str) -> list[dict]:
        """
        List all sections for a document with metadata.

        Returns list of dicts with: section_id, title, char_count, estimated_tokens, address
        Sorted by section_id (natural sort).
        """
        import re

        normalized = self._normalize_id(dok_id)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Find the document first
            doc = conn.execute(
                "SELECT dok_id FROM documents WHERE dok_id = ? OR ref_id = ? OR short_title = ?",
                (normalized, normalized, dok_id.lower()),
            ).fetchone()

            if not doc:
                # Try LIKE match
                doc = self._find_document_row(conn, dok_id)

            if not doc:
                return []

            rows = conn.execute(
                "SELECT section_id, title, char_count, address FROM sections WHERE dok_id = ?",
                (doc["dok_id"],),
            ).fetchall()

            sections = []
            for row in rows:
                char_count = row["char_count"] or 0
                sections.append(
                    {
                        "section_id": row["section_id"],
                        "title": row["title"],
                        "char_count": char_count,
                        "estimated_tokens": int(char_count / 3.5),
                        "address": row["address"],
                    }
                )

            # Natural sort: 1, 1a, 2, 3-1, 3-2, 10, 11
            def sort_key(s):
                section_id = s["section_id"]
                parts = section_id.replace("-", ".").split(".")
                result = []
                for p in parts:
                    match = re.match(r"^(\d+)\s*([a-z]?)$", p.strip(), re.I)
                    if match:
                        result.append((int(match.group(1)), match.group(2).lower()))
                    else:
                        result.append((float("inf"), p.lower()))
                return result

            sections.sort(key=sort_key)
            return sections

    def _find_document(self, identifier: str) -> dict | None:
        """Find document by ID, short_title, or partial match."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            doc = self._find_document_row(conn, identifier)
            return dict(doc) if doc else None

    def _find_document_row(self, conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
        """Find document row by various matching strategies.

        Prioritizes is_current documents (gjeldende > opphevet).
        """
        normalized = self._normalize_id(identifier)

        # Exact match on dok_id or ref_id (prioritize current)
        row = conn.execute(
            "SELECT * FROM documents WHERE dok_id = ? OR ref_id = ? ORDER BY is_current DESC LIMIT 1",
            (normalized, normalized),
        ).fetchone()
        if row:
            return row

        # Short title exact match (case-insensitive, prioritize current)
        row = conn.execute(
            "SELECT * FROM documents WHERE LOWER(short_title) = LOWER(?) ORDER BY is_current DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        if row:
            return row

        # LIKE match on short_title (starts with, prioritize current)
        row = conn.execute(
            "SELECT * FROM documents WHERE short_title LIKE ? ORDER BY is_current DESC LIMIT 1",
            (f"{identifier}%",),
        ).fetchone()
        if row:
            return row

        # LIKE match on short_title (contains, prioritize current)
        row = conn.execute(
            "SELECT * FROM documents WHERE short_title LIKE ? ORDER BY is_current DESC LIMIT 1",
            (f"%{identifier}%",),
        ).fetchone()
        if row:
            return row

        # LIKE match on dok_id (contains, prioritize current)
        row = conn.execute(
            "SELECT * FROM documents WHERE dok_id LIKE ? ORDER BY is_current DESC LIMIT 1",
            (f"%{normalized}%",),
        ).fetchone()
        return row

    def get_section_size(self, dok_id: str, section_id: str) -> dict | None:
        """
        Get section size info without fetching full content.

        Returns:
            Dict with char_count and estimated_tokens, or None
        """
        normalized = self._normalize_id(dok_id)
        section_id = section_id.replace("§", "").strip()

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            doc = conn.execute(
                "SELECT dok_id FROM documents WHERE dok_id = ? OR ref_id = ? OR short_title = ?",
                (normalized, normalized, dok_id.lower()),
            ).fetchone()

            if not doc:
                return None

            row = conn.execute(
                "SELECT char_count, LENGTH(content) as content_len FROM sections WHERE dok_id = ? AND section_id = ?",
                (doc["dok_id"], section_id),
            ).fetchone()

            if not row:
                return None

            char_count = row["char_count"] or row["content_len"] or 0
            return {
                "char_count": char_count,
                "estimated_tokens": int(char_count / 3.5),
            }

    def get_sections_batch(self, dok_id: str, section_ids: list[str]) -> list[LawSection]:
        """
        Fetch multiple sections in a single database call.

        Args:
            dok_id: Document ID or alias
            section_ids: List of section IDs to fetch

        Returns:
            List of LawSection objects (in requested order)
        """
        normalized = self._normalize_id(dok_id)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            doc = conn.execute(
                "SELECT dok_id FROM documents WHERE dok_id = ? OR ref_id = ? OR short_title = ?",
                (normalized, normalized, dok_id.lower()),
            ).fetchone()

            if not doc:
                return []

            # Normalize section IDs
            clean_ids = [s.replace("§", "").strip() for s in section_ids]

            # Use IN clause with parameter placeholders
            placeholders = ",".join("?" * len(clean_ids))
            rows = conn.execute(
                f"SELECT * FROM sections WHERE dok_id = ? AND section_id IN ({placeholders})",
                [doc["dok_id"], *clean_ids],
            ).fetchall()

            # Build lookup for ordering
            sections_dict = {}
            for row in rows:
                content = row["content"]
                sections_dict[row["section_id"]] = LawSection(
                    dok_id=doc["dok_id"],
                    section_id=row["section_id"],
                    title=row["title"],
                    content=content,
                    address=row["address"],
                    char_count=len(content) if content else 0,
                )

            # Return in requested order
            return [sections_dict[sid] for sid in clean_ids if sid in sections_dict]

    def get_chapter_sections(
        self, dok_id: str, chapter_id: str  # noqa: ARG002
    ) -> tuple[dict | None, list[LawSection]]:
        """
        Get all sections belonging to a chapter.

        SQLite backend does not have a structures table, so this always returns
        no results. Chapter lookup is only supported with the Supabase backend.
        """
        return None, []

    def _normalize_id(self, id_str: str) -> str:
        """Normalize document ID to match database format."""
        # Handle various ID formats
        id_upper = id_str.upper()

        if id_upper.startswith("LOV-"):
            # Convert LOV-1992-07-03-93 to lov/1992-07-03-93
            return "lov/" + id_str[4:].lower()
        elif id_upper.startswith("FOR-"):
            return "forskrift/" + id_str[4:].lower()
        elif id_upper.startswith("NL/"):
            return id_str[3:]  # Remove NL/ prefix

        return id_str.lower()


# =============================================================================
# CLI Interface
# =============================================================================


def sync_cli():
    """Command-line interface for syncing Lovdata."""
    import argparse

    parser = argparse.ArgumentParser(description="Sync Lovdata to local cache")
    parser.add_argument("--force", "-f", action="store_true", help="Force re-download")
    parser.add_argument("--cache-dir", "-c", type=Path, help="Cache directory")
    parser.add_argument(
        "--dataset", "-d", choices=["lover", "forskrifter"], help="Sync only specific dataset"
    )

    args = parser.parse_args()

    service = LovdataSyncService(cache_dir=args.cache_dir)

    if args.dataset:
        filename = DATASETS[args.dataset]
        stats = service.sync_dataset(args.dataset, filename, force=args.force)
        print(f"Synced {args.dataset}: {stats}")
    else:
        results = service.sync_all(force=args.force)
        for dataset, stats in results.items():
            print(f"Synced {dataset}: {stats}")


if __name__ == "__main__":
    sync_cli()
