"""standard.site_claims: every website an outside source publishes for a public body, with who it says the body is.

A claim is one source row that gives a website (or, for Illinois, a mail domain) and the body's name and place,
copied as published. Which of our records a claim belongs to is a judgement, made in labels.website_matches.

What counts as a claim:
- SAM.gov: registrations with an ENTITY URL of US government entities (structure 2A), and of tax-exempt corporations
  (8H) whose NAICS codes include fire protection (922160), water supply (221310), libraries (519120, 519210) or
  transit (4851, 4852, 485991, 485999);
- the .gov list: every domain except federal ones;
- Texas Comptroller: each district's latest report year, when it gives a website;
- Colorado DOLA: each district (its first row), when it gives a website;
- New York Authorities Budget Office: each authority with a website;
- New York Department of State: each county, city, town and village with a website;
- Delaware: each municipality with a website;
- CAL FIRE: each California local fire agency with a website (with its FDID);
- Wisconsin DPI: each public library and branch with a website (with its IMLS FSCS ID);
- USAC E-Rate: each active library and library system with a website (with its IMLS FSCS ID where given);
- state library lists (MA, NJ, VT, UT, WA, PA, MN, VA; Texas with its FSCS ID): each library with a website (branches named as such);
- EPA: each active community water system whose contact e-mail has a domain (website_basis 'email_domain', like Illinois),
  with the system's name and place from the water system table;
- Wikidata: each item with an official website and a FIPS place or county code, an NCES district ID or an IMLS
  library ID (crowd-edited, so matched by exact ID only);
- IRS 990-N: each organisation not marked terminated, with something in its website field, whose name says it is a
  fire, rescue, ambulance, library, water or transit body;
- Oregon Library Directory: each library with a website;
- Illinois Comptroller: each unit whose contact e-mail has a domain (website_basis 'email_domain': the domain is a
  lead, not a published website, and labels treat it so).
"""

from __future__ import annotations

import duckdb

from .. import catalogue, tables
from ..catalogue import Column
from ..raw import illinois, irs, sam, tslac

SAM, DOTGOV, TEXAS = sam.SPEC.name, "cisa_dotgov_domains", "texas_spd_entity"
COLORADO, NEW_YORK, OREGON, ILLINOIS = "co_dola_special_districts", "ny_abo_public_authorities", "or_library_directory", illinois.SPEC.name
NY_LOCALITIES, DELAWARE = "ny_locality_websites", "de_municipalities"
CALIFORNIA_FIRE, WISCONSIN = "ca_calfire_local_fire_districts", "wi_dpi_public_libraries"
USAC, IRS = "usac_erate_libraries", irs.SPEC.name
STATE_LIBRARIES = ("ma_mblc_libraries", "nj_dca_libraries", "vt_public_libraries", "ut_public_libraries", "wa_statewide_libraries",
                   "pa_edna_libraries", "mn_mde_public_libraries", "va_vgin_public_libraries")
TEXAS_LIBRARIES = tslac.SPEC.name
EPA_DOMAINS = "epa_sdwis_email_domains"
WIKIDATA = ("wd_places", "wd_counties", "wd_nces_districts", "wd_libraries")
SOURCES = (SAM, DOTGOV, TEXAS, COLORADO, NEW_YORK, OREGON, ILLINOIS, NY_LOCALITIES, DELAWARE, CALIFORNIA_FIRE, WISCONSIN, USAC, IRS,
           *STATE_LIBRARIES, TEXAS_LIBRARIES, *WIKIDATA, EPA_DOMAINS, "epa_sdwis_water_system")
# IRS 990-N: only organisations whose name says they are a kind of body we track (the rest are other charities).
IRS_KINDS = r"FIRE|RESCUE|HOSE|HOOK AND LADDER|HOOK & LADDER|AMBULANCE|EMERGENCY|LIBRARY|WATER|TRANSIT|TRANSPORTATION"
NONPROFIT_NAICS = r"(^|~)(922160|221310|519120|519210|4851\d\d|4852\d\d|485991|485999)"

