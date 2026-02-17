"""
MCP Server implementation for Lovdata tools.

Implements the Model Context Protocol (MCP) JSON-RPC interface
for exposing Norwegian law lookup tools to AI assistants.

Protocol specification: https://modelcontextprotocol.io/specification/2025-03-26
"""

import logging
from typing import Any

from paragraf.service import LovdataService
from paragraf.vector_search import LovdataVectorSearch

logger = logging.getLogger(__name__)


# MCP Protocol version
PROTOCOL_VERSION = "2025-06-18"

# Server info
SERVER_INFO = {
    "name": "paragraf",
    "version": "0.1.0",
}

# Server instructions - shown to connecting clients
SERVER_INSTRUCTIONS = """
# Paragraf - Norsk Lovoppslag

Tilgang til norske lover og forskrifter fra Lovdata Public API (92 000+ paragrafer).

## Verktøy

| Verktøy | Bruk |
|---------|------|
| `lov(lov_id, paragraf?)` | Slå opp lov, kapittel ('Kapittel X'/'kap X'), eller innholdsfortegnelse |
| `forskrift(id, paragraf?)` | Slå opp forskrift. Uten paragraf → innholdsfortegnelse med hjemmelslov |
| `sok(query, limit?, departement?, doc_type?, rettsomrade?, inkluder_endringslover?)` | **FTS-søk** med filtre |
| `semantisk_sok(query, limit?, doc_type?, ministry?, rettsomrade?, inkluder_endringslover?)` | **AI-søk** for naturlig språk |
| `hent_flere(lov_id, [paragrafer])` | Batch-henting (~80% raskere enn separate kall) |
| `relaterte_forskrifter(lov_id)` | Finn forskrifter med hjemmel i en lov |
| `departementer()` | List alle departementer (for filterverdier) |
| `rettsomrader()` | List alle rettsområder (for filterverdier) |
| `liste` | Vis aliaser (IKKE komplett liste - alle 770+ lover kan slås opp) |
| `sjekk_storrelse` | Estimer tokens før henting |

## Velg riktig søk

| Brukersituasjon | Bruk | Hvorfor |
|-----------------|------|---------|
| Kjenner juridisk term | `sok("vesentlig mislighold")` | FTS er raskere |
| Vil kombinere/ekskludere | `sok("miljø OR klima -bil")` | FTS har full søkesyntaks |
| Bruker spør med egne ord | `semantisk_sok("skjulte feil i boligen")` | AI forstår → finner "mangel" |
| Synonym-problem | `semantisk_sok("oppsigelse")` | Finner også "avskjed"-paragrafer |
| Filtrere på type/departement | `semantisk_sok(query, doc_type="lov")` eller `sok(query, departement="Klima")` | Begge har filter |
| Filtrere på rettsområde | `sok(query, rettsomrade="Erstatningsrett")` | Begrenser til fagfelt (bruk `rettsomrader()` for verdier) |
| Finne tilhørende forskrifter | `relaterte_forskrifter("aml")` | Viser forskrifter hjemlet i loven |

**Kjerneforskjell:** FTS krever at ordene finnes i teksten. Semantisk finner relatert innhold selv om ordene er annerledes.

## Søketips for FTS (`sok`)

FTS prøver AND-logikk først. Hvis 0 treff, faller den automatisk tilbake til OR.

| Søk | Resultat | Modus |
|-----|----------|-------|
| `klima` | ✅ | AND |
| `vesentlig mislighold` | ✅ | AND (begge ord finnes) |
| `oppsigelse nedbemanning` | ✅ | OR-fallback (AND ga 0) |
| `"eksakt frase"` | ✅ | AND (quotes respekteres) |

**Automatisk OR-fallback:**
- Søk med flere ord prøver AND først
- Hvis 0 treff → konverteres automatisk til OR
- Responsen viser om OR-fallback ble brukt
- Spesielle operatorer (OR, quotes, -) respekteres og utløser ikke fallback

**Søkesyntaks:**
- `miljø OR klima` → eksplisitt OR (ingen fallback)
- `"vesentlig mislighold"` → eksakt frase
- `mangel -bil` → mangel, men ikke bil

**Tommelfingerregler:**
- Bruk 1-2 **substantiver fra lovteksten** (ikke dokumentnavn)
- For presise treff → bruk `"eksakt frase"` med quotes
- For brede søk → la automatisk OR-fallback gjøre jobben

## Anbefalt arbeidsflyt

1. **Ukjent rettsområde?** → `sok("brede nøkkelord")` - kartlegg først!
2. **Vet hvilken lov?** → `lov("navn")` gir hierarkisk oversikt (Del → Kapittel → §) med tokens
3. **Trenger flere §§?** → `hent_flere()` er ~80% raskere
4. **Før henting av kapitler/store §§** → `sjekk_storrelse()` ALLTID. Spør bruker ved >5000 tokens
5. **Presis sitering?** → `lov("navn", "paragraf")`
6. **Helt kapittel?** → `lov("navn", "Kapittel X")` eller `lov("navn", "kap X")`
7. **ALLTID etter søk** → Tilby systematisk utforskning (se under)

## VIKTIG: Sjekk størrelse før store hentinger

Bruk `sjekk_storrelse(lov_id, paragraf)` **FØR** du henter:
- Hele kapitler eller deler av en lov
- Paragrafer du ikke kjenner størrelsen på
- Når bruker ber om "alt om [tema]"

Hvis estimatet er >5000 tokens: **spør brukeren** før du henter.
Hvis <2000 tokens: trygt å hente direkte.

## VIKTIG: Tilby systematisk utforskning etter søk

**ALLTID** etter et søk, gjør BEGGE deler:

1. **Utforsk selv først:** Hent innholdsfortegnelse med `lov("navn")` for å se strukturen
2. **Tilby bruker videre utforskning** på slutten av svaret:

```
---
**Vil du utforske videre?**
- Se hele [kapittel X] om [tema]?
- Søke i tilgrensende områder (f.eks. [forslag])?
```

**Hvorfor dette er kritisk:**
- Søk gir relevante treff, men IKKE nødvendigvis alle relevante paragrafer
- Brukere uten juridisk bakgrunn vet ikke at søk bare er et utgangspunkt
- Relaterte paragrafer (samme kapittel, kryssreferanser) er ofte like viktige
- Ved å tilby utforskning gir du brukeren kontroll over dybden

**Eksempel:** Bruker spør om "oppsigelse i prøvetid"
1. Du søker og finner § 15-6 (oppsigelsesvern i prøvetid)
2. Du henter `lov("aml")` og ser at kapittel 15 handler om opphør
3. Du gir svaret basert på § 15-6 og § 15-3 (frister)
4. Du avslutter med: "Vil du se hele kapittel 15 om opphør av arbeidsforhold?" → `lov("aml", "Kapittel 15")`

## Viktig om lov-IDer

**Hvis lovnavn ikke fungerer:** Bruk full ID fra sok-resultatet!
- `sok("tvangsfullbyrdelse")` → gir ID `lov/1992-06-26-86`
- `lov("lov/1992-06-26-86", "13-2")` → fungerer alltid

Sok returnerer alltid gyldig ID som kan brukes direkte i lov().

**Ikke anta du kjenner hele rettsbildet!**
- Søk bredt ved tverrfaglige spørsmål
- Søk tilgrensende områder (personvern → også "arkiv", "taushetsplikt")
- Ved offentlig sektor: sok også sektorspesifikke regler

## GDPR / Personvern

GDPR (personvernforordningen) er tilgjengelig via personopplysningsloven:
- `lov("personopplysningsloven", "Artikkel 5")` → GDPR Art. 5 (prinsipper)
- `lov("personopplysningsloven", "Artikkel 6")` → GDPR Art. 6 (behandlingsgrunnlag)
- `sok("personvernkonsekvenser")` → finner DPIA-krav (Art. 35)

## Begrensninger

**IKKE tilgjengelig:**
- Rettsavgjørelser (Høyesterett, lagmannsrett)
- Forarbeider (NOU, Prop., Ot.prp.)
- Juridiske artikler

→ Henvis til lovdata.no for disse.

## Kommuniser muligheter til brukeren

Når relevant, informer brukeren om hva du kan tilby:
- "Vil du se hvordan loven er bygd opp? Jeg kan vise kapitteloversikt."
- "Jeg kan søke på tvers av alle 92 000 paragrafer hvis du er usikker på hvilken lov."
- "Skal jeg hente flere paragrafer samtidig?"

## Aliaser

Forkortelser som `aml`, `avhl`, `pbl`, `foa` finnes i databasen.
Bruk `liste` for oversikt, eller søk direkte - f.eks. `lov("aml")` fungerer.
"""


