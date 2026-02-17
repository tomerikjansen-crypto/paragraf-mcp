"""
LovdataService - Client for Lovdata's public API.

Provides access to Norwegian laws and regulations via the free Lovdata API
released November 2025 under NLOD 2.0 license.

API Documentation: https://api.lovdata.no/
Data source: tar.bz2 archives containing XML documents

Storage backends:
    - Supabase PostgreSQL (default when SUPABASE_URL is set) - for cloud deploy
    - Local SQLite (fallback) - for local development

Usage:
    service = LovdataService()
    text = service.lookup_law("avhendingslova", "3-9")

    # Sync data from Lovdata API
    service.sync()
"""

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

# Cache directory for SQLite fallback
CACHE_DIR = Path(os.getenv("LOVDATA_CACHE_DIR", "/tmp/lovdata-cache"))

# Use Supabase if available
USE_SUPABASE = bool(os.getenv("SUPABASE_URL"))

# Token estimation: ~3.5 chars per token for Norwegian text
CHARS_PER_TOKEN = 3.5
LARGE_RESPONSE_THRESHOLD = 5000  # tokens

# Sentinel returned by _fetch_law_content when document exists but section does not
_SECTION_NOT_FOUND = "__SECTION_NOT_FOUND__"

# Pattern to detect chapter input (e.g. "Kapittel 16", "kap. III", "kap 8 a")
_CHAPTER_RE = re.compile(r"^(?:kapittel|kap\.?)\s+(.+)$", re.IGNORECASE)

# Lazy service singletons
_supabase_service = None
_sqlite_service = None


def _get_backend_service():
    """
    Get appropriate backend service based on configuration.

    Uses Supabase when SUPABASE_URL is set, otherwise SQLite.
    """
    global _supabase_service, _sqlite_service

    if USE_SUPABASE:
        if _supabase_service is None:
            try:
                from paragraf.supabase_backend import LovdataSupabaseService

                _supabase_service = LovdataSupabaseService()
                logger.info("Using Supabase backend for Lovdata")
            except Exception as e:
                logger.warning(f"Supabase unavailable, falling back to SQLite: {e}")
                return _get_sqlite_service()
        return _supabase_service
    else:
        return _get_sqlite_service()


def _get_sqlite_service():
    """Get SQLite backend service."""
    global _sqlite_service
    if _sqlite_service is None:
        from paragraf.sqlite_backend import LovdataSyncService

        _sqlite_service = LovdataSyncService(cache_dir=CACHE_DIR)
        logger.info("Using SQLite backend for Lovdata")
    return _sqlite_service


def estimate_tokens(text: str) -> int:
    """Estimate token count for Norwegian text."""
    return int(len(text) / CHARS_PER_TOKEN)


