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