class MCPServer:
    """
    MCP Server for Lovdata law lookup tools.

    Handles JSON-RPC requests according to the MCP protocol,
    routing to appropriate tool implementations.
    """

    def __init__(self, lovdata_service: LovdataService | None = None):
        """
        Initialize MCP Server.

        Args:
            lovdata_service: LovdataService instance (created if not provided)
        """
        self.lovdata = lovdata_service or LovdataService()
        self._vector_search: LovdataVectorSearch | None = None  # Lazy init
        self.tools = self._define_tools()
        logger.info(f"MCPServer initialized with {len(self.tools)} tools")

    def _get_vector_search(self) -> LovdataVectorSearch:
        """Get or create vector search service (lazy init)."""
        if self._vector_search is None:
            self._vector_search = LovdataVectorSearch()
        return self._vector_search

    def _define_tools(self) -> list[dict[str, Any]]:
        """Define available MCP tools with their schemas."""
        return [
            {
                "name": "lov",
                "title": "Lovoppslag",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "Slå opp norsk lov eller spesifikk paragraf fra Lovdata. "
                    "Støtter kortnavn (avhendingslova, buofl, pbl, aml) eller full ID. "
                    "Paragraf: bruk kun tall ('3-9'), ikke '§ 3-9'. "
                    "Eksempel: lov('aml', '14-9') for arbeidsmiljøloven § 14-9"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lov_id": {
                            "type": "string",
                            "description": (
                                "Lovens kortnavn eller ID. "
                                "Korte aliaser: aml, pbl, buofl, avhl, tvl. "
                                "Lange: arbeidsmiljøloven, plan-og-bygningsloven, etc."
                            ),
                        },
                        "paragraf": {
                            "type": "string",
                            "description": (
                                "Paragrafnummer uten §-tegn, eller 'Kapittel X' / 'kap X' for hele kapitler. "
                                "Format: '3-9', '14-9', 'Kapittel 16', 'kap III'. "
                                "Utelat for dokumentoversikt."
                            ),
                        },
                        "max_tokens": {
                            "type": "integer",
                            "description": (
                                "Maks tokens i respons. "
                                "Bruk sjekk_storrelse først for store paragrafer."
                            ),
                        },
                    },
                    "required": ["lov_id"],
                },
            },
            {
                "name": "forskrift",
                "title": "Forskriftsoppslag",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "Slå opp norsk forskrift fra Lovdata. "
                    "Eksempel: forskrift('byggherreforskriften', '5')"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "forskrift_id": {
                            "type": "string",
                            "description": "Forskriftens navn eller ID",
                        },
                        "paragraf": {"type": "string", "description": "Paragrafnummer (valgfritt)"},
                        "max_tokens": {
                            "type": "integer",
                            "description": "Maks antall tokens i respons (valgfritt)",
                        },
                    },
                    "required": ["forskrift_id"],
                },
            },
            {
                "name": "sok",
                "title": "Søk i Lovdata (FTS)",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "Fulltekstsøk i norske lover. Raskt og presist når du kjenner termene. "
                    'Støtter: OR, "frase", -ekskluder. '
                    "Eks: 'mangel', 'miljø OR klima', '\"vesentlig mislighold\"'. "
                    "For naturlig språk/synonymer, bruk semantisk_sok."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                'Søkeord. Støtter: OR, "frase", -ekskluder. '
                                "Eks: 'klima', 'miljø OR tildelingskriterier', "
                                "'\"vesentlig mislighold\"'"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maks antall resultater (standard: 20)",
                            "default": 20,
                        },
                        "departement": {
                            "type": "string",
                            "description": (
                                "Filtrer på departement (delvis match). "
                                "Eks: 'Klima' matcher 'Klima- og miljødepartementet'"
                            ),
                        },
                        "doc_type": {
                            "type": "string",
                            "enum": ["lov", "forskrift"],
                            "description": "Filtrer på dokumenttype",
                        },
                        "rettsomrade": {
                            "type": "string",
                            "description": (
                                "Filtrer på rettsområde (delvis match). "
                                "Eks: 'Erstatningsrett', 'Arbeidsliv'"
                            ),
                        },
                        "inkluder_endringslover": {
                            "type": "boolean",
                            "description": (
                                "Inkluder endringslover i resultater. "
                                "Standard: false (endringslover filtreres bort)"
                            ),
                            "default": False,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "semantisk_sok",
                "title": "Semantisk søk (AI)",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "Hybrid vektorsøk med AI-embeddings. Beste for naturlig språk og synonymer. "
                    "Finner relaterte paragrafer selv om ordene ikke matcher eksakt. "
                    "Eks: 'skjulte feil i boligen' → finner 'mangel'-paragrafer. "
                    "Kan filtrere på doc_type, ministry, rettsområde og ekskludere endringslover."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Søketekst i naturlig språk. "
                                "Eks: 'hva skjer hvis jeg ikke betaler husleie', "
                                "'oppsigelse av ansatt', 'skjulte feil ved boligkjøp'"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maks antall resultater (standard: 10)",
                            "default": 10,
                        },
                        "doc_type": {
                            "type": "string",
                            "enum": ["lov", "forskrift"],
                            "description": "Filtrer på dokumenttype (valgfritt)",
                        },
                        "ministry": {
                            "type": "string",
                            "description": (
                                "Filtrer på departement (delvis match). "
                                "Eks: 'Klima' matcher 'Klima- og miljødepartementet'"
                            ),
                        },
                        "rettsomrade": {
                            "type": "string",
                            "description": (
                                "Filtrer på rettsområde (delvis match). "
                                "Eks: 'Erstatningsrett', 'Arbeidsliv'"
                            ),
                        },
                        "inkluder_endringslover": {
                            "type": "boolean",
                            "description": (
                                "Inkluder endringslover i resultater. "
                                "Standard: false (endringslover filtreres bort)"
                            ),
                            "default": False,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "hent_flere",
                "title": "Hent flere paragrafer",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "Hent flere paragrafer fra samme lov i ett kall. "
                    "Mer effektivt enn flere separate lov()-kall. "
                    "Eksempel: hent_flere('personopplysningsloven', ['Artikkel 5', 'Artikkel 6', 'Artikkel 35'])"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lov_id": {
                            "type": "string",
                            "description": "Lov-ID eller alias (f.eks. 'personopplysningsloven')",
                        },
                        "paragrafer": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Liste med paragraf-IDer (f.eks. ['Artikkel 5', 'Artikkel 6'])",
                        },
                        "max_tokens": {
                            "type": "integer",
                            "description": "Maks tokens per paragraf (valgfri)",
                        },
                    },
                    "required": ["lov_id", "paragrafer"],
                },
            },
            {
                "name": "liste",
                "title": "Aliaser",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "Vis forhåndsdefinerte aliaser (snarveier) for vanlige lover. "
                    "MERK: Dette er IKKE en komplett liste - alle 770+ lover i Lovdata "
                    "kan slås opp med lov('lovnavn'). Bruk sok() for å finne lover."
                ),
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "sync",
                "title": "Synkroniser",
                "annotations": {"destructiveHint": True, "readOnlyHint": False},
                "description": (
                    "Synkroniser lovdata fra Lovdata API. "
                    "Laster ned gjeldende lover og forskrifter til lokal cache. "
                    "Må kjøres minst én gang for at lov() og sok() skal returnere innhold."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "force": {
                            "type": "boolean",
                            "description": "Tving re-nedlasting selv om data er oppdatert",
                            "default": False,
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "status",
                "title": "Synkroniseringsstatus",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "Vis status for synkronisert lovdata. "
                    "Viser når data sist ble synkronisert og antall dokumenter."
                ),
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "sjekk_storrelse",
                "title": "Sjekk paragrafstørrelse",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "Sjekk størrelsen på en paragraf før henting. "
                    "Returnerer estimert antall tokens. "
                    "Bruk dette for å avgjøre om du bør be brukeren om bekreftelse "
                    "før du henter store paragrafer (>5000 tokens)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lov_id": {"type": "string", "description": "Lovens kortnavn eller ID"},
                        "paragraf": {
                            "type": "string",
                            "description": "Paragrafnummer (f.eks. '3-9')",
                        },
                    },
                    "required": ["lov_id", "paragraf"],
                },
            },
            {
                "name": "relaterte_forskrifter",
                "title": "Forskrifter med hjemmel i lov",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "Finn forskrifter som har hjemmel i en gitt lov. "
                    "Nyttig for å se hvilke forskrifter som utfyller en lov. "
                    "Eks: relaterte_forskrifter('aml') → forskrifter hjemlet i arbeidsmiljøloven"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lov_id": {
                            "type": "string",
                            "description": "Lovens kortnavn eller ID (f.eks. 'aml', 'pbl')",
                        },
                    },
                    "required": ["lov_id"],
                },
            },
            {
                "name": "departementer",
                "title": "Liste over departementer",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "List alle departementer som har lover/forskrifter. "
                    "Bruk dette for å finne gyldige filterverdier for "
                    "sok(departement=...) og semantisk_sok(ministry=...)."
                ),
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "rettsomrader",
                "title": "Liste over rettsområder",
                "annotations": {"readOnlyHint": True},
                "description": (
                    "List alle rettsområder (juridiske fagfelt) som finnes i databasen. "
                    "Bruk dette for å finne gyldige filterverdier for "
                    "sok(rettsomrade=...) og semantisk_sok(rettsomrade=...)."
                ),
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
        ]

    def handle_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        Handle incoming MCP JSON-RPC request.

        Args:
            body: JSON-RPC request body

        Returns:
            JSON-RPC response
        """
        method = body.get("method", "")
        params = body.get("params", {})
        request_id = body.get("id")

        logger.debug(f"MCP request: method={method}, id={request_id}")

        try:
            if method == "initialize":
                result = self.handle_initialize(params)
            elif method == "initialized":
                # Client acknowledgment - no response needed
                result = {}
            elif method == "tools/list":
                result = self.handle_tools_list()
            elif method == "tools/call":
                result = self.handle_tools_call(params)
            elif method == "resources/list":
                result = self.handle_resources_list()
            elif method == "resources/read":
                result = self.handle_resources_read(params)
            elif method == "prompts/list":
                result = self.handle_prompts_list()
            elif method == "prompts/get":
                result = self.handle_prompts_get(params)
            elif method == "ping":
                result = {}
            else:
                logger.warning(f"Unknown MCP method: {method}")
                return self._error_response(request_id, -32601, f"Method not found: {method}")

            return self._success_response(request_id, result)

        except Exception as e:
            logger.exception(f"Error handling MCP request: {e}")
            return self._error_response(request_id, -32603, str(e))

    def handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Handle initialize request.

        Returns server capabilities and protocol version.
        """
        client_info = params.get("clientInfo", {})
        logger.info(
            f"MCP client connected: {client_info.get('name', 'unknown')} "
            f"v{client_info.get('version', '?')}"
        )

        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO,
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {},
            },
            "instructions": SERVER_INSTRUCTIONS.strip(),
        }

    def handle_tools_list(self) -> dict[str, Any]:
        """Return list of available tools."""
        return {"tools": self.tools}

    def handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a tool call.

        Args:
            params: Tool call parameters (name, arguments)

        Returns:
            Tool execution result
        """
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        logger.info(f"Tool call: {tool_name} with args: {arguments}")

        try:
            if tool_name == "lov":
                content = self.lovdata.lookup_law(
                    arguments.get("lov_id", ""),
                    arguments.get("paragraf"),
                    max_tokens=arguments.get("max_tokens"),
                )
            elif tool_name == "forskrift":
                content = self.lovdata.lookup_regulation(
                    arguments.get("forskrift_id", ""),
                    arguments.get("paragraf"),
                    max_tokens=arguments.get("max_tokens"),
                )
            elif tool_name == "sok":
                query = arguments.get("query", "")
                limit = arguments.get("limit", 20)
                departement = arguments.get("departement")
                doc_type = arguments.get("doc_type")
                rettsomrade = arguments.get("rettsomrade")
                inkluder_endringslover = arguments.get("inkluder_endringslover", False)
                content = self.lovdata.search(
                    query,
                    limit,
                    exclude_amendments=not inkluder_endringslover,
                    ministry_filter=departement,
                    doc_type_filter=doc_type,
                    legal_area_filter=rettsomrade,
                )
            elif tool_name == "semantisk_sok":
                query = arguments.get("query", "")
                limit = arguments.get("limit", 10)
                doc_type = arguments.get("doc_type")
                ministry = arguments.get("ministry")
                rettsomrade = arguments.get("rettsomrade")
                inkluder_endringslover = arguments.get("inkluder_endringslover", False)
                content = self._handle_semantic_search(
                    query,
                    limit,
                    doc_type,
                    ministry,
                    not inkluder_endringslover,
                    legal_area=rettsomrade,
                )
            elif tool_name == "hent_flere":
                lov_id = arguments.get("lov_id", "")
                paragrafer = arguments.get("paragrafer", [])
                max_tokens = arguments.get("max_tokens")
                content = self.lovdata.lookup_sections_batch(
                    lov_id, paragrafer, max_tokens=max_tokens
                )
            elif tool_name == "liste":
                content = self.lovdata.list_available_laws()
            elif tool_name == "sync":
                force = arguments.get("force", False)
                results = self.lovdata.sync(force=force)
                content = self._format_sync_results(results)
            elif tool_name == "status":
                status = self.lovdata.get_sync_status()
                content = self._format_status(status)
            elif tool_name == "sjekk_storrelse":
                size_info = self.lovdata.get_section_size(
                    arguments.get("lov_id", ""), arguments.get("paragraf", "")
                )
                content = self._format_size_check(
                    arguments.get("lov_id", ""), arguments.get("paragraf", ""), size_info
                )
            elif tool_name == "relaterte_forskrifter":
                lov_id = arguments.get("lov_id", "")
                content = self.lovdata.get_related_regulations(lov_id)
            elif tool_name == "departementer":
                content = self.lovdata.list_ministries()
            elif tool_name == "rettsomrader":
                content = self.lovdata.list_legal_areas()
            else:
                content = f"Ukjent verktøy: {tool_name}"
                logger.warning(f"Unknown tool requested: {tool_name}")

            return {"content": [{"type": "text", "text": content}]}

        except Exception as e:
            logger.exception(f"Tool execution error: {e}")
            return {
                "content": [{"type": "text", "text": f"Feil ved kjøring av {tool_name}: {str(e)}"}],
                "isError": True,
            }

    def _format_sync_results(self, results: dict[str, int]) -> str:
        """Format sync results for display."""
        lines = ["## Synkronisering fullført\n"]

        total = 0
        for dataset, count in results.items():
            if count >= 0:
                lines.append(f"- **{dataset}**: {count} dokumenter indeksert")
                total += count
            else:
                lines.append(f"- **{dataset}**: Feilet")

        lines.append(f"\n**Totalt:** {total} dokumenter")
        lines.append("\n*Lovdata er nå tilgjengelig for oppslag og sok.*")

        return "\n".join(lines)

    def _format_status(self, status: dict) -> str:
        """Format sync status for display."""
        if not status:
            return """## Lovdata Status