SPEC = catalogue.register(catalogue.Table(
    dataset="standard", name="site_claims", grain="website an outside source publishes for a public body (one source row)",
    description="Websites published by SAM.gov, the .gov list and state sources (Texas, Colorado, New York, Oregon, Illinois), "
                "with each body's name and place as published (see ddwb/standard/site_claims.py)",
    columns=(
        Column("claim_id", "'<list>#<row>'", "bookkeeping"),
        Column("list", "the raw table the claim comes from", "bookkeeping"),
        Column("source_row", "the row of that raw table", "copy", "raw.<list>._row"),
        Column("edition", "the edition of the source", "copy", "raw.<list>._edition"),
        Column("website", "the website as published; for a domain (the .gov list, Illinois mail domains) 'https://' is put in front",
               "copy", "SAM ENTITY URL; .gov Domain name; Texas wbst_url_tx; Colorado url; New York Website; Oregon Website; "
               "Illinois Email_GOV_domain"),
        Column("website_basis", "published_website, or email_domain (Illinois: the domain of the unit's contact e-mail)", "bookkeeping"),
        Column("name", "the body's name as published", "copy",
               "SAM LEGAL BUSINESS NAME; .gov Organization name; Texas ent_dis_nm; Colorado name; New York Public Authority Name; "
               "Oregon Library Name; Illinois UnitName; CAL FIRE Name; Wisconsin LIBRARY; USAC entity_name; IRS Organization Name"),
        Column("sub_name", "the part of the body the website is for, when the source names one", "copy",
               "SAM ENTITY DIVISION NAME; .gov Suborganization name; Oregon Alt/Branch Library Name"),
        Column("dba_name", "SAM: the doing-business-as name", "copy", "SAM DBA NAME"),
        Column("state", "state postal code", "copy", "SAM PHYSICAL ADDRESS PROVINCE OR STATE; .gov State; the state of a state source"),
        Column("county", "the county, by name, as published", "copy", "Oregon County; Illinois County; New York County Name"),
        Column("county_fips5", "5-digit county FIPS code, as published", "copy", "New York County FIPS; Wikidata P882"),
        Column("place_fips", "7-digit place FIPS code (state + place)", "split", "Delaware GEOID10; Wikidata P774 without its '-'"),
        Column("nces_leaid", "NCES district ID", "copy", "Wikidata P2483"),
        Column("fscskey", "IMLS FSCS library ID", "copy", "Wikidata P6618"),
        Column("city", "city as published", "copy", "SAM PHYSICAL ADDRESS CITY; .gov City; Texas city_nm; Colorado physCity, "
               "else mailCity; New York City; Oregon Physical City; Illinois City"),
        Column("zip5", "the first five digits of the ZIP code", "split", "SAM PHYSICAL ADDRESS ZIP/POSTAL CODE; Illinois ZIP"),
        Column("kind_codes", "the source's own codes or words for the kind of body", "copy",
               "SAM BUS TYPE STRING; .gov Domain type; Texas ent_ty_tx; Colorado lgTypeId; New York Public Authority Type; "
               "Oregon Library Type and Location Type; Illinois Description"),
        Column("naics_codes", "SAM: the NAICS code string", "copy", "SAM NAICS CODE STRING"),
        Column("entity_structure", "SAM: 2A government entity, 8H tax-exempt corporation", "copy", "SAM ENTITY STRUCTURE"),
        Column("uei", "SAM: the Unique Entity ID", "copy", "SAM UNIQUE ENTITY ID"),
        Column("source_id", "the source's own ID for the body", "copy",
               "SAM UNIQUE ENTITY ID; .gov Domain name; Texas spd_publ_id; Colorado lgId; Oregon LIBID (the IMLS ID for public "
               "libraries); Illinois Code; CAL FIRE FDID; Wisconsin FSCSKEY; USAC fscs_key; IRS EIN"),
        Column("report_year", "Texas: the report year the claim comes from", "copy", "Texas rpt_yr"),
    ),
    inputs=tuple(f"raw.{t}" for t in SOURCES) + ("standard.code_states",),
))

