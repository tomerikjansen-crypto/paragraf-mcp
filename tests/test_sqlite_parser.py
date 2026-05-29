"""Regresjonstester for SQLite-backendens seksjonsparser (_parse_sections).

Bakgrunn (GitHub issue #8): Parseren brukte
`article.find_all("article", class_="legalP")`, som kun matcher legalP-klassen
og rekursivt graver ned i nostede bokstavlister. Nar et nummerert ledd
(<article class="numberedLegalP">) omslutter en bokstavliste, ble selve
ledd-prosaen droppet stille - bindende lovtekst forsvant.

Fixturene under bruker EKTE Lovdata-XML-struktur (TEK17, hentet 2026-05-29).
"""

import logging

from paragraf.sqlite_backend import LovdataSyncService

TEK17_DOK_ID = "SF/forskrift/2017-06-19-840"

# Ekte § 13-5 (Radon): ledd (1) ren prosa, ledd (2) intro + nostet bokstavliste
# (numberedLegalP > ol > li > listArticle > legalP), ledd (3) ren prosa.
TEK17_13_5 = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§13-5" id="kapittel-13-kapittel-3-paragraf-1">
  <h4 class="legalArticleHeader">
    <span class="legalArticleValue">§ 13-5</span>. <span class="legalArticleTitle">Radon</span>
  </h4>
  <article class="numberedLegalP" data-numerator="1" id="kapittel-13-kapittel-3-paragraf-1-nummer-1">
    (1) I bygning med rom for varig opphold skal &#229;rsmiddelverdi for radonkonsentrasjon ikke overstige 200 Bq/m<sup>3</sup>.
  </article>
  <article class="numberedLegalP" data-numerator="2" id="kapittel-13-kapittel-3-paragraf-1-nummer-2">
    (2) Bygning med rom for varig opphold skal
    <ol class="defaultList" type="a">
      <li data-name="a." value="1">
        <article class="listArticle" id="kapittel-13-kapittel-3-paragraf-1-nummer-2-punkt-1">
          <article class="legalP" id="kapittel-13-kapittel-3-paragraf-1-nummer-2-punkt-1-ledd-1">ha radonsperre mot grunnen, og</article>
        </article>
      </li>
      <li data-name="b." value="2">
        <article class="listArticle" id="kapittel-13-kapittel-3-paragraf-1-nummer-2-punkt-2">
          <article class="legalP" id="kapittel-13-kapittel-3-paragraf-1-nummer-2-punkt-2-ledd-1">v&#230;re tilrettelagt for trykkreduserende tiltak i grunnen under bygningen som kan aktiveres n&#229;r radonkonsentrasjonen i inneluften overstiger 100 Bq/m<sup>3</sup>.</article>
        </article>
      </li>
    </ol>
  </article>
  <article class="numberedLegalP" data-numerator="3" id="kapittel-13-kapittel-3-paragraf-1-nummer-3">
    (3) Annet ledd gjelder ikke dersom det kan dokumenteres at tiltakene er un&#248;dvendige for &#229; tilfredsstille kravet i f&#248;rste ledd.
  </article>
</article>
</main></body></html>"""

# Ekte § 1-1 (Formal): flate legalP-ledd som direkte barn, ingen nummerering.
TEK17_1_1 = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§1-1" id="kapittel-1-paragraf-1">
  <h3 class="legalArticleHeader">
    <span class="legalArticleValue">§ 1-1</span>. <span class="legalArticleTitle">Form&#229;l</span>
  </h3>
  <article class="legalP" id="kapittel-1-paragraf-1-ledd-1">Forskriften skal sikre at tiltak planlegges, prosjekteres og utf&#248;res ut fra hensyn til god visuell kvalitet, universell utforming og slik at tiltaket oppfyller tekniske krav til sikkerhet, milj&#248;, helse og energi.</article>
</article>
</main></body></html>"""

