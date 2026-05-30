"""Tester for sections unique-noekkel, migrering og deterministiske oppslag (issue #10).

Bakgrunn: sections-tabellen brukte feil/manglende unique-noekkel. Korrekt noekkel er
(dok_id, section_id, address) - address skiller legitimt gjentatte section_id paa tvers
av vedlegg (f.eks. 84x "Artikkel 1" i samme EOS-forskrift). Disse testene verifiserer
at fersk DB faar korrekt constraint, at gamle DB-er migreres trygt, og at oppslag er
deterministiske.
"""

import sqlite3

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
    """En fersk DB skal opprettes med UNIQUE(dok_id, section_id, address)."""
    LovdataSyncService(cache_dir=tmp_path)
    sql = _sections_sql(tmp_path / "lovdata.db")
    assert "UNIQUE(dok_id, section_id, address)" in sql


def test_insert_or_replace_bevarer_distinkt_address(tmp_path):
    """To rader med samme (dok, section) men ulik address skal begge overleve."""
    LovdataSyncService(cache_dir=tmp_path)
    con = sqlite3.connect(tmp_path / "lovdata.db")
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
    con.close()
    assert n == 2


def test_insert_or_replace_erstatter_samme_address(tmp_path):
    """To rader med IDENTISK (dok, section, address) skal kollapse til en (replace)."""
    LovdataSyncService(cache_dir=tmp_path)
    con = sqlite3.connect(tmp_path / "lovdata.db")
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
    con.close()
    assert len(rows) == 1
    assert rows[0][0] == "nytt innhold"