class LovdataService:
    """
    Client for Lovdata's public API.

    Fetches and caches Norwegian laws and regulations from api.lovdata.no.
    The API provides ZIP files containing XML documents for all laws
    published in Norsk Lovtidend since 2001.
    """

    BASE_URL = "https://api.lovdata.no"

    # Common law aliases -> Lovdata IDs
    # Format: LOV-YYYY-MM-DD-NR
    LOV_ALIASES: dict[str, str] = {
        # Entreprise og bygg
        "bustadoppføringslova": "LOV-1997-06-13-43",
        "buofl": "LOV-1997-06-13-43",
        "avhendingslova": "LOV-1992-07-03-93",
        "avhl": "LOV-1992-07-03-93",
        "plan-og-bygningsloven": "LOV-2008-06-27-71",
        "pbl": "LOV-2008-06-27-71",
        "byggherreforskriften": "FOR-2009-08-03-1028",
        "byggesaksforskriften": "FOR-2010-03-26-488",
        "sak10": "FOR-2010-03-26-488",
        "byggteknisk-forskrift": "FOR-2017-06-19-840",
        "tek17": "FOR-2017-06-19-840",
        # Husleie
        "husleieloven": "LOV-1999-03-26-17",
        "husll": "LOV-1999-03-26-17",
        # Kontraktsrett
        "kjøpsloven": "LOV-1988-05-13-27",
        "forbrukerkjøpsloven": "LOV-2002-06-21-34",
        "fkjl": "LOV-2002-06-21-34",
        "håndverkertjenesteloven": "LOV-1989-06-16-63",
        "hvtjl": "LOV-1989-06-16-63",
        "angrerettloven": "LOV-2014-06-20-27",
        # Arbeidsrett
        "arbeidsmiljøloven": "LOV-2005-06-17-62",
        "aml": "LOV-2005-06-17-62",
        "ferieloven": "LOV-1988-04-29-21",
        "folketrygdloven": "LOV-1997-02-28-19",
        "ftrl": "LOV-1997-02-28-19",
        # Forvaltning
        "forvaltningsloven": "LOV-1967-02-10",
        "fvl": "LOV-1967-02-10",
        "offentleglova": "LOV-2006-05-19-16",
        "offl": "LOV-2006-05-19-16",
        "kommuneloven": "LOV-2018-06-22-83",
        "koml": "LOV-2018-06-22-83",
        # Tvisteløsning
        "tvisteloven": "LOV-2005-06-17-90",
        "tvl": "LOV-2005-06-17-90",
        "voldgiftsloven": "LOV-2004-05-14-25",
        "domstolloven": "LOV-1915-08-13-5",
        # Anskaffelser
        "anskaffelsesloven": "LOV-2016-06-17-73",
        "loa": "LOV-2016-06-17-73",
        "anskaffelsesforskriften": "FOR-2016-08-12-974",
        "foa": "FOR-2016-08-12-974",
        # Erstatning
        "skadeserstatningsloven": "LOV-1969-06-13-26",
        "skl": "LOV-1969-06-13-26",
        # Generelt
        "avtaleloven": "LOV-1918-05-31-4",
        "avtl": "LOV-1918-05-31-4",
        "straffeloven": "LOV-2005-05-20-28",
        "strl": "LOV-2005-05-20-28",
        "personopplysningsloven": "LOV-2018-06-15-38",
        "popplyl": "LOV-2018-06-15-38",
    }

    # Human-readable names
    LOV_NAMES: dict[str, str] = {
        "LOV-1997-06-13-43": "Lov om avtalar med forbrukar om oppføring av ny bustad m.m. (bustadoppføringslova)",
        "LOV-1992-07-03-93": "Lov om avhending av fast eigedom (avhendingslova)",
        "LOV-2008-06-27-71": "Lov om planlegging og byggesaksbehandling (plan- og bygningsloven)",
        "LOV-2005-06-17-62": "Lov om arbeidsmiljø, arbeidstid og stillingsvern mv. (arbeidsmiljøloven)",
        "LOV-2005-06-17-90": "Lov om mekling og rettergang i sivile tvister (tvisteloven)",
        "LOV-1967-02-10": "Lov om behandlingsmåten i forvaltningssaker (forvaltningsloven)",
        "LOV-2002-06-21-34": "Lov om forbrukerkjøp (forbrukerkjøpsloven)",
        "LOV-1988-05-13-27": "Lov om kjøp (kjøpsloven)",
        "LOV-1918-05-31-4": "Lov om avslutning av avtaler, om fuldmagt og om ugyldige viljeserklæringer (avtaleloven)",
        "LOV-1969-06-13-26": "Lov om skadeserstatning (skadeserstatningsloven)",
        "LOV-2016-06-17-73": "Lov om offentlige anskaffelser (anskaffelsesloven)",
    }

    def __init__(self):
        """Initialize LovdataService."""
        # Backend is lazily initialized on first use via _get_backend_service()
        pass

    def _resolve_id(self, alias: str) -> str:
        """
        Resolve alias to Lovdata ID.

        Uses a four-tier resolution strategy:
        1. Hardcoded aliases (fast, for common abbreviations like aml, pbl)
        2. Database lookup via short_title (covers all 4400+ laws/regulations)
        3. Fuzzy matching via pg_trgm (handles misspellings like husleielova)
           - Only for inputs >= 8 chars to avoid false positives with short words
        4. Return original input (may already be a valid ID)

        Args:
            alias: Law name, abbreviation, or ID

        Returns:
            Lovdata ID (e.g., LOV-1992-07-03-93 or lov/1992-07-03-93)
        """
        if not alias or not alias.strip():
            return ""

        normalized = alias.lower().replace(" ", "-").replace("_", "-")

        # 1. Check hardcoded aliases (fast path for common abbreviations)
        if normalized in self.LOV_ALIASES:
            return self.LOV_ALIASES[normalized]

        # 2. Database fallback - search by short_title (exact/ILIKE)
        backend = _get_backend_service()
        if hasattr(backend, "_find_document"):
            try:
                doc = backend._find_document(alias)
                if doc and doc.get("dok_id"):
                    return doc["dok_id"]
            except Exception as e:
                logger.debug(f"Database lookup failed for '{alias}': {e}")

        # 3. Fuzzy matching - handles misspellings (requires pg_trgm)
        # Only use fuzzy matching for inputs >= 8 chars to avoid false positives
        # with short generic words like "loven" matching "SE-loven"
        MIN_FUZZY_LENGTH = 8
        if len(alias) >= MIN_FUZZY_LENGTH and hasattr(backend, "find_similar_law"):
            try:
                similar = backend.find_similar_law(alias, threshold=0.4)  # type: ignore[attr-defined]
                if similar:
                    logger.info(
                        f"Fuzzy match: '{alias}' -> '{similar['short_title']}' (similarity: {similar['similarity']:.2f})"
                    )
                    return similar["dok_id"]
            except Exception as e:
                logger.debug(f"Fuzzy matching failed for '{alias}': {e}")

        # 4. Return original (may already be a valid ID like lov/1999-03-26-17)
        return alias.upper() if alias.startswith(("lov", "LOV", "for", "FOR")) else alias

    def _get_law_name(self, lov_id: str) -> str:
        """Get human-readable name for a law ID."""
        return self.LOV_NAMES.get(lov_id, lov_id)

    def _format_lovdata_url(self, lov_id: str, paragraf: str | None = None) -> str:
        """
        Format URL to lovdata.no for a law/section.

        Args:
            lov_id: Lovdata ID (e.g., LOV-1992-07-03-93)
            paragraf: Optional section number (e.g., "3-9")

        Returns:
            URL to lovdata.no
        """
        # Convert LOV-1992-07-03-93 to lov/1992-07-03-93
        if lov_id.startswith("LOV-"):
            path = "lov/" + lov_id[4:].lower()
        elif lov_id.startswith("FOR-"):
            path = "forskrift/" + lov_id[4:].lower()
        else:
            path = lov_id.lower()

        url = f"https://lovdata.no/{path}"

        if paragraf:
            # Normalize section format
            section = paragraf.lstrip("§").strip()
            url += f"/§{section}"

        return url

    def lookup_law(
        self, lov_id: str, paragraf: str | None = None, max_tokens: int | None = None
    ) -> str:
        """
        Look up a Norwegian law or specific section.

        Args:
            lov_id: Law identifier or alias (e.g., "avhendingslova", "LOV-1992-07-03-93")
            paragraf: Optional section number (e.g., "3-9", "§ 3-9")
            max_tokens: Optional token limit for truncating long responses

        Returns:
            Formatted law text with metadata and source link
        """
        # Input validation
        if not lov_id or not lov_id.strip():
            return "**Feil:** Lov-ID kan ikke være tom. Oppgi lovnavn eller ID."

        resolved_id = self._resolve_id(lov_id)
        law_name = self._get_law_name(resolved_id)
        url = self._format_lovdata_url(resolved_id, paragraf)

        logger.info(f"Looking up law: {resolved_id}, section: {paragraf}, max_tokens: {max_tokens}")

        # Get document metadata for is_current check
        backend = _get_backend_service()
        doc_meta = backend.get_document(resolved_id) if hasattr(backend, "get_document") else None
        is_current = doc_meta.get("is_current", True) if doc_meta else None

        # Try to fetch from cache/API
        content = self._fetch_law_content(resolved_id, paragraf, max_tokens=max_tokens)

        if content == _SECTION_NOT_FOUND:
            return (
                f"**Feil:** § {paragraf} finnes ikke i {law_name}.\n\n"
                f'Bruk `lov("{lov_id}")` for å se innholdsfortegnelsen, '
                f'eller `sok("{paragraf}")` for å søke.'
            )
        elif content:
            return self._format_response(
                law_name=law_name,
                law_id=resolved_id,
                paragraf=paragraf,
                content=content,
                url=url,
                is_current=is_current,
            )
        else:
            # Document not found at all
            return (
                f"**Feil:** Fant ikke loven «{lov_id}».\n\n"
                f'**Tips:** Bruk `sok("{lov_id}")` for å søke, '
                f"eller `liste()` for å se kjente aliaser.\n\n"
                f"Du kan også bruke full Lovdata-ID fra søkeresultater, "
                f'f.eks. `lov("lov/1999-03-26-17", "9-2")`.'
            )

    def _fetch_law_content(
        self, lov_id: str, paragraf: str | None = None, max_tokens: int | None = None
    ) -> str | None:
        """
        Fetch law content from cache (Supabase or SQLite).

        Args:
            lov_id: Lovdata ID or alias
            paragraf: Optional section number
            max_tokens: Optional token limit (truncates if exceeded)

        Returns:
            Law text content, _SECTION_NOT_FOUND if doc exists but section doesn't,
            or None if document not found
        """
        backend = _get_backend_service()

        try:
            if paragraf:
                # Get specific section
                section = backend.get_section(lov_id, paragraf)
                if section:
                    content = ""
                    if section.title:
                        content = f"**{section.title}**\n\n"
                    content += section.content

                    # Apply token limit if specified
                    if max_tokens:
                        max_chars = int(max_tokens * CHARS_PER_TOKEN)
                        if len(content) > max_chars:
                            content = content[:max_chars] + "\n\n... [avkortet]"

                    return content

                # Section not found — try stripping "nr X" suffix (e.g. "4-2 nr 1" → "4-2")
                stripped = re.sub(r"\s+nr\s+\d+.*$", "", paragraf, flags=re.IGNORECASE)
                if stripped != paragraf:
                    section = backend.get_section(lov_id, stripped)
                    if section:
                        content = ""
                        if section.title:
                            content = f"**{section.title}**\n\n"
                        content += section.content
                        content += (
                            f"\n\n> *Merk: § {paragraf} ble ikke funnet som egen seksjon. "
                            f"Viser hele § {stripped} som inneholder denne bestemmelsen.*"
                        )

                        if max_tokens:
                            max_chars = int(max_tokens * CHARS_PER_TOKEN)
                            if len(content) > max_chars:
                                content = content[:max_chars] + "\n\n... [avkortet]"

                        return content

                # Check if this is a chapter lookup (e.g. "Kapittel 16", "kap III")
                chapter_match = _CHAPTER_RE.match(paragraf)
                if chapter_match and hasattr(backend, "get_chapter_sections"):
                    chapter_id = chapter_match.group(1).strip()
                    chapter_info, sections = backend.get_chapter_sections(
                        lov_id, chapter_id
                    )
                    if chapter_info and sections:
                        return self._format_chapter(
                            chapter_info, sections, max_tokens
                        )
                    elif chapter_info:
                        # Chapter exists but has no sections
                        return (
                            f"**{chapter_info.get('title', f'Kapittel {chapter_id}')}**\n\n"
                            "*Ingen paragrafer funnet i dette kapittelet.*"
                        )
                    # Chapter not found — fall through to _SECTION_NOT_FOUND

                # Check if document exists at all (to differentiate errors)
                doc = backend.get_document(lov_id)
                if doc:
                    return _SECTION_NOT_FOUND
            else:
                # Get document overview with table of contents
                doc = backend.get_document(lov_id)
                if doc:
                    sections = backend.list_sections(lov_id)
                    structures = (
                        backend.list_structures(lov_id)  # type: ignore[attr-defined]
                        if hasattr(backend, "list_structures")
                        else []
                    )
                    if sections:
                        return self._format_table_of_contents(doc, sections, structures)
                    return "*Dokument funnet, men ingen paragrafer i cache.*"

        except Exception as e:
            logger.warning(f"Failed to fetch law content for {lov_id}: {e}")

        return None

    def get_section_size(self, lov_id: str, paragraf: str) -> dict | None:
        """
        Get section size without fetching content.

        Useful for Claude to decide whether to fetch full content.

        Args:
            lov_id: Law ID or alias
            paragraf: Section number

        Returns:
            Dict with char_count and estimated_tokens, or None
        """
        resolved_id = self._resolve_id(lov_id)
        backend = _get_backend_service()

        try:
            # Try Supabase method first
            if hasattr(backend, "get_section_size"):
                return backend.get_section_size(resolved_id, paragraf)

            # Fallback: fetch section and measure
            section = backend.get_section(resolved_id, paragraf)
            if section:
                char_count = len(section.content)
                return {
                    "char_count": char_count,
                    "estimated_tokens": estimate_tokens(section.content),
                }
        except Exception as e:
            logger.warning(f"Failed to get section size: {e}")

        return None

    def lookup_sections_batch(
        self, lov_id: str, section_ids: list[str], max_tokens: int | None = None
    ) -> str:
        """
        Look up multiple sections in a single call.

        More efficient than multiple lookup_law() calls.

        Args:
            lov_id: Law identifier or alias
            section_ids: List of section IDs (e.g., ['Artikkel 5', 'Artikkel 6'])
            max_tokens: Optional token limit per section

        Returns:
            Formatted text with all sections
        """
        # Input validation
        if not lov_id or not lov_id.strip():
            return "**Feil:** Lov-ID kan ikke være tom."

        if not section_ids:
            return "**Feil:** Paragraf-listen kan ikke være tom. Oppgi minst én paragraf."

        # Limit batch size to prevent excessive response sizes (~10k tokens max)
        MAX_BATCH_SIZE = 50
        if len(section_ids) > MAX_BATCH_SIZE:
            return f"**Feil:** For mange paragrafer ({len(section_ids)}). Maks {MAX_BATCH_SIZE} per batch for å unngå for store responser. Del opp i flere kall."

        resolved_id = self._resolve_id(lov_id)
        law_name = self._get_law_name(resolved_id)
        url = self._format_lovdata_url(resolved_id)

        logger.info(f"Batch lookup: {resolved_id}, sections: {section_ids}")

        backend = _get_backend_service()

        try:
            if hasattr(backend, "get_sections_batch"):
                sections = backend.get_sections_batch(resolved_id, section_ids)
            else:
                # Fallback: fetch one by one
                sections = []
                for section_id in section_ids:
                    section = backend.get_section(resolved_id, section_id)
                    if section:
                        sections.append(section)

            if not sections:
                return self._format_fallback_response(
                    law_name=law_name, law_id=resolved_id, paragraf=", ".join(section_ids), url=url
                )

            # Track which sections were found vs requested
            found_ids = {s.section_id for s in sections}
            requested_ids = set(section_ids)
            not_found = requested_ids - found_ids

            # Format all sections
            content_parts = []
            total_tokens = 0

            for section in sections:
                section_content = ""
                if section.title:
                    section_content = f"### § {section.section_id}: {section.title}\n\n"
                else:
                    section_content = f"### § {section.section_id}\n\n"

                text = section.content

                # Apply token limit per section if specified
                if max_tokens:
                    max_chars = int(max_tokens * CHARS_PER_TOKEN)
                    if len(text) > max_chars:
                        text = text[:max_chars] + "\n\n... [avkortet]"

                section_content += text
                content_parts.append(section_content)
                total_tokens += estimate_tokens(section_content)

            content = "\n\n---\n\n".join(content_parts)

            # Build not-found warning if any sections were missing
            not_found_warning = ""
            if not_found:
                not_found_list = ", ".join(sorted(not_found))
                not_found_warning = f"\n\n> **Ikke funnet:** {not_found_list}"

            return f"""## {law_name}

**Paragrafer:** {", ".join(f"§ {s.section_id}" for s in sections)}
**Lovdata ID:** {resolved_id}
**Totalt:** ~{total_tokens:,} tokens{not_found_warning}

---

{content}

---

**Kilde:** [{url}]({url})
**Lisens:** NLOD 2.0 - Norsk lisens for offentlige data
"""

        except Exception as e:
            logger.warning(f"Batch lookup failed for {resolved_id}: {e}")
            return self._format_fallback_response(
                law_name=law_name, law_id=resolved_id, paragraf=", ".join(section_ids), url=url
            )

    def sync(self, force: bool = False) -> dict:
        """
        Sync law data from Lovdata API.

        Downloads and indexes all laws and regulations.

        Args:
            force: Force re-download even if up-to-date

        Returns:
            Dict with sync stats per dataset
        """
        backend = _get_backend_service()
        return backend.sync_all(force=force)

    def get_sync_status(self) -> dict:
        """
        Get sync status for cached data.

        Returns:
            Dict with sync timestamps and file counts
        """
        backend = _get_backend_service()
        return backend.get_sync_status()

    def is_synced(self) -> bool:
        """Check if any data has been synced."""
        backend = _get_backend_service()
        return backend.is_synced()

    def get_backend_type(self) -> str:
        """Return which backend is in use."""
        return "supabase" if USE_SUPABASE else "sqlite"

    @staticmethod
    def _format_based_on(raw: str) -> str:
        """Format based_on references into a readable string.

        Handles both old concatenated format and new "; "-delimited format:
          Old: 'lov/2005-06-17-62/§1-4lov/2005-06-17-62/§14-12forskrift/2007-05-31-590'
          New: 'lov/2005-06-17-62/§1-4; lov/2005-06-17-62/§14-12; forskrift/2007-05-31-590'
        Output: 'lov/2005-06-17-62 §§ 1-4, 14-12; forskrift/2007-05-31-590'
        """
        # Normalize: strip any existing "; " delimiters, then re-split on boundaries
        normalized = re.sub(r";\s*", "", raw)
        parts_raw = re.split(r"(?=(?:lov|forskrift)/\d{4})", normalized)
        parts_raw = [p for p in parts_raw if p]

        if not parts_raw:
            return raw

        # Parse each reference into (doc_id, paragraph)
        from collections import OrderedDict

        grouped: OrderedDict[str, list[str]] = OrderedDict()
        for ref in parts_raw:
            m = re.match(r"((?:lov|forskrift)/\d{4}-\d{2}-\d{2}-\d+)(?:/§(.+))?$", ref)
            if m:
                doc_id = m.group(1)
                paragraph = m.group(2)
                if doc_id not in grouped:
                    grouped[doc_id] = []
                if paragraph:
                    grouped[doc_id].append(paragraph)
            else:
                grouped[ref] = []

        parts = []
        for doc_id, paragraphs in grouped.items():
            if paragraphs:
                if len(paragraphs) == 1:
                    parts.append(f"{doc_id} § {paragraphs[0]}")
                else:
                    parts.append(f"{doc_id} §§ {', '.join(paragraphs)}")
            else:
                parts.append(doc_id)
        return "; ".join(parts)

    def _format_chapter(
        self,
        chapter_info: dict,
        sections: list,
        max_tokens: int | None = None,
    ) -> str:
        """
        Format all sections in a chapter for display.

        Shows chapter title, then each section with heading and content.
        Respects max_tokens by showing a TOC-only fallback if content is too large.
        """
        title = chapter_info.get("title", f"Kapittel {chapter_info.get('structure_id', '?')}")
        total_chars = sum(s.char_count for s in sections)
        total_tokens = int(total_chars / CHARS_PER_TOKEN)

        # Build header
        header = f"## {title}\n\n"
        header += f"*{len(sections)} paragrafer, ~{total_tokens} tokens*\n\n"

        # Check if we need to truncate
        max_chars = int(max_tokens * CHARS_PER_TOKEN) if max_tokens else None
        if max_chars and total_chars > max_chars:
            # Too large — show TOC with sizes instead
            header += "**Kapittelet er for stort til å vise i sin helhet. Innhold:**\n\n"
            for s in sections:
                tokens = int(s.char_count / CHARS_PER_TOKEN)
                label = f"§ {s.section_id}"
                if s.title:
                    label += f": {s.title}"
                header += f"- {label} (~{tokens} tokens)\n"
            header += (
                f"\n*Bruk `lov(id, paragraf)` for å hente enkeltparagrafer, "
                f"eller øk max_tokens (nå: {max_tokens}).*"
            )
            return header

        # Full content
        parts = [header]
        for s in sections:
            section_header = f"### § {s.section_id}"
            if s.title:
                section_header += f": {s.title}"
            parts.append(section_header)
            parts.append(s.content)

        return "\n\n".join(parts)

    def _format_table_of_contents(
        self, doc: dict, sections: list[dict], structures: list[dict] | None = None
    ) -> str:
        """
        Format table of contents for a law/regulation.

        Shows hierarchical structure (Del, Kapittel) when available,
        otherwise falls back to flat list of paragraphs.

        Args:
            doc: Document metadata from backend
            sections: List of sections with section_id, title, char_count, estimated_tokens
            structures: Optional list of structures (Del, Kapittel, etc.)

        Returns:
            Formatted table of contents with usage guidance
        """
        title = doc.get("title") or doc.get("short_title") or doc.get("dok_id")
        is_current = doc.get("is_current", True)
        if not is_current:
            title = f"{title} (opphevet)"
        total_tokens = sum(s.get("estimated_tokens", 0) for s in sections)

        lines = [
            f"### Innholdsfortegnelse: {title}",
            "",
        ]

        if not is_current:
            lines.append(
                "> **Denne loven/forskriften er opphevet.** "
                "Resultatene kan vaere utdaterte. Bruk `sok()` for a finne gjeldende regelverk."
            )
            lines.append("")

        lines.append(f"**Totalt:** {len(sections)} paragrafer (~{total_tokens:,} tokens)")

        # Document metadata block (gracefully degrades on SQLite)
        meta_lines = []
        ministry = doc.get("ministry")
        if ministry:
            meta_lines.append(f"**Departement:** {ministry}")
        legal_area = doc.get("legal_area")
        if legal_area:
            meta_lines.append(f"**Rettsomrade:** {legal_area}")
        based_on = doc.get("based_on")
        if based_on:
            meta_lines.append(f"**Hjemmelslov:** {self._format_based_on(based_on)}")
        is_amendment = doc.get("is_amendment")
        if is_amendment:
            meta_lines.append("*Dette er en endringslov/-forskrift.*")

        if meta_lines:
            lines.append("")
            lines.extend(meta_lines)

        lines.append("")

        # Use hierarchical display if structures are available
        if structures:
            lines.extend(self._format_hierarchical_toc(sections, structures))
        else:
            lines.extend(self._format_flat_toc(sections))

        # Add usage guidance
        lines.extend(
            [
                "",
                "---",
                "",
                "**Bruk:**",
                f"- Hent én paragraf: `lov('{doc.get('dok_id')}', '1')` eller `forskrift(...)`",
                "- Begrens respons: `lov(..., max_tokens=2000)`",
                "",
                "*Tips: Hent spesifikke paragrafer for å spare tokens.*",
            ]
        )

        return "\n".join(lines)

    def _format_flat_toc(self, sections: list[dict]) -> list[str]:
        """Format flat table of contents (fallback when no structure)."""
        lines = [
            "| Paragraf | Tittel | Tokens |",
            "|----------|--------|-------:|",
        ]

        MAX_DISPLAY = 100
        displayed_sections = sections[:MAX_DISPLAY]

        for sec in displayed_sections:
            section_id = sec.get("section_id", "?")
            section_title = sec.get("title", "") or ""
            tokens = sec.get("estimated_tokens", 0)

            if len(section_title) > 50:
                section_title = section_title[:47] + "..."
            section_title = section_title.replace("|", "\\|")

            lines.append(f"| § {section_id} | {section_title} | {tokens:,} |")

        if len(sections) > MAX_DISPLAY:
            remaining = len(sections) - MAX_DISPLAY
            remaining_tokens = sum(s.get("estimated_tokens", 0) for s in sections[MAX_DISPLAY:])
            lines.append(f"| ... | *{remaining} flere paragrafer* | {remaining_tokens:,} |")

        return lines

    def _format_hierarchical_toc(self, sections: list[dict], structures: list[dict]) -> list[str]:
        """Format hierarchical table of contents with Del/Kapittel structure."""
        lines = []

        # Build address-to-structure mapping
        # Structure addresses are like /kapittel/1/paragraf/1-1/
        # Section addresses are like /kapittel/1/paragraf/1-1/ledd/1/
        # We match sections to the structure they belong to

        # Group sections by their parent structure based on address matching
        structure_sections: dict[str, list[dict]] = {}
        orphan_sections: list[dict] = []

        for sec in sections:
            address = sec.get("address", "") or ""
            matched = False

            # Find the most specific matching structure
            for struct in reversed(structures):  # Check deepest first
                struct_addr = struct.get("address", "") or ""
                if struct_addr and address.startswith(struct_addr):
                    key = f"{struct.get('structure_type')}:{struct.get('structure_id')}"
                    if key not in structure_sections:
                        structure_sections[key] = []
                    structure_sections[key].append(sec)
                    matched = True
                    break

            if not matched:
                orphan_sections.append(sec)

        # Render structures with their sections
        MAX_SECTIONS_PER_STRUCT = 8

        for struct in structures:
            struct_type = struct.get("structure_type", "")
            struct_id = struct.get("structure_id", "")
            struct_title = struct.get("title", "")
            key = f"{struct_type}:{struct_id}"

            # Indentation based on structure type
            if struct_type == "del":
                indent = ""
                lines.append("")  # Extra spacing before Del
            elif struct_type == "kapittel":
                indent = "  "
            else:  # avsnitt, vedlegg
                indent = "    "

            # Format structure heading
            lines.append(f"{indent}**{struct_title}**")

            # List sections in this structure
            struct_secs = structure_sections.get(key, [])
            for sec in struct_secs[:MAX_SECTIONS_PER_STRUCT]:
                sec_id = sec.get("section_id", "?")
                sec_title = sec.get("title", "") or ""
                tokens = sec.get("estimated_tokens", 0)

                if len(sec_title) > 35:
                    sec_title = sec_title[:32] + "..."

                lines.append(f"{indent}  - § {sec_id}: {sec_title} ({tokens} tok)")

            if len(struct_secs) > MAX_SECTIONS_PER_STRUCT:
                remaining = len(struct_secs) - MAX_SECTIONS_PER_STRUCT
                remaining_tokens = sum(
                    s.get("estimated_tokens", 0) for s in struct_secs[MAX_SECTIONS_PER_STRUCT:]
                )
                lines.append(f"{indent}  - *... og {remaining} flere ({remaining_tokens} tok)*")

        # Show orphan sections (no matching structure)
        if orphan_sections:
            lines.append("")
            lines.append("**Andre paragrafer:**")
            for sec in orphan_sections[:20]:
                sec_id = sec.get("section_id", "?")
                tokens = sec.get("estimated_tokens", 0)
                lines.append(f"  - § {sec_id} ({tokens} tok)")
            if len(orphan_sections) > 20:
                lines.append(f"  - *... og {len(orphan_sections) - 20} flere*")

        return lines

    def _format_response(
        self,
        law_name: str,
        law_id: str,
        paragraf: str | None,
        content: str,
        url: str,
        is_current: bool | None = None,
    ) -> str:
        """Format successful lookup response."""
        if not paragraf:
            section_header = "(hele loven)"
        elif _CHAPTER_RE.match(paragraf):
            section_header = paragraf
        else:
            section_header = f"§ {paragraf}"

        header = law_name
        if is_current is False:
            header = f"{law_name} (opphevet)"

        warning = ""
        if is_current is False:
            warning = (
                "\n> **Denne loven/forskriften er opphevet.** "
                "Resultatene kan vaere utdaterte. Bruk `sok()` for a finne gjeldende regelverk.\n"
            )

        return f"""## {header}

**Paragraf:** {section_header}
**Lovdata ID:** {law_id}
{warning}
---

{content}

---

**Kilde:** [{url}]({url})
**Lisens:** NLOD 2.0 - Norsk lisens for offentlige data
"""

    def _format_fallback_response(
        self, law_name: str, law_id: str, paragraf: str | None, url: str
    ) -> str:
        """Format fallback response when content is not cached."""
        section_header = f"§ {paragraf}" if paragraf else "(hele loven)"

        # Check sync status for better message
        sync_status = self.get_sync_status()
        if sync_status:
            tip_msg = "*Lovdata er synkronisert, men denne loven ble ikke funnet i cache.*"
        else:
            tip_msg = "*Tips: Kjør `python -m services.lovdata_sync` for å laste ned lovdata.*"

        return f"""## {law_name}

**Paragraf:** {section_header}
**Lovdata ID:** {law_id}

---

Lovteksten er ikke tilgjengelig i lokal cache.

**Se fullstendig tekst på Lovdata:**
[{url}]({url})

---

{tip_msg}
**Lisens:** NLOD 2.0 - Norsk lisens for offentlige data
"""

    def lookup_regulation(
        self, forskrift_id: str, paragraf: str | None = None, max_tokens: int | None = None
    ) -> str:
        """
        Look up a Norwegian regulation or specific section.

        Args:
            forskrift_id: Regulation identifier or alias
            paragraf: Optional section number
            max_tokens: Optional token limit for truncating long responses

        Returns:
            Formatted regulation text with metadata
        """
        resolved_id = self._resolve_id(forskrift_id)
        regulation_name = self._get_law_name(resolved_id)
        url = self._format_lovdata_url(resolved_id, paragraf)

        logger.info(
            f"Looking up regulation: {resolved_id}, section: {paragraf}, max_tokens: {max_tokens}"
        )

        # Get document metadata for is_current check
        backend = _get_backend_service()
        doc_meta = backend.get_document(resolved_id) if hasattr(backend, "get_document") else None
        is_current = doc_meta.get("is_current", True) if doc_meta else None

        # Try to fetch from cache (same as laws - both stored in lovdata_sections)
        content = self._fetch_law_content(resolved_id, paragraf, max_tokens=max_tokens)

        if content == _SECTION_NOT_FOUND:
            return (
                f"**Feil:** § {paragraf} finnes ikke i {regulation_name}.\n\n"
                f'Bruk `forskrift("{forskrift_id}")` for å se innholdsfortegnelsen, '
                f'eller `sok("{paragraf}")` for å søke.'
            )
        elif content:
            return self._format_response(
                law_name=regulation_name,
                law_id=resolved_id,
                paragraf=paragraf,
                content=content,
                url=url,
                is_current=is_current,
            )
        else:
            # Document not found at all
            return (
                f"**Feil:** Fant ikke forskriften «{forskrift_id}».\n\n"
                f'**Tips:** Bruk `sok("{forskrift_id}")` for å søke, '
                f"eller prøv fullt navn/ID."
            )

    def search(
        self,
        query: str,
        limit: int = 20,
        exclude_amendments: bool = True,
        ministry_filter: str | None = None,
        doc_type_filter: str | None = None,
        legal_area_filter: str | None = None,
    ) -> str:
        """
        Search Norwegian laws and regulations.

        Uses full-text search if data is synced, otherwise falls back to alias matching.

        Args:
            query: Search query
            limit: Maximum number of results
            exclude_amendments: Exclude amendment laws from results (default True)
            ministry_filter: Filter by ministry (partial match)
            doc_type_filter: Filter by document type ("lov" or "forskrift")
            legal_area_filter: Filter by legal area (partial match)

        Returns:
            Formatted search results
        """
        # Input validation
        if not query or not query.strip():
            return "**Feil:** Søkestreng kan ikke være tom. Oppgi ett eller flere søkeord."

        query = query.strip()

        # Normalize typographic variants for better matching
        # Em-dash (–) and en-dash (–) to hyphen (-)
        query = query.replace("–", "-").replace("—", "-")
        # Smart quotes to regular quotes
        query = query.replace('"', '"').replace('"', '"')
        query = query.replace(""", "'").replace(""", "'")

        logger.info(f"Searching laws for: {query} (limit={limit})")

        # Try FTS search first if data is synced
        if self.is_synced():
            backend = _get_backend_service()
            try:
                fts_results = backend.search(
                    query,
                    limit=limit,
                    exclude_amendments=exclude_amendments,
                    ministry_filter=ministry_filter,
                    doc_type_filter=doc_type_filter,
                    legal_area_filter=legal_area_filter,
                )
                if fts_results:
                    return self._format_fts_results(query, fts_results)
            except Exception as e:
                logger.warning(f"FTS search failed, falling back to alias search: {e}")

        # Fallback: Simple keyword matching against known laws
        results = []
        query_lower = query.lower()

        for alias, lov_id in self.LOV_ALIASES.items():
            law_name = self._get_law_name(lov_id)
            if query_lower in alias or query_lower in law_name.lower():
                if lov_id not in [r["id"] for r in results]:
                    results.append(
                        {"id": lov_id, "name": law_name, "url": self._format_lovdata_url(lov_id)}
                    )

            if len(results) >= limit:
                break

        if not results:
            return f"""## Søkeresultater for "{query}"

Ingen treff i indekserte lover.

**Tips:** Kjør `service.sync()` for å laste ned lovdata, eller søk direkte på Lovdata:
https://lovdata.no/sok?q={query.replace(" ", "+")}
"""

        result_lines = []
        for r in results:
            result_lines.append(f"- **{r['name']}**\n  ID: `{r['id']}`\n  [{r['url']}]({r['url']})")

        return f"""## Søkeresultater for "{query}"

Fant {len(results)} treff (alias-søk):

{chr(10).join(result_lines)}

---

*For fulltekstsøk, kjør `service.sync()` først.*
**Søk på Lovdata:** https://lovdata.no/sok?q={query.replace(" ", "+")}
"""

    def _format_fts_results(self, query: str, results: list[Any]) -> str:
        """Format full-text search results."""
        result_lines = []
        used_or_fallback = False

        for r in results:
            # Handle both dict and SearchResult dataclass
            if hasattr(r, "doc_type"):
                # SearchResult dataclass
                doc_type_raw = r.doc_type
                doc_type = "Lov" if doc_type_raw == "lov" else "Forskrift"
                title = r.title or r.short_title or r.dok_id
                snippet = r.snippet or ""
                dok_id = r.dok_id
                section_id = getattr(r, "section_id", None)
                search_mode = getattr(r, "search_mode", None)
                based_on = getattr(r, "based_on", None)
                legal_area = getattr(r, "legal_area", None)
                is_current = getattr(r, "is_current", None)
            else:
                # Dict fallback
                doc_type_raw = r.get("doc_type", "lov")
                doc_type = "Lov" if doc_type_raw == "lov" else "Forskrift"
                title = r.get("title") or r.get("short_title") or r.get("dok_id")
                snippet = r.get("snippet", "")
                dok_id = r["dok_id"]
                section_id = r.get("section_id")
                search_mode = r.get("search_mode")
                based_on = r.get("based_on")
                legal_area = r.get("legal_area")
                is_current = r.get("is_current")

            if search_mode == "or_fallback":
                used_or_fallback = True

            # Clean up snippet (remove HTML if present)
            snippet = snippet.replace("<mark>", "**").replace("</mark>", "**")

            # Include section_id if available
            section_info = f" § {section_id}" if section_id else ""

            # Mark opphevet documents
            opphevet_marker = " (opphevet)" if is_current is False else ""

            # Show hjemmelslov for forskrifter
            based_on_line = ""
            if doc_type == "Forskrift" and based_on:
                based_on_line = f"\n**Hjemmelslov:** {self._format_based_on(based_on)}"

            # Show legal area for context
            legal_area_line = ""
            if legal_area:
                legal_area_line = f" | *{legal_area}*"

            result_lines.append(f"""### {doc_type}: {title}{opphevet_marker}{section_info}
**ID:** `{dok_id}`{f" **Paragraf:** `{section_id}`" if section_id else ""}{legal_area_line}{based_on_line}

{snippet}
""")

        # Add note if OR fallback was used
        fallback_note = ""
        if used_or_fallback:
            fallback_note = """
> **Merk:** Søk med alle ordene ga 0 treff. Viser resultater der minst ett av ordene finnes.
> For mer presist søk, bruk `"eksakt frase"` eller `ord1 OR ord2` syntaks.

"""

        return f"""## Søkeresultater for "{query}"

Fant {len(results)} treff (fulltekstsøk):
{fallback_note}
{chr(10).join(result_lines)}

---

**Søk på Lovdata:** https://lovdata.no/sok?q={query.replace(" ", "+")}
"""

    def list_available_laws(self) -> str:
        """
        List all available law aliases and their IDs.

        Returns:
            Formatted list of available laws
        """
        categories = {
            "Entreprise og bygg": [
                "bustadoppføringslova",
                "avhendingslova",
                "plan-og-bygningsloven",
            ],
            "Kontraktsrett": [
                "kjøpsloven",
                "forbrukerkjøpsloven",
                "håndverkertjenesteloven",
                "avtaleloven",
            ],
            "Arbeidsrett": ["arbeidsmiljøloven", "ferieloven", "folketrygdloven"],
            "Tvisteløsning": ["tvisteloven", "voldgiftsloven", "domstolloven"],
            "Forvaltning": ["forvaltningsloven", "offentleglova", "kommuneloven"],
            "Anskaffelser": ["anskaffelsesloven", "anskaffelsesforskriften"],
        }

        lines = ["## Aliaser (snarveier)\n"]
        lines.append("**NB:** Dette er bare snarveier for vanlige lover. ")
        lines.append("Alle 770+ lover i Lovdata kan slås opp med `lov('lovnavn')`.\n")
        lines.append("**Tips:** Bruk `sok('emne')` for å finne lover du ikke kjenner navnet på.\n")

        for category, laws in categories.items():
            lines.append(f"### {category}\n")
            for alias in laws:
                lov_id = self.LOV_ALIASES.get(alias, "")
                if lov_id:
                    name = self._get_law_name(lov_id)
                    lines.append(f"- `{alias}` → {name}")
            lines.append("")

        lines.append("---")
        lines.append(
            "*Eksempel: `lov('husleieloven', '9-2')` fungerer selv om husleieloven ikke er i listen.*"
        )

        return "\n".join(lines)

    def get_related_regulations(self, lov_id: str) -> str:
        """
        Find regulations (forskrifter) that are based on a given law.

        Args:
            lov_id: Law identifier or alias

        Returns:
            Formatted list of related regulations
        """
        if not lov_id or not lov_id.strip():
            return "**Feil:** Lov-ID kan ikke vaere tom."

        resolved_id = self._resolve_id(lov_id)
        backend = _get_backend_service()

        if not hasattr(backend, "find_related_regulations"):
            return "**Feil:** Denne funksjonen krever Supabase-backend."

        try:
            regulations = backend.find_related_regulations(resolved_id)
        except Exception as e:
            logger.warning(f"Failed to find related regulations for {lov_id}: {e}")
            return f"**Feil:** Kunne ikke hente relaterte forskrifter for {lov_id}."

        if not regulations:
            return f"Ingen forskrifter funnet med hjemmel i **{lov_id}** (`{resolved_id}`)."

        lines = [f"## Forskrifter med hjemmel i {lov_id}\n"]
        lines.append(f"Fant {len(regulations)} forskrifter:\n")

        for reg in regulations:
            title = reg.get("short_title") or reg.get("title") or reg.get("dok_id")
            dok_id = reg.get("dok_id", "")
            ministry = reg.get("ministry")

            line = f"- **{title}**\n  ID: `{dok_id}`"
            if ministry:
                line += f"\n  Departement: {ministry}"
            lines.append(line)

        lines.append("")
        lines.append("---")
        lines.append("*Bruk `forskrift('ID', 'paragraf')` for a sla opp en forskrift.*")

        return "\n".join(lines)

    def list_ministries(self) -> str:
        """
        List all ministries that have laws/regulations.

        Returns:
            Formatted list of ministries
        """
        backend = _get_backend_service()

        if not hasattr(backend, "list_ministries"):
            return "**Feil:** Denne funksjonen krever Supabase-backend."

        try:
            ministries = backend.list_ministries()
        except Exception as e:
            logger.warning(f"Failed to list ministries: {e}")
            return "**Feil:** Kunne ikke hente departementsliste."

        if not ministries:
            return "Ingen departementer funnet. Data er kanskje ikke synkronisert."

        lines = [f"## Departementer ({len(ministries)} stk)\n"]

        for m in ministries:
            lines.append(f"- {m}")

        lines.append("")
        lines.append("---")
        lines.append(
            "**Bruk med filter:** `sok('emne', departement='Klima')` "
            "eller `semantisk_sok('emne', ministry='Justis')`"
        )

        return "\n".join(lines)

    def list_legal_areas(self) -> str:
        """
        List all legal areas (rettsområder) that have laws/regulations.

        Returns:
            Formatted list of legal areas
        """
        backend = _get_backend_service()

        if not hasattr(backend, "list_legal_areas"):
            return "**Feil:** Denne funksjonen krever Supabase-backend."

        try:
            areas = backend.list_legal_areas()  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning(f"Failed to list legal areas: {e}")
            return "**Feil:** Kunne ikke hente rettsområdeliste."

        if not areas:
            return "Ingen rettsområder funnet. Data er kanskje ikke synkronisert."

        lines = [f"## Rettsområder ({len(areas)} stk)\n"]

        for a in areas:
            lines.append(f"- {a}")

        lines.append("")
        lines.append("---")
        lines.append(
            "**Bruk med filter:** `sok('emne', rettsomrade='Erstatningsrett')` "
            "eller `semantisk_sok('emne', rettsomrade='Arbeidsliv')`"
        )

        return "\n".join(lines)