**Status:** Ikke synkronisert

Kjør `sync()` for å laste ned lovdata fra Lovdata API.
"""

        lines = ["## Lovdata Status\n"]

        # Show backend type
        lines.append(f"**Backend:** {self.lovdata.get_backend_type()}\n")

        for dataset, info in status.items():
            lines.append(f"### {dataset.title()}")
            lines.append(f"- **Sist synkronisert:** {info.get('synced_at', 'Ukjent')}")
            lines.append(f"- **Antall filer:** {info.get('file_count', 0)}")
            lines.append(f"- **Kilde oppdatert:** {info.get('last_modified', 'Ukjent')}")
            lines.append("")

        return "\n".join(lines)

    def _format_size_check(self, lov_id: str, paragraf: str, size_info: dict | None) -> str:
        """Format size check result."""
        if not size_info:
            return f"Fant ikke § {paragraf} i {lov_id}."

        tokens = size_info.get("estimated_tokens", 0)
        chars = size_info.get("char_count", 0)

        # Determine if this is a large response
        if tokens > 5000:
            warning = (
                f"\n**Advarsel:** Denne paragrafen er stor ({tokens:,} tokens). "
                f"Vurder å be brukeren om bekreftelse før henting, "
                f"eller bruk `max_tokens` parameter for å begrense."
            )
        elif tokens > 2000:
            warning = "\n*Mellomstor paragraf - bør gå greit å hente.*"
        else:
            warning = "\n*Liten paragraf - trygt å hente.*"

        return f"""## Størrelse: {lov_id} § {paragraf}