COLUMNS = [c.name for c in SPEC.columns]


def _part(list_name: str, source: str, where: str, **exprs: str) -> str:
    exprs = {"claim_id": f"'{list_name}#' || _row", "list": f"'{list_name}'", "source_row": "_row", "edition": "_edition",
             "website_basis": "'published_website'", **exprs}
    return f"SELECT {', '.join(f'CAST({exprs.get(c, 'NULL')} AS VARCHAR) AS {c}' if c != 'source_row' else '_row AS source_row' for c in COLUMNS)} FROM {source} WHERE {where}"


def build(run_id: str) -> dict[str, int]:
    con = duckdb.connect()
    states = tables.latest("standard", "code_states")
    parts = [
        _part(SAM, tables.latest("raw", SAM),
              f""""ENTITY URL" IS NOT NULL AND ("ENTITY STRUCTURE" = '2A' OR regexp_matches(coalesce("NAICS CODE STRING", ''), '{NONPROFIT_NAICS}'))""",
              website='"ENTITY URL"', name='"LEGAL BUSINESS NAME"', sub_name='"ENTITY DIVISION NAME"', dba_name='"DBA NAME"',
              state='"PHYSICAL ADDRESS PROVINCE OR STATE"', city='"PHYSICAL ADDRESS CITY"', zip5='left("PHYSICAL ADDRESS ZIP/POSTAL CODE", 5)',
              kind_codes='"BUS TYPE STRING"', naics_codes='"NAICS CODE STRING"', entity_structure='"ENTITY STRUCTURE"',
              uei='"UNIQUE ENTITY ID"', source_id='"UNIQUE ENTITY ID"'),
        _part(DOTGOV, tables.latest("raw", DOTGOV), "\"Domain type\" NOT LIKE 'Federal%'",
              website="'https://' || lower(\"Domain name\")", name='"Organization name"', sub_name='"Suborganization name"',
              state='"State"', city='"City"', kind_codes='"Domain type"', source_id='lower("Domain name")'),
        _part(TEXAS, f"(SELECT * FROM {tables.latest('raw', TEXAS)} QUALIFY rpt_yr = max(rpt_yr) OVER (PARTITION BY spd_publ_id))",
              "wbst_url_tx IS NOT NULL", website="wbst_url_tx", name="ent_dis_nm", state="'TX'", city="city_nm",
              kind_codes="ent_ty_tx", source_id="spd_publ_id", report_year="rpt_yr"),
        _part(COLORADO, f"(SELECT * FROM {tables.latest('raw', COLORADO)} QUALIFY row_number() OVER (PARTITION BY lgId ORDER BY _row) = 1)",
              "url IS NOT NULL", website="url", name="name", state="'CO'", city="coalesce(physCity, mailCity)",
              kind_codes="lgTypeId", source_id="lgId"),
        _part(NEW_YORK, tables.latest("raw", NEW_YORK), '"Website" IS NOT NULL', website='"Website"', name='"Public Authority Name"',
              state="'NY'", city='"City"', kind_codes='"Public Authority Type"', source_id='"Public Authority Name"'),
        _part(OREGON, tables.latest("raw", OREGON), '"Website" IS NOT NULL', website='"Website"', name='"Library Name"',
              sub_name='"Alt/Branch Library Name"', state="'OR'", county='"County"', city='"Physical City"',
              kind_codes='"Library Type" || \' \' || "Location Type"', source_id='"LIBID"'),
        _part(ILLINOIS, tables.latest("raw", ILLINOIS), "Email_GOV_domain IS NOT NULL", website="'https://' || Email_GOV_domain",
              website_basis="'email_domain'", name="UnitName", state="'IL'", county="County", city="City", zip5="left(ZIP, 5)",
              kind_codes="Description", source_id="Code"),
        _part(NY_LOCALITIES, tables.latest("raw", NY_LOCALITIES), '"Website" IS NOT NULL', website='"Website"',
              name='"Type" || \' of \' || "Municipality"', state="'NY'", county='"County Name"', county_fips5='"County FIPS"',
              kind_codes='"Type"', source_id='"SWIS Code"'),
        _part(DELAWARE, tables.latest("raw", DELAWARE), '"WEBSITE" IS NOT NULL', website='"WEBSITE"', name='"NAME"', state="'DE'",
              place_fips='"GEOID10"', kind_codes="'municipality'", source_id='"GEOID10"'),
        _part(CALIFORNIA_FIRE, tables.latest("raw", CALIFORNIA_FIRE), '"Website" IS NOT NULL', website='"Website"', name='"Name"',
              state="'CA'", county='"County"', city='"City"', zip5='left("Zip", 5)', kind_codes="'fire agency'", source_id='"FDID"'),
        _part(WISCONSIN, tables.latest("raw", WISCONSIN), '"WEBSITE" IS NOT NULL', website='"WEBSITE"', name='"LIBRARY"',
              sub_name='CASE WHEN "LIB_TYPE" LIKE \'Branch%\' THEN "LIBRARY" END', state="'WI'", county='"COUNTY"', city='"PHYSCITY"',
              zip5='left("PHYSZIP", 5)', kind_codes='"LIB_TYPE"', source_id='"FSCSKEY"'),
        _part(USAC, tables.latest("raw", USAC), "website_url IS NOT NULL AND status = 'Active'", website="website_url",
              name="entity_name", sub_name="CASE WHEN upper(entity_name) LIKE '%BRANCH%' THEN entity_name END",
              state="physical_state", county="physical_county", city="physical_city", zip5="left(physical_zipcode, 5)",
              kind_codes="entity_type", source_id="fscs_key"),
        _part(IRS, tables.latest("raw", IRS),
              f"""\"Website URL\" IS NOT NULL AND coalesce(\"Organization Has Terminated\", 'F') <> 'T'
                  AND regexp_matches(upper(\"Organization Name\"), '{IRS_KINDS}')""",
              website='"Website URL"', name='"Organization Name"', dba_name='"Doing Business As Name 1"',
              state='"Organization Address State"', city='"Organization Address City"', zip5='left("Organization Address Zip", 5)',
              kind_codes="'990-N filer'", source_id='"EIN"'),
        _part("ma_mblc_libraries", tables.latest("raw", "ma_mblc_libraries"), "WEBSITE IS NOT NULL AND TYPE IN ('PUBLIC', 'PUBLIC BRANCH')",
              website="WEBSITE", name="NAME", sub_name="CASE WHEN TYPE = 'PUBLIC BRANCH' THEN NAME END", state="'MA'", city="TOWN",
              zip5="left(ZIP, 5)", kind_codes="TYPE", source_id="LIBKEY"),
        _part("nj_dca_libraries", tables.latest("raw", "nj_dca_libraries"), "Links IS NOT NULL", website="Links", name="Library",
              sub_name="CASE WHEN upper(Library) LIKE '%BRANCH%' THEN Library END", state="'NJ'", city="Town", kind_codes="'library'"),
        _part("vt_public_libraries", tables.latest("raw", "vt_public_libraries"), "WEBSITE IS NOT NULL", website="WEBSITE",
              name="LIBRARY", sub_name="CASE WHEN upper(LIBRARY) LIKE '%BRANCH%' THEN LIBRARY END", state="'VT'", county="COUNTY", city="TOWNNAME", kind_codes="'library'"),
        _part("ut_public_libraries", tables.latest("raw", "ut_public_libraries"), "WEBSITE IS NOT NULL AND TYPE = 'Public'",
              website="WEBSITE", name="LIBRARY", sub_name="CASE WHEN upper(LIBRARY) LIKE '%BRANCH%' THEN LIBRARY END", state="'UT'", county="COUNTY", city="CITY",
              zip5="left(ZIP, 5)", kind_codes="TYPE"),
        _part("wa_statewide_libraries", tables.latest("raw", "wa_statewide_libraries"),
              "LDLI_URL IS NOT NULL AND LDTC_TypeDescription = 'Public Libraries'", website="LDLI_URL", name="Library",
              state="'WA'", county="LDLI_County", city="LDLI_City", zip5="left(LDLI_Zip, 5)", kind_codes="LDLI_Category"),
        _part("pa_edna_libraries", tables.latest("raw", "pa_edna_libraries"), "webaddress IS NOT NULL", website="webaddress",
              name="instname", sub_name="CASE WHEN upper(instname) LIKE '%BRANCH%' THEN instname END", state="'PA'", city="city", zip5="left(zipcode, 5)",
              kind_codes="'library'", source_id="aun"),
        _part("mn_mde_public_libraries", tables.latest("raw", "mn_mde_public_libraries"), "web_url IS NOT NULL", website="web_url",
              name="mdename", state="'MN'", county="countyname", kind_codes="'library'", source_id="orgnumber"),
        _part("va_vgin_public_libraries", tables.latest("raw", "va_vgin_public_libraries"), "URL IS NOT NULL", website="URL",
              name="LandmkName", sub_name="CASE WHEN upper(LandmkName) LIKE '%BRANCH%' THEN LandmkName END", state="'VA'",
              city="City", zip5="left(Zip, 5)", kind_codes="'library'"),
        _part(TEXAS_LIBRARIES, tables.latest("raw", TEXAS_LIBRARIES), '"Web URL" IS NOT NULL', website='"Web URL"',
              name='"Library Name"', state="'TX'", county='"County"', city='"Street City"', zip5='left("Street ZIP Code", 5)',
              kind_codes='"Legal Establishment"', source_id='"Library ID"', fscskey='"FSCS"'),
        _part(EPA_DOMAINS, f"""(SELECT d.*, w.pws_name, w.state_code, w.city_name FROM {tables.latest('raw', EPA_DOMAINS)} d
                                 JOIN {tables.latest('raw', 'epa_sdwis_water_system')} w USING (pwsid))""",
              "email_domain IS NOT NULL", website="'https://' || email_domain", website_basis="'email_domain'", name="pws_name",
              state="state_code", city="city_name", kind_codes="'water system'", source_id="pwsid"),
        _part("wd_places", tables.latest("raw", "wd_places"), "website IS NOT NULL", website="website", name="label",
              place_fips="replace(id, '-', '')", state=f"(SELECT postal FROM {states} WHERE fips_state = left(id, 2))",
              kind_codes="'wikidata place'", source_id="item"),
        _part("wd_counties", tables.latest("raw", "wd_counties"), "website IS NOT NULL", website="website", name="label",
              county_fips5="id", state=f"(SELECT postal FROM {states} WHERE fips_state = left(id, 2))",
              kind_codes="'wikidata county'", source_id="item"),
        _part("wd_nces_districts", tables.latest("raw", "wd_nces_districts"), "website IS NOT NULL", website="website", name="label",
              nces_leaid="id", kind_codes="'wikidata school district'", source_id="item"),
        _part("wd_libraries", tables.latest("raw", "wd_libraries"), "website IS NOT NULL", website="website", name="label",
              fscskey="id", state="left(id, 2)", kind_codes="'wikidata library'", source_id="item"),
    ]
    select = f"SELECT * REPLACE (CAST(source_row AS BIGINT) AS source_row) FROM ({' UNION ALL '.join(parts)}) ORDER BY list, source_row"
    inputs = {f"raw.{t}": tables.latest_run(con, "raw", t) for t in SOURCES}
    inputs["standard.code_states"] = tables.latest_run(con, "standard", "code_states")
    return {SPEC.key: tables.write(con, SPEC, select, run_id, inputs)}