# § 11-8-stil: rene nummererte ledd uten bokstavliste (numberedLegalP, ingen indre legalP).
TEK17_11_8 = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§11-8" id="kapittel-11-paragraf-8">
  <h4 class="legalArticleHeader">
    <span class="legalArticleValue">§ 11-8</span>. <span class="legalArticleTitle">Brannceller</span>
  </h4>
  <article class="numberedLegalP" data-numerator="1" id="kapittel-11-paragraf-8-nummer-1">(1) Byggverk skal deles opp i brannceller p&#229; en hensiktsmessig m&#229;te.</article>
  <article class="numberedLegalP" data-numerator="2" id="kapittel-11-paragraf-8-nummer-2">(2) Brannceller skal v&#230;re utf&#248;rt slik at de forhindrer spredning av brann og branngasser.</article>
</article>
</main></body></html>"""

# Ekte § 14-5-struktur: nummerert ledd + defaultP (tabell med energitiltak/
# U-verdier) + changesToParent (endringsnote). Tabellen er bindende tekst og
# skal med; endringsnoten er metadata og skal IKKE med i content.
TEK17_14_5 = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§14-5" id="kapittel-14-paragraf-5">
  <h3 class="legalArticleHeader">
    <span class="legalArticleValue">§ 14-5</span>. <span class="legalArticleTitle">Unntak og krav til ulike tiltak</span>
  </h3>
  <article class="numberedLegalP" data-numerator="1" id="kapittel-14-paragraf-5-nummer-1">(1) For boligbygning og fritidsbolig med laftede yttervegger gjelder egne energitiltak.</article>
  <article class="defaultP">
    <i>Tabell: Energitiltak for boligbygning og fritidsbolig med laftede yttervegger</i>
    <table>
      <thead><tr><th></th><th><i>Energitiltak</i></th></tr></thead>
      <tbody>
        <tr><td>2.</td><td>U-verdi tak [W/(m<sup>2</sup>K)]</td><td>&#8804; 0,13</td></tr>
      </tbody>
    </table>
  </article>
  <article class="changesToParent">Endret ved <a href="forskrift/2017-12-12-2000">forskrift 12 des 2017 nr. 2000</a> (i kraft 1 jan 2018).</article>
</article>
</main></body></html>"""

# Ledd med innskutt bokstavliste etterfulgt av leddfortsettelse-prosa.
# <p class="leddfortsettelse"> er bindende lovtekst som fortsetter leddet
# ETTER lista. Ligger som direkte barn av legalArticle og ble droppet stille:
# direkte-barn-loopen fant allerede numberedLegalP (content_parts ikke-tom),
# saa fallbackene fyrte aldri. Referanse: supabase_backend.py:668-672.
REGNSKAP_5_1 = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§5-1" id="kapittel-5-paragraf-1">
  <h4 class="legalArticleHeader">
    <span class="legalArticleValue">§ 5-1</span>. <span class="legalArticleTitle">Renteinntekter</span>
  </h4>
  <article class="numberedLegalP" data-numerator="1" id="kapittel-5-paragraf-1-nummer-1">(1) Resultatregnskapet skal omfatte f&#248;lgende poster:
    <ol class="defaultList" type="a">
      <li data-name="a." value="1"><article class="listArticle"><article class="legalP">Renter av utl&#229;n til kunder</article></article></li>
      <li data-name="b." value="2"><article class="listArticle"><article class="legalP">Renter av plasseringer i sentralbank</article></article></li>
    </ol>
  </article>
  <p class="leddfortsettelse">Sum renteinntekter og lignende inntekter</p>