- **Tegn:** {chars:,}
- **Estimerte tokens:** {tokens:,}
{warning}
"""

    def _handle_semantic_search(
        self,
        query: str,
        limit: int = 10,
        doc_type: str | None = None,
        ministry: str | None = None,
        exclude_amendments: bool = True,
        legal_area: str | None = None,
    ) -> str:
        """
        Handle semantic search using hybrid vector + FTS.

        Args:
            query: Natural language search query
            limit: Max results
            doc_type: Filter by "lov" or "forskrift"
            ministry: Filter by ministry (partial match)
            exclude_amendments: Exclude amendment laws from results
            legal_area: Filter by legal area (partial match)

        Returns:
            Formatted search results
        """
        try:
            vector_search = self._get_vector_search()
            results = vector_search.search(
                query=query,
                limit=limit,
                doc_type=doc_type,
                ministry=ministry,
                exclude_amendments=exclude_amendments,
                legal_area=legal_area,
            )
        except Exception as e:
            logger.warning(f"Semantic search failed, falling back to FTS: {e}")
            return self.lovdata.search(query, limit)

        if not results:
            return f"Ingen treff for: {query}"

        lines = [f"## Semantisk søk: {query}\n"]

        if doc_type or ministry or legal_area:
            filters = []
            if doc_type:
                filters.append(f"type={doc_type}")
            if ministry:
                filters.append(f"departement={ministry}")
            if legal_area:
                filters.append(f"rettsomrade={legal_area}")
            lines.append(f"*Filter: {', '.join(filters)}*\n")

        lines.append(f"Fant {len(results)} relevante paragrafer:\n")

        for r in results:
            # Format reference
            ref = (
                f"{r.short_title} § {r.section_id}"
                if r.short_title
                else f"{r.dok_id} § {r.section_id}"
            )

            # Truncate content for snippet
            snippet = r.content[:300] + "..." if len(r.content) > 300 else r.content

            lines.append(f"### {ref}")
            if r.title:
                lines.append(f"**{r.title}**")
            score_line = f"*Score: {r.combined_score:.2f} (vektor: {r.similarity:.2f}, FTS: {r.fts_rank:.2f})*"
            if r.legal_area:
                score_line += f" | *{r.legal_area}*"
            lines.append(score_line)
            lines.append(f"\n{snippet}\n")
            lines.append(f'→ `lov("{r.dok_id}", "{r.section_id}")`\n')

        return "\n".join(lines)

    def handle_resources_list(self) -> dict[str, Any]:
        """Return list of available resources."""
        return {"resources": []}

    def handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read a resource by URI."""
        uri = params.get("uri", "")
        logger.warning(f"Unknown resource URI: {uri}")
        return {"contents": []}

    def handle_prompts_list(self) -> dict[str, Any]:
        """Return list of available prompts."""
        return {
            "prompts": [
                {
                    "name": "paragraf-guide",
                    "description": (
                        "Komplett brukerveiledning for Paragraf. "
                        "Inkluderer tilgjengelige verktøy, aliaser, begrensninger og tips."
                    ),
                    "arguments": [],
                }
            ]
        }

    def handle_prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Get a specific prompt by name.

        Args:
            params: Prompt parameters (name, arguments)

        Returns:
            Prompt content
        """
        prompt_name = params.get("name", "")

        if prompt_name == "paragraf-guide":
            return {
                "description": "Brukerveiledning for Paragraf",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": SERVER_INSTRUCTIONS.strip()},
                    }
                ],
            }

        return {"description": f"Ukjent prompt: {prompt_name}", "messages": []}

    def _success_response(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        """Format successful JSON-RPC response."""
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error_response(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        """Format error JSON-RPC response."""
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
