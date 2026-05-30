"""Tester for sections unique-noekkel, migrering og deterministiske oppslag (issue #10).

Bakgrunn: sections-tabellen brukte feil/manglende unique-noekkel. Korrekt noekkel er
(dok_id, section_id, address) - address skiller legitimt gjentatte section_id paa tvers
av vedlegg (f.eks. 84x "Artikkel 1" i samme EOS-forskrift). Disse testene verifiserer
at fersk DB faar korrekt constraint, at gamle DB-er migreres trygt, og at oppslag er
deterministiske.
"""

import sqlite3

import pytest

from paragraf.sqlite_backend import LovdataSyncService

DOK = "SF/forskrift/2010-02-17-187"


def _sections_sql(db_path) -> str:
    """Hent CREATE-statement for sections-tabellen fra en DB-fil."""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sections'"
        ).fetchone()
        return row[0] if row else ""
    finally:
        con.close()


def test_fresh_db_har_korrekt_unique_noekkel(tmp_path):
    """En fersk DB skal ha UNIQUE(dok_id, section_id, address) - i schema OG haandhevet."""
    LovdataSyncService(cache_dir=tmp_path)
    sql = _sections_sql(tmp_path / "lovdata.db")
    assert "UNIQUE(dok_id, section_id, address)" in sql

    # Verifiser at constraintet faktisk HAANDHEVES (ikke bare staar i DDL):
    # plain INSERT av identisk (dok, section, address) skal gi IntegrityError.
    con = sqlite3.connect(tmp_path / "lovdata.db")
    try:
        con.execute(
            "INSERT INTO sections (dok_id, section_id, title, content, address, char_count)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (DOK, "Artikkel 1", None, "x", "vedlegg-1/art-1", 1),
        )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO sections (dok_id, section_id, title, content, address, char_count)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (DOK, "Artikkel 1", None, "y", "vedlegg-1/art-1", 1),
            )
    finally:
        con.close()


