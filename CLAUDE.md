# Paragraf - Claude Code Context

## Prosjektoversikt

MCP-server (Model Context Protocol) for oppslag i norsk lovdata. Gir Claude/LLM tilgang til 92 000+ paragrafer fra norske lover og forskrifter via fulltekstsok og vektorsok. Landingsside pa paragraf.dev (GitHub Pages).

Data synkroniseres fra Lovdata sitt gratis Public Data API (NLOD 2.0-lisens).

## Tech Stack

| Lag | Teknologi |
|-----|-----------|
| Kjerne | Python 3.11+ |
| MCP | JSON-RPC over stdio eller HTTP (Flask) |
| Database (prod) | Supabase PostgreSQL + pgvector |
| Database (lokal) | SQLite (fallback uten SUPABASE_URL) |
| Fulltekstsok | PostgreSQL tsvector/GIN med norsk stemming (prod), SQLite FTS5 (lokal) |
| Vektorsok | pgvector IVFFlat + Gemini gemini-embedding-001 |
| Sync | Streaming fra Lovdata API (tar.bz2) |
| Hosting | GitHub Pages (paragraf.dev), Cloudflare DNS |
| Linting | Ruff (PostToolUse hook pa Edit/Write) |

## Mappestruktur

```
src/paragraf/
  __init__.py          # Eksporterer MCPServer, LovdataService
  cli.py               # CLI: paragraf serve/sync/status
  service.py           # LovdataService - hovedfasade, velger backend
  supabase_backend.py  # Supabase-implementasjon (sync, sok, oppslag)
  sqlite_backend.py    # SQLite-implementasjon (lokal utvikling)
  server.py            # MCPServer - JSON-RPC handler
  web.py               # Flask blueprint for HTTP-modus
  vector_search.py     # Hybrid vektorsok (Gemini + FTS)
  structure_parser.py  # XML-parsing av Lovdata-dokumenter
  _supabase_utils.py   # Retry/backoff, feilhandtering

scripts/
  embed.py             # Generer embeddings for alle seksjoner

migrations/              # Referansekopi av Supabase-migrasjoner (001-008)

web/
  app.py               # Flask blueprint for hosted MCP (unified-timeline)

site/                    # GitHub Pages landingsside (paragraf.dev)

docs/
  ADR-001.md           # Arkitekturbeslutninger, skjemadetaljer, indekser
```

## Konvensjoner

### Kodestil
- **Sprak:** Norske MCP-verktoy og parameternavn (`sok`, `rettsomrade`, `inkluder_endringslover`). Engelsk i Python-kode (variabelnavn, docstrings, kommentarer).
- **Formatering:** Ruff (kjores automatisk via PostToolUse hook pa Edit/Write av .py-filer)
- **Typing:** Bruk type hints. Pyright brukes for statisk analyse.

### Dual backend-paritet
`service.py` velger backend basert pa `SUPABASE_URL`:
- **Satt:** `LovdataSupabaseService` (PostgreSQL)
- **Ikke satt:** `LovdataSyncService` (SQLite)

Begge backends ma holdes i paritet. Nar du legger til metoder, felt eller endrer datamodellen
i en backend, oppdater den andre. Kritiske metoder som ma finnes i begge:
`get_section`, `search`, `list_sections`, `is_synced`. Se ADR-001.2 for detaljer.

### Database/SQL
- **Migrasjoner:** Kjores via Supabase MCP (`apply_migration`). Filene i `migrations/` er referansekopi - hold dem oppdatert. Historiske migrasjonsfiler skal ikke endres.
- **RLS-policies:** Separate policies per operasjon (`SELECT`, `INSERT`, `UPDATE`, `DELETE`) - aldri `FOR ALL`. Unnga multiple permissive policies for samme rolle/action. Bruk `(select auth.role())` i stedet for `auth.role()` for a unnga re-evaluering per rad.
- **SQL-funksjoner:** Alltid `SET search_path = ''` og `public.`-prefiks pa tabellreferanser.