</article>
</main></body></html>"""


def _parse(tmp_path, html, dok_id=TEK17_DOK_ID):
    """Parse en HTML-fixture via den faktiske _parse_sections-metoden."""
    svc = LovdataSyncService(cache_dir=tmp_path)
    xml = tmp_path / "doc.xml"
    xml.write_text(html, encoding="utf-8")
    return {s.section_id: s for s in svc._parse_sections(xml, dok_id)}


def test_numbered_ledd_prose_is_retained(tmp_path):
    """Ren prosa i nummererte ledd (ledd 1 og 3) skal ikke droppes."""
    secs = _parse(tmp_path, TEK17_13_5)
    assert "13-5" in secs
    content = secs["13-5"].content
    # Ledd (1) - mest siterte radon-grenseverdi i byggesak:
    assert "200 Bq" in content
    assert "årsmiddelverdi" in content  # arsmiddelverdi
    # Ledd (3) - unntaket:
    assert "Annet ledd gjelder ikke" in content


def test_letter_list_items_still_retained(tmp_path):
    """Bokstav-elementene i ledd (2) skal fortsatt vaere med (ingen regresjon)."""
    content = _parse(tmp_path, TEK17_13_5)["13-5"].content
    assert "radonsperre" in content
    assert "trykkreduserende" in content


def test_nested_letter_list_not_duplicated(tmp_path):
    """Ledd (2)-teksten skal ikke telles to ganger (intro + nostet liste)."""
    content = _parse(tmp_path, TEK17_13_5)["13-5"].content
    assert content.count("radonsperre") == 1


def test_flat_legalp_ledd_still_parsed(tmp_path):
    """Flate legalP-ledd (uten nummerering) skal fortsatt parses korrekt."""
    content = _parse(tmp_path, TEK17_1_1)["1-1"].content
    assert "god visuell kvalitet" in content
    assert "universell utforming" in content


def test_pure_numbered_ledd_retained(tmp_path):
    """Rene nummererte ledd uten bokstavliste skal bevares fullstendig."""
    content = _parse(tmp_path, TEK17_11_8)["11-8"].content
    assert "deles opp i brannceller" in content
    assert "spredning av brann" in content


def test_defaultp_table_content_retained(tmp_path):
    """defaultP-tabeller (f.eks. energitiltak/U-verdier) er bindende og skal med."""
    content = _parse(tmp_path, TEK17_14_5)["14-5"].content
    assert "Energitiltak" in content
    assert "U-verdi tak" in content


def test_change_notes_excluded(tmp_path):
    """changesToParent (endringsnoter) er metadata, ikke forskriftstekst."""
    content = _parse(tmp_path, TEK17_14_5)["14-5"].content
    assert "Endret ved" not in content


def test_leddfortsettelse_after_list_is_retained(tmp_path):
    """Prosa som fortsetter et ledd etter en innskutt liste (leddfortsettelse)
    er bindende lovtekst og skal med - ikke droppes naar andre ledd finnes."""
    content = _parse(tmp_path, REGNSKAP_5_1)["5-1"].content
    assert "Sum renteinntekter og lignende inntekter" in content
    # Sanity: ledd-intro og listeelementer fortsatt med (ingen regresjon)
    assert "Renter av utl" in content
    assert "Renter av plasseringer" in content


# listLegalP og marginIdLegalP er to legalP-varianter i legal_p_classes-settet
# men uten egen testdekning. Begge skal fanges av direkte-barn-loopen.
VARIANT_LEDD = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§7-2" id="kapittel-7-paragraf-2">
  <h4 class="legalArticleHeader">
    <span class="legalArticleValue">§ 7-2</span>. <span class="legalArticleTitle">Varianter</span>
  </h4>
  <article class="listLegalP" id="kapittel-7-paragraf-2-ledd-1">Tekst i et listLegalP-ledd.</article>
  <article class="marginIdLegalP" id="kapittel-7-paragraf-2-ledd-2">Tekst i et marginIdLegalP-ledd.</article>
</article>
</main></body></html>"""

# Et barn med kombinert klasse "legalP footnote" skal ekskluderes (fotnoter er
# ikke forskriftstekst), mens et rent legalP-ledd ved siden av skal beholdes.
FOOTNOTE_LEDD = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§8-1" id="kapittel-8-paragraf-1">
  <h4 class="legalArticleHeader">
    <span class="legalArticleValue">§ 8-1</span>. <span class="legalArticleTitle">Med fotnote</span>
  </h4>
  <article class="legalP" id="kapittel-8-paragraf-1-ledd-1">Dette er bindende ledd-tekst.</article>
  <article class="legalP footnote" id="kapittel-8-paragraf-1-fotnote-1">Dette er en fotnote som ikke skal med.</article>
