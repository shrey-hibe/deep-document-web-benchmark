"""Raw tables for the agencies' CSV files and spreadsheets, and the Census finance unit list (fixed width).

Each table's published columns are listed in raw/headers/<table>.txt, one per line, in file order. The build reads
the file and fails if its header differs, so a new edition with different columns is noticed, never absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from .. import catalogue, paths, tables
from . import PROVENANCE, sha256, unzip_member

HEADERS = Path(__file__).parent / "headers"


@dataclass(frozen=True)
class Source:
    table: str
    file: str  # relative to the downloads folder; 'archive.zip!member' for a file inside a zip
    agency: str
    edition: str
    grain: str
    documentation: str
    encoding: str = "utf-8"
    sheet: str = ""  # the tab to read, for a spreadsheet
    meanings: tuple[tuple[str, str], ...] = ()  # (column, meaning) for the columns later layers read

    def path(self) -> Path:
        if "!" in self.file:
            archive, member = self.file.split("!", 1)
            return unzip_member(paths.DOWNLOADS / archive, member)
        return paths.DOWNLOADS / self.file

    def archive(self) -> Path:
        return paths.DOWNLOADS / self.file.split("!", 1)[0]

    def columns(self) -> list[str]:
        return (HEADERS / f"{self.table}.txt").read_text(encoding="utf-8").splitlines()


SOURCES = (
    Source("nces_lea_directory_2025_26", "nces/ccd_lea_029_2526_w_0a_050626.zip!ccd_lea_029_2526_w_0a_050626.csv",
           "NCES", "Common Core of Data: LEA directory, school year 2025-26 (preliminary)", "local education agency (school district or similar)",
           "https://nces.ed.gov/ccd/files.asp (CCD file documentation, 2025-26 LEA directory)",
           meanings=(("LEAID", "NCES's 7-digit ID for the agency"), ("ST_LEAID", "the state's own ID for the agency"),
                     ("LEA_NAME", "the agency's name"), ("LEA_TYPE", "NCES's code for the kind of agency (1-9)"),
                     ("LEA_TYPE_TEXT", "NCES's name for that kind"), ("UPDATED_STATUS", "NCES's status code"),
                     ("UPDATED_STATUS_TEXT", "NCES's name for that status"), ("WEBSITE", "the agency's website as reported"),
                     ("ST", "state postal code"), ("FIPST", "state FIPS code"), ("CHARTER_LEA_TEXT", "whether the agency is a charter agency"))),
    Source("nces_school_directory_2025_26", "nces/ccd_sch_029_2526_w_0a_050626.zip!ccd_sch_029_2526_w_0a_050626.csv",
           "NCES", "Common Core of Data: school directory, school year 2025-26 (preliminary)", "public school",
           "https://nces.ed.gov/ccd/files.asp (CCD file documentation, 2025-26 school directory)",
           meanings=(("NCESSCH", "NCES's 12-digit ID for the school"), ("SCH_NAME", "the school's name"),
                     ("LEAID", "NCES ID of the agency the school belongs to"), ("CHARTER_TEXT", "whether the school is a charter school"))),
    Source("epa_sdwis_water_system", "epa-sdwis/sdwis_water_system_cws_active.csv",
           "EPA", "SDWIS (Envirofacts): active community water systems, all owner types, pulled 2026-10-28", "community water system",
           "https://echo.epa.gov/tools/data-downloads/sdwa-download-summary",
           meanings=(("pwsid", "EPA's ID for the water system"), ("pws_name", "the system's name"),
                     ("owner_type_code", "EPA's code for the owner: L local government, M public/private, P private, S state, F federal, N native American"),
                     ("org_name", "the owning organisation's name, where the API returned it as an organisation"),
                     ("pws_activity_code", "EPA's activity code (A = active)"), ("population_served_count", "people served"))),
    Source("epa_sdwis_email_domains", "epa-sdwis/sdwis_water_system_email_domains.csv",
           "EPA", "SDWIS (Envirofacts) WATER_SYSTEM, active community water systems: the domain of each system's contact e-mail, "
           "pulled 2026-10-31 (the e-mail address and other contact fields were dropped before saving)",
           "community water system and the domain of its contact e-mail", "https://echo.epa.gov/tools/data-downloads/sdwa-download-summary",
           meanings=(("pwsid", "EPA's ID for the water system"), ("email_domain", "the domain of the system's contact e-mail (EMAIL_ADDR)"))),
    Source("epa_sdwis_geographic_area", "epa-sdwis/sdwis_geographic_area_cws_active.csv",
           "EPA", "SDWIS (Envirofacts): geographic areas served by active community water systems, pulled 2026-10-28",
           "area served by a water system (county, city or tribal area)", "https://echo.epa.gov/tools/data-downloads/sdwa-download-summary"),
    Source("imls_pls_fy2024_administrative_entity", "imls/pls_fy2024_csv.zip!CSV/PLS_FY24_AE_pud24i.csv",
           "IMLS", "Public Libraries Survey FY2024: administrative entities (published 2026-06)", "public library administrative entity",
           "data/registries/docs/imls/ (FY2024 data file documentation, Appendix A record layout)", encoding="latin-1",
           meanings=(("FSCSKEY", "IMLS's ID for the library"), ("LIBNAME", "the library's name"),
                     ("C_LEGBAS", "IMLS's code for the library's legal basis (e.g. CI city, CO county, LD library district, NP nonprofit)"),
                     ("C_ADMIN", "IMLS's code for the administrative structure"), ("STABR", "state postal code"),
                     ("STATSTRU", "IMLS's structure-change code"), ("GEOCODE", "IMLS's code for the type of service area"))),
    Source("usfa_registry", "usfa/usfa_registry_national.csv",
           "USFA", "National Fire Department Registry, pulled 2026-10-27", "registered fire department",
           "https://apps.usfa.fema.gov/registry/download",
           meanings=(("FDID", "the department's fire department ID, unique within its state"), ("Fire dept name", "the department's name"),
                     ("HQ state", "state of the headquarters"), ("Dept Type", "USFA's department type (career, mostly career, mostly volunteer, volunteer)"),
                     ("Organization Type", "USFA's organization type"), ("Website", "the department's website as registered"))),
    Source("ntd_reporters_2025", "ntd/ntd_reporters_jm7a-9hsy_ry2025.csv",
           "FTA", "National Transit Database: reporter agency information, report year 2025",
           "transit reporter as reported through one program (a private operator such as Greyhound appears once per state program it reports through)",
           "https://data.transportation.gov/resource/jm7a-9hsy",
           meanings=(("ntd_id", "the NTD ID of the reporter"), ("agency_name", "the reporter's name"),
                     ("organization_type", "NTD's organization type (e.g. 'City, County or Local Government Unit or Department of Transportation')"),
                     ("reported_by_ntd_id", "NTD ID of the agency that reports for this one"), ("ueid", "the SAM.gov unique entity ID"),
                     ("url", "the reporter's website as reported"))),
    Source("census_f33_2024", "census-schools/elsec24.xlsx",
           "Census", "Annual Survey of School System Finances (F-33), FY2024, individual unit file (released 2026-05-07)",
           "public school system (independent district, dependent system or educational service agency)",
           "https://www2.census.gov/programs-surveys/school-finances/tables/2024/secondary-education-finance/school24doc.docx",
           sheet="elsec24",
           meanings=(("STATE", "the Census's own state number (alphabetical order: 05 = California), not the FIPS code"),
                     ("PID6", "the Census Bureau's 6-digit ID for the school system (CENSUS_ID_PID6 in the Government Units List)"),
                     ("UNIT_TYPE", "0, 1, 2, 3: a school system dependent on a state, county, city or township government; 5: independent"),
                     ("FIPST", "2-digit state FIPS (ANSI) code"), ("NAME", "the school system's name"),
                     ("CONUM", "5-digit state and county FIPS (ANSI) code"), ("SCHLEV", "school level code"),
                     ("NCESID", "NCES's 7-digit ID (LEAID) for the same school system: the Census's own link to NCES"),
                     ("YRDATA", "last two digits of the data year"), ("V33", "fall membership (pupils enrolled)"))),
    Source("cisa_dotgov_domains", "dotgov/current-full_no-contacts.csv",
           "CISA", "the .gov domain list (current-full.csv), fetched 2026-10-28; its security-contact e-mail column was dropped "
           "at download (people data)", ".gov domain",
           "https://github.com/cisagov/dotgov-data",
           meanings=(("Domain name", "the .gov domain"), ("Domain type", "CISA's kind of registrant (City, County, Special district, ...)"),
                     ("Organization name", "the registrant organisation"), ("Suborganization name", "the part of it the domain is for"),
                     ("City", "the registrant's city"), ("State", "the registrant's state (postal code)"))),
    Source("texas_spd_entity", "texas-comptroller/spdpid-entity.csv",
           "Texas Comptroller", "Special Purpose District Public Information, entity file (report years 2018-2026), downloaded 2026-10-27",
           "special purpose district's report for one year", "https://assets.comptroller.texas.gov/open-data-files/",
           meanings=(("spd_publ_id", "the Comptroller's ID for the district"), ("ent_dis_nm", "the district's name"),
                     ("rpt_yr", "the report year"), ("city_nm", "the district's city"),
                     ("cnty_cd", "the Comptroller's county number (alphabetical, 1-254), not a FIPS code"),
                     ("wbst_url_tx", "the district's website, as reported"), ("ent_ty_tx", "the kind of district"),
                     ("tp_id", "the Texas taxpayer number"))),
    Source("co_dola_special_districts", "states/co/co_dola_special_districts_7rar-v5z8.csv",
           "Colorado DOLA", "All Special Districts in Colorado (data.colorado.gov 7rar-v5z8), downloaded 2026-10-31",
           "Colorado special district with a mapped boundary (a district can have several rows)", "https://data.colorado.gov/d/7rar-v5z8",
           meanings=(("lgId", "DOLA's local government ID"), ("name", "the district's name"), ("lgTypeId", "DOLA's district type"),
                     ("lgStatus", "DOLA's status code"), ("url", "the district's website (Colorado law requires one)"),
                     ("mailCity", "mailing city"), ("physCity", "physical city"), ("geometry", "the district's boundary (WKT)"))),
    Source("ny_abo_public_authorities", "states/ny/ny_abo_public_authorities_4vym-q77x.csv",
           "New York Authorities Budget Office", "Directory of Public Authorities (data.ny.gov 4vym-q77x), downloaded 2026-10-31",
           "New York public authority", "https://data.ny.gov/d/4vym-q77x",
           meanings=(("Public Authority Name", "the authority's name"), ("Public Authority Type", "State, Local, IDA or LDC"),
                     ("Website", "the authority's website"), ("City", "its city"))),
    Source("or_library_directory", "states/or/or_library_directory_6x9d-idz4.csv",
           "State Library of Oregon", "Oregon Library Directory (data.oregon.gov 6x9d-idz4), downloaded 2026-10-31",
           "Oregon library or library branch", "https://data.oregon.gov/d/6x9d-idz4",
           meanings=(("LIBID", "the library's ID: the IMLS FSCS ID for public libraries (e.g. OR0001)"),
                     ("Library Name", "the library's name"), ("Library Type", "Public, Academic, ..."),
                     ("Location Type", "Main or Branch"), ("Website", "the library's website"))),
    Source("ny_locality_websites", "states/ny/ny_locality_hierarchy_websites_55k6-h6qq.csv",
           "New York Department of State", "NYS Locality Hierarchy with Websites (data.ny.gov 55k6-h6qq), downloaded 2026-10-31",
           "New York county, city, town or village", "https://data.ny.gov/d/55k6-h6qq",
           meanings=(("Type", "County, City, Town or Village"), ("Municipality", "the municipality's name"),
                     ("Website", "its website"), ("County FIPS", "5-digit county FIPS code"), ("GNIS ID", "USGS GNIS ID"),
                     ("SWIS Code", "New York's SWIS code"))),
    Source("de_municipalities", "states/de/de_municipalities_vkck-xjed.csv",
           "Delaware FirstMap", "Delaware Municipalities (data.delaware.gov vkck-xjed), downloaded 2026-10-31",
           "Delaware municipality", "https://data.delaware.gov/d/vkck-xjed",
           meanings=(("NAME", "the municipality's name"), ("GEOID10", "the Census 2010 place GEOID (state + place FIPS)"),
                     ("WEBSITE", "its website"), ("the_geom", "its boundary (WKT)"))),
    Source("usac_erate_libraries", "usac/usac_erate_libraries_7i5i-83qf.csv",
           "USAC", "E-Rate Supplemental Entity Information (opendata.usac.org 7i5i-83qf), libraries and library systems, "
           "non-contact columns only, downloaded 2026-10-31", "library or library system registered for E-Rate",
           "https://opendata.usac.org/d/7i5i-83qf",
           meanings=(("entity_name", "the library's name"), ("entity_type", "Library or Library System"),
                     ("website_url", "its website"), ("fscs_key", "the IMLS FSCS ID of its administrative entity"),
                     ("fscs_seq", "the IMLS outlet number"), ("main_branch", "Yes for a main library"),
                     ("physical_state", "state"), ("physical_city", "city"), ("physical_county", "county"))),
    Source("pa_edna_libraries", "states/pa/pa_edna_libraries_knsp-q7fv.csv",
           "Pennsylvania Department of Education", "EdNA, Education & Library Name, Address & GIS (data.pa.gov knsp-q7fv), "
           "rows whose name contains LIBRARY, downloaded 2026-10-31", "Pennsylvania library", "https://data.pa.gov/d/knsp-q7fv",
           meanings=(("aun", "the administrative unit number"), ("instname", "the library's name"), ("city", "city"),
                     ("webaddress", "its website"))),
    Source("wd_places", "wikidata/wd_places_p774.csv", "Wikidata", "Wikidata query of 2026-10-31: items with an official website (P856) and a FIPS 55-3 place code (P774, state-place), "
           "not dissolved (crowd-edited, CC0; not an agency source)", "Wikidata item and one of its official websites",
           "https://www.wikidata.org/wiki/Property:P856",
           meanings=(("item", "the Wikidata item"), ("label", "its English label"), ("id", "a FIPS 55-3 place code (P774, state-place)"), ("website", "its official website (P856)"))),
    Source("wd_counties", "wikidata/wd_counties_p882.csv", "Wikidata", "Wikidata query of 2026-10-31: items with an official website (P856) and a FIPS 6-4 county code (P882), "
           "not dissolved (crowd-edited, CC0; not an agency source)", "Wikidata item and one of its official websites",
           "https://www.wikidata.org/wiki/Property:P856",
           meanings=(("item", "the Wikidata item"), ("label", "its English label"), ("id", "a FIPS 6-4 county code (P882)"), ("website", "its official website (P856)"))),
    Source("wd_nces_districts", "wikidata/wd_nces_districts_p2483.csv", "Wikidata", "Wikidata query of 2026-10-31: items with an official website (P856) and an NCES district ID (P2483), "
           "not dissolved (crowd-edited, CC0; not an agency source)", "Wikidata item and one of its official websites",
           "https://www.wikidata.org/wiki/Property:P856",
           meanings=(("item", "the Wikidata item"), ("label", "its English label"), ("id", "an NCES district ID (P2483)"), ("website", "its official website (P856)"))),
    Source("wd_libraries", "wikidata/wd_libraries_p6618.csv", "Wikidata", "Wikidata query of 2026-10-31: items with an official website (P856) and an IMLS FSCS library ID (P6618), "
           "not dissolved (crowd-edited, CC0; not an agency source)", "Wikidata item and one of its official websites",
           "https://www.wikidata.org/wiki/Property:P856",
           meanings=(("item", "the Wikidata item"), ("label", "its English label"), ("id", "an IMLS FSCS library ID (P6618)"), ("website", "its official website (P856)"))),
)

SPECS = {
    s.table: catalogue.register(catalogue.Table(
        dataset="raw", name=s.table, grain=s.grain, description=f"{s.agency}: {s.edition}, exactly as published",
        columns=tuple(catalogue.Column(c, dict(s.meanings).get(c, f"as published by {s.agency}; see {s.documentation}"), "copy",
                                       f"{s.file}:{c}") for c in s.columns()) + PROVENANCE,
        inputs=(s.file,),
    ))
    for s in SOURCES
}

# Census finance unit list 2022 (fixed width, no header): positions from the Census's 2022 S&L Public Use Files
# Technical Documentation (in the same zip), record layout of Fin_PID_2022.txt.
FIN_FILE = "census/2022_Individual_Unit_File.zip!2022_Individual_Unit_files/Fin_PID_2022.txt"
FIN_LAYOUT = (  # (column, first position, length, meaning) as documented
    ("ID", 1, 12, "12-character government ID: state FIPS (2), type (1), county FIPS (3), unit ID (6)"),
    ("ID_NAME", 13, 64, "the government's name"),
    ("COUNTY_NAME", 77, 35, "county name"),
    ("FIPS_PLACE", 112, 5, "FIPS place code"),
    ("POPULATION", 117, 9, "population"),
    ("POPULATION_YEAR", 126, 2, "last two digits of the population year"),
    ("ENROLLMENT", 128, 7, "enrollment"),
    ("ENROLLMENT_YEAR", 135, 2, "last two digits of the enrollment year"),
    ("FUNCTION_CODE", 137, 2, "special districts: the Census function code"),
    ("SCHOOL_LEVEL_CODE", 139, 2, "school districts: the school level code"),
    ("FISCAL_YEAR_ENDING", 141, 4, "fiscal year ending (MMYY)"),
    ("SURVEY_YEAR", 145, 2, "last two digits of the survey year"),
)
FIN_SPEC = catalogue.register(catalogue.Table(
    dataset="raw", name="census_finance_2022_units", grain="government in the 2022 annual finance survey",
    description="Census Bureau: 2022 State and Local Government Finance, individual unit list (Fin_PID_2022.txt), exactly as published",
    columns=tuple(catalogue.Column(c, m, "copy", f"{FIN_FILE}:positions {p}-{p + n - 1}") for c, p, n, m in FIN_LAYOUT) + PROVENANCE,
    inputs=(FIN_FILE,),
))


def _check_header(con: duckdb.DuckDBPyConnection, source: Source, read: str) -> None:
    found = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {read}").fetchall()]
    if found != source.columns():
        raise ValueError(f"{source.file}: header differs from raw/headers/{source.table}.txt")


def build(run_id: str) -> dict[str, int]:
    con = duckdb.connect()
    con.execute("LOAD excel; SET threads = 1; SET preserve_insertion_order = true")  # keeps rows in file order for _row
    counts = {}
    for source in SOURCES:
        spec = SPECS[source.table]
        if source.sheet:
            read = f"read_xlsx('{source.path()}', sheet = '{source.sheet}', all_varchar = true)"
        else:
            read = (f"read_csv('{source.path()}', all_varchar = true, header = true, encoding = '{source.encoding}', "
                    f"quote = '\"', escape = '\"', strict_mode = false, max_line_size = 20000000)")
        _check_header(con, source, read)
        file_hash = sha256(source.archive())
        select = f"""SELECT *, '{source.file}' AS _file, '{source.sheet}' AS _sheet, row_number() OVER () AS _row,
                            '{file_hash}' AS _file_sha256, '{source.edition.replace("'", "''")}' AS _edition FROM {read}"""
        counts[spec.key] = tables.write(con, spec, select, run_id, {source.file.split('!')[0]: file_hash})
    archive, member = FIN_FILE.split("!", 1)
    text = unzip_member(paths.DOWNLOADS / archive, member)
    fields = ", ".join(f"rtrim(substr(line, {p}, {n})) AS {c}" for c, p, n, _ in FIN_LAYOUT)
    file_hash = sha256(paths.DOWNLOADS / archive)
    select = f"""SELECT {fields}, '{FIN_FILE}' AS _file, '' AS _sheet, row_number() OVER () AS _row,
                        '{file_hash}' AS _file_sha256, 'Census of Governments: 2022 finance, individual unit list' AS _edition
                 FROM read_csv('{text}', columns = {{'line': 'VARCHAR'}}, header = false, delim = '\\x01', quote = '',
                               encoding = 'latin-1', strict_mode = false)
                 WHERE trim(line) <> ''"""
    counts[FIN_SPEC.key] = tables.write(con, FIN_SPEC, select, run_id, {archive: file_hash})
    return counts