### Testing
Ingen automatisert test-suite. Verifiser endringer med direkte SQL via Supabase MCP:
- `execute_sql` for a sjekke data og sokefunksjoner
- `get_advisors` (security + performance) etter DDL-endringer
- `tests/test_mcp_tools.sh` finnes for integrasjonstester av MCP-verktoy (75 tester)

## Arkitektur

### Datapipeline
```
Lovdata API (tar.bz2) -> Streaming download -> XML-parsing -> Upsert til DB
                                                                  |
                                                          mark_non_current_docs()
                                                          (opphevede lover markeres)
                                                                  |
                                                          embed.py (Gemini API)
                                                                  |
                                                          pgvector embeddings
```

### MCP-verktoy

| Verktoy | Beskrivelse |
|---------|-------------|
| `sok(query, departement?, doc_type?, rettsomrade?, inkluder_endringslover?)` | Fulltekstsok med norsk stemming + filtre |
| `semantisk_sok(query, doc_type?, ministry?, rettsomrade?, inkluder_endringslover?)` | Hybrid vektorsok (naturlig sprak) |
| `lov(id, paragraf)` | Hent lovtekst, kapittel ('Kapittel X'/'kap X'), eller innholdsfortegnelse |
| `forskrift(id, paragraf)` | Hent forskriftstekst (uten paragraf = innholdsfortegnelse med hjemmelslov) |
| `hent_flere(id, paragrafer)` | Batch-henting (~80% raskere) |
| `relaterte_forskrifter(lov_id)` | Finn forskrifter med hjemmel i en lov |
| `departementer()` | List alle departementer (for filterverdier) |
| `rettsomrader()` | List alle rettsomrader (for filterverdier) |
| `liste()` | List tilgjengelige lover |
| `status()` | Sync-status |
| `sjekk_storrelse(id, paragraf)` | Token-estimat for seksjon |

### Alias-opplosning
Lover/forskrifter kan slas opp med naturlig navn via fire nivaer:
1. Hardkodede aliaser (`aml` -> arbeidsmiljoloven)
2. Database `short_title`-match
3. Fuzzy matching (`pg_trgm`, min 8 tegn)
4. Direkte dok_id

## Kommandoer

```bash
# Env-oppsett (credentials i .env, gitignored)
set -a && source .env && set +a

# MCP-server
paragraf serve              # stdio-modus
paragraf serve --http       # HTTP-modus (Flask)

# Sync og vedlikehold
paragraf sync               # Inkrementell sync fra Lovdata API
paragraf sync --force       # Tving full re-sync (ignorerer last_modified)
paragraf status             # Vis sync-status

# Embeddings
python3 scripts/embed.py --dry-run    # Sjekk antall manglende + kostnad
python3 scripts/embed.py              # Generer embeddings
python3 scripts/embed.py --max-time 25  # Tidsbegrenset (Supabase free tier)
```

## Supabase

Prosjektet bruker Supabase-prosjektet **unified-timeline** (`iyetsvrteyzpirygxenu`).

Tabeller: `lovdata_documents`, `lovdata_sections`, `lovdata_structure`, `lovdata_sync_meta`.
Se `docs/ADR-001.md` for skjemadetaljer, indekser og sokefunksjoner.

## Begrensninger

Kun **gjeldende lover og sentrale forskrifter** er tilgjengelig (gratis Lovdata API). Folgende er IKKE tilgjengelig:
- Rettsavgjorelser (HR, LG, LA)
- Forarbeider (NOU, Prop., Ot.prp.)
- Juridiske artikler
- Lokale forskrifter

## Vedlikehold av denne filen

**Oppdater CLAUDE.md** nar du endrer:
- MCP-verktoy (navn, parametere, nye verktoy)
- Mappestruktur eller viktige filer
- Kommandoer eller scripts
- Datamodell, migrasjoner eller konvensjoner
- Tech stack eller begrensninger

## Relaterte prosjekter

- `../unified-timeline/` - Hosting-plattform, deler Supabase-prosjekt
- `../unified-timeline/tredjepart-api/lovdata-api.json` - Lovdata API OpenAPI-spec
- `../unified-timeline/tredjepart-api/lovdata-xml.md` - XML-formatdokumentasjon