</article>
</main></body></html>"""

# Fallback 1: ingen direkte legal-p-barn (legalP pakket i ukjent <div>-wrapper),
# saa direkte-barn-loopen gir tomt og rekursjonen etter legalP maa traa inn.
FALLBACK1_WRAPPED = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§9-1" id="kapittel-9-paragraf-1">
  <h4 class="legalArticleHeader">
    <span class="legalArticleValue">§ 9-1</span>. <span class="legalArticleTitle">Pakket</span>
  </h4>
  <div class="ukjentWrapper">
    <article class="legalP" id="kapittel-9-paragraf-1-ledd-1">Dypt nostet ledd-tekst her.</article>
  </div>
</article>
</main></body></html>"""

# Fallback 2: gyldig legalArticleValue men ingen legalP-etterkommere i det hele
# tatt, saa hele artikkelteksten tas (en paragraf lagres aldri helt tom).
FALLBACK2_BARE = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§9-2" id="kapittel-9-paragraf-2">
  <h4 class="legalArticleHeader">
    <span class="legalArticleValue">§ 9-2</span>. <span class="legalArticleTitle">Bar</span>
  </h4>
  <div class="ukjentWrapper">Kun loes tekst uten legalP-tagger.</div>
</article>
</main></body></html>"""


def test_listlegalp_and_marginidlegalp_retained(tmp_path):
    """Begge legalP-variantene i settet skal fanges av direkte-barn-loopen."""
    content = _parse(tmp_path, VARIANT_LEDD)["7-2"].content
    assert "listLegalP-ledd" in content
    assert "marginIdLegalP-ledd" in content


def test_footnote_class_excluded(tmp_path):
    """Barn med 'footnote' i klassesettet ekskluderes, ledd-tekst beholdes."""
    content = _parse(tmp_path, FOOTNOTE_LEDD)["8-1"].content
    assert "bindende ledd-tekst" in content
    assert "fotnote som ikke skal med" not in content


def test_fallback1_recurses_for_wrapped_legalp(tmp_path):
    """Fallback 1: legalP pakket i ukjent wrapper fanges via rekursjon."""
    content = _parse(tmp_path, FALLBACK1_WRAPPED)["9-1"].content
    assert "Dypt nostet ledd-tekst her" in content


def test_fallback2_takes_whole_article_when_no_legalp(tmp_path):
    """Fallback 2: uten legalP-etterkommere tas hele artikkelteksten."""
    content = _parse(tmp_path, FALLBACK2_BARE)["9-2"].content
    assert "Kun loes tekst uten legalP-tagger" in content


# Et innholdsbaerende soesken i en ukjent klasse (ikke i legal_p_classes og
# ikke metadata) skal logges paa DEBUG saa stille tap blir synlig.
UNKNOWN_SIBLING = """<!DOCTYPE html><html><body><main class="documentBody">
<article class="legalArticle" data-name="§10-1" id="kapittel-10-paragraf-1">
  <h4 class="legalArticleHeader">
    <span class="legalArticleValue">§ 10-1</span>. <span class="legalArticleTitle">Ukjent soesken</span>
  </h4>
  <article class="legalP" id="kapittel-10-paragraf-1-ledd-1">Fanget ledd-tekst.</article>
  <div class="merkverdigInnhold">Innhold i en ukjent klasse som droppes.</div>
</article>
</main></body></html>"""


def test_dropped_unknown_sibling_is_logged(tmp_path, caplog):
    """Ukjent innholdsbaerende soesken skal logges paa DEBUG (synlighet)."""
    with caplog.at_level(logging.DEBUG, logger="paragraf.sqlite_backend"):
        _parse(tmp_path, UNKNOWN_SIBLING)
    assert any("merkverdigInnhold" in rec.getMessage() for rec in caplog.records)