def test_insert_or_replace_bevarer_distinkt_address(tmp_path):
    """To rader med samme (dok, section) men ulik address skal begge overleve."""
    LovdataSyncService(cache_dir=tmp_path)
    con = sqlite3.connect(tmp_path / "lovdata.db")
    try:
        con.execute(
            "INSERT OR REPLACE INTO sections (dok_id, section_id, title, content, address, char_count)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (DOK, "Artikkel 1", None, "Vedlegg I innhold", "vedlegg-1/art-1", 17),
        )
        con.execute(
            "INSERT OR REPLACE INTO sections (dok_id, section_id, title, content, address, char_count)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (DOK, "Artikkel 1", None, "Vedlegg II innhold", "vedlegg-2/art-1", 18),
        )
        con.commit()
        n = con.execute(
            "SELECT COUNT(*) FROM sections WHERE dok_id=? AND section_id=?", (DOK, "Artikkel 1")
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 2


def test_insert_or_replace_erstatter_samme_address(tmp_path):
    """To rader med IDENTISK (dok, section, address) skal kollapse til en (replace)."""
    LovdataSyncService(cache_dir=tmp_path)
    con = sqlite3.connect(tmp_path / "lovdata.db")
    try:
        for innhold in ("gammelt innhold", "nytt innhold"):
            con.execute(
                "INSERT OR REPLACE INTO sections (dok_id, section_id, title, content, address, char_count)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (DOK, "Artikkel 1", None, innhold, "vedlegg-1/art-1", len(innhold)),
            )
        con.commit()
        rows = con.execute(
            "SELECT content FROM sections WHERE dok_id=? AND section_id=? AND address=?",
            (DOK, "Artikkel 1", "vedlegg-1/art-1"),
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0][0] == "nytt innhold"


# Gammel schema UTEN unique-constraint (slik den levende 648 MB-DB-en faktisk er).
_OLD_SECTIONS_SCHEMA = """
    CREATE TABLE sections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dok_id TEXT,
        section_id TEXT,
        title TEXT,
        content TEXT,
        address TEXT,
        char_count INTEGER DEFAULT 0
    )
"""


def _lag_gammel_db(cache_dir, rader):
    """Skriv en lovdata.db med GAMMEL sections-schema (ingen UNIQUE) + gitte rader.

    rader: liste av (dok_id, section_id, content, address)-tupler.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cache_dir / "lovdata.db")
    con.executescript(_OLD_SECTIONS_SCHEMA)
    for dok_id, section_id, content, address in rader:
        con.execute(
            "INSERT INTO sections (dok_id, section_id, title, content, address, char_count)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (dok_id, section_id, None, content, address, len(content)),
        )
    con.commit()
    con.close()


def test_migrering_de_dupliserer_ekte_duplikater_men_bevarer_distinkt_address(tmp_path):
    """Migrering: kollaps identiske (dok, section, address), bevar ulik address."""
    _lag_gammel_db(
        tmp_path,
        [
            # to ekte duplikater (samme dok+section+address) - skal bli EN
            (DOK, "Artikkel 1", "gammelt", "vedlegg-1/art-1"),
            (DOK, "Artikkel 1", "nyeste", "vedlegg-1/art-1"),
            # legitimt distinkt (samme dok+section, ulik address) - skal bevares
            (DOK, "Artikkel 1", "vedlegg II", "vedlegg-2/art-1"),
        ],
    )

    # Konstruksjon trigger _init_db -> migrering
    LovdataSyncService(cache_dir=tmp_path)

    con = sqlite3.connect(tmp_path / "lovdata.db")
    try:
        # Korrekt constraint er paa plass
        sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sections'"
        ).fetchone()[0]
        assert "UNIQUE(dok_id, section_id, address)" in sql
        # Ekte duplikat kollapset til en, nyeste id beholdt
        dup = con.execute(
            "SELECT content FROM sections WHERE address=?", ("vedlegg-1/art-1",)
        ).fetchall()
        assert len(dup) == 1
        assert dup[0][0] == "nyeste"
        # Legitimt distinkt rad bevart
        assert con.execute(
            "SELECT COUNT(*) FROM sections WHERE dok_id=? AND section_id=?", (DOK, "Artikkel 1")
        ).fetchone()[0] == 2
    finally:
        con.close()


def test_migrering_er_idempotent(tmp_path):
    """Aa konstruere tjenesten to ganger skal ikke endre data andre gang."""
    _lag_gammel_db(tmp_path, [(DOK, "Artikkel 1", "x", "a"), (DOK, "Artikkel 2", "y", "b")])
    LovdataSyncService(cache_dir=tmp_path)
    con = sqlite3.connect(tmp_path / "lovdata.db")
    try:
        foer = con.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    finally:
        con.close()
    LovdataSyncService(cache_dir=tmp_path)  # andre konstruksjon
    con = sqlite3.connect(tmp_path / "lovdata.db")
    try:
        etter = con.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    finally:
        con.close()
    assert foer == etter == 2


def test_migrering_rebuilder_fts_uten_duplikater(tmp_path):
    """Etter migrering skal FTS-indeksen ikke ha duplikat-treff for ekte duplikater."""
    _lag_gammel_db(
        tmp_path,
        [
            (DOK, "Artikkel 1", "unik radontekst", "vedlegg-1/art-1"),
            (DOK, "Artikkel 1", "unik radontekst", "vedlegg-1/art-1"),
        ],
    )
    LovdataSyncService(cache_dir=tmp_path)
    con = sqlite3.connect(tmp_path / "lovdata.db")
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM sections_fts WHERE content MATCH 'radontekst'"
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1


def _seed_doc_og_seksjoner(cache_dir, rader):
    """Konstruer tjeneste (fersk DB) og sett inn ett dokument + seksjonsrader.

    rader: liste av (section_id, content, address)-tupler.
    """
    svc = LovdataSyncService(cache_dir=cache_dir)
    con = sqlite3.connect(cache_dir / "lovdata.db")
    try:
        con.execute(
            "INSERT OR REPLACE INTO documents (dok_id, ref_id, title, short_title, doc_type)"
            " VALUES (?, ?, ?, ?, ?)",
            (DOK, DOK, "Testforskrift", "testforskrift", "forskrift"),
        )
        for section_id, content, address in rader:
            con.execute(
                "INSERT OR REPLACE INTO sections (dok_id, section_id, title, content, address, char_count)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (DOK, section_id, None, content, address, len(content)),
            )
        con.commit()
    finally:
        con.close()
    return svc


def test_get_section_er_deterministisk_ved_kollisjon(tmp_path):
    """Naar samme section_id finnes flere ganger, returneres nyeste (hoyeste id)."""
    svc = _seed_doc_og_seksjoner(
        tmp_path,
        [
            ("Artikkel 1", "vedlegg I", "vedlegg-1/art-1"),
            ("Artikkel 1", "vedlegg II", "vedlegg-2/art-1"),  # hoyest id -> nyeste
        ],
    )
    # Slaa opp via alias (short_title) - den realistiske kodestien.
    sec = svc.get_section("testforskrift", "Artikkel 1")
    assert sec is not None
    assert sec.content == "vedlegg II"


def test_get_section_size_er_deterministisk_ved_kollisjon(tmp_path):
    """get_section_size skal ogsaa returnere nyeste rad deterministisk."""
    svc = _seed_doc_og_seksjoner(
        tmp_path,
        [
            ("Artikkel 1", "kort", "vedlegg-1/art-1"),
            ("Artikkel 1", "mye lengre innhold her", "vedlegg-2/art-1"),
        ],
    )
    info = svc.get_section_size("testforskrift", "Artikkel 1")
    assert info is not None
    assert info["char_count"] == len("mye lengre innhold her")


def test_get_sections_batch_er_deterministisk_ved_kollisjon(tmp_path):
    """Batch-oppslag skal velge nyeste rad (hoyeste id) ved kollisjon."""
    svc = _seed_doc_og_seksjoner(
        tmp_path,
        [
            ("Artikkel 1", "vedlegg I", "vedlegg-1/art-1"),
            ("Artikkel 1", "vedlegg II", "vedlegg-2/art-1"),  # nyeste
            ("Artikkel 2", "art 2 tekst", "vedlegg-1/art-2"),
        ],
    )
    result = svc.get_sections_batch("testforskrift", ["Artikkel 1", "Artikkel 2"])
    by_id = {s.section_id: s for s in result}
    assert by_id["Artikkel 1"].content == "vedlegg II"
    assert by_id["Artikkel 2"].content == "art 2 tekst"
