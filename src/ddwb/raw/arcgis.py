"""Raw tables for state map layers (ArcGIS feature services), downloaded as JSON with only the fields listed.

Each download asked the service for named fields only, so people data (a fire chief's or library director's name
and e-mail) was never fetched. The fields asked for are the table's columns, in that order; the download URL is in
data/registries/states/_fetch_log.tsv.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import duckdb

from .. import catalogue, paths, tables
from . import PROVENANCE, sha256


@dataclass(frozen=True)
class Layer:
    table: str
    file: str
    publisher: str
    edition: str
    grain: str
    fields: tuple[tuple[str, str], ...]  # (field, meaning), in the order asked for


LAYERS = (
    Layer("ca_calfire_local_fire_districts", "states/ca/ca_calfire_local_fire_districts.json", "CAL FIRE",
          "California Local Fire Districts (FeatureServer FireDistricts26_1), downloaded 2026-10-31", "California local fire agency",
          (("OBJECTID", "the layer's row ID"), ("County", "county"), ("NERISID", "NERIS ID"),
           ("FDID", "the fire department ID (the USFA registry's FDID)"), ("MACSID", "MACS ID"), ("Name", "the agency's name"),
           ("Address", "address"), ("City", "city"), ("Zip", "ZIP code"), ("Notes", "notes"), ("Website", "the agency's website"),
           ("CALFIREUnit", "the CAL FIRE unit"), ("LastUpdated", "when the row was last updated (epoch milliseconds)"))),
    Layer("wi_dpi_public_libraries", "states/wi/wi_dpi_public_libraries.json", "Wisconsin DPI",
          "Public Libraries and Branches, Wisconsin (FeatureServer layer 6), downloaded 2026-10-31", "Wisconsin public library or branch",
          (("OBJECTID", "the layer's row ID"), ("LIBID", "the state library ID"), ("FSCSKEY", "the IMLS FSCS ID"),
           ("LIBRARY", "the library's name"), ("LIB_TYPE", "the kind of library (Municipal Library, Branch, ...)"),
           ("AGENCYTYPE", "the agency type"), ("LIB_SYSTEM", "the regional library system"), ("PARENT_LIB", "the parent library's ID"),
           ("COUNTY", "county"), ("PHYSADDR", "address"), ("PHYSCITY", "city"), ("PHYSSTATE", "state"), ("PHYSZIP", "ZIP code"),
           ("WEBSITE", "the library's website (DPI caps it at 55 characters)"), ("LOCALE", "NCES locale code"))),
    Layer("ma_mblc_libraries", "states/ma/ma_mblc_libraries.json", "MassGIS / MBLC", "Libraries (FeatureServer Libraries/0), downloaded 2026-10-31", "library or library branch",
          (('OBJECTID', "the layer's row ID"), ('NAME', "the library's name"), ('TYPE', 'PUBLIC, PUBLIC BRANCH or SPECIAL'), ('WEBSITE', 'its website'), ('TOWN', 'town'), ('STATE', 'state'), ('ZIP', 'ZIP code'), ('NETWORK', 'its library network'), ('LIBKEY', 'the MBLC library key'))),
    Layer("nj_dca_libraries", "states/nj/nj_dca_libraries_2025.json", "New Jersey DCA", "Libraries_2025 (FeatureServer Libraries_2023/11), downloaded 2026-10-31", "library or library branch",
          (('OBJECTID', "the layer's row ID"), ('Library', "the library's name"), ('Town', 'town'), ('Address', 'address'), ('Links', 'its website'))),
    Layer("vt_public_libraries", "states/vt/vt_public_libraries.json", "Vermont Center for Geographic Information", "Vermont public libraries (FeatureServer FS_VCGI_OPENDATA_Facilities_VTPUBLIB_point_SP_v1/0), downloaded 2026-10-31", "library or library branch",
          (('OBJECTID', "the layer's row ID"), ('LIBRARY', "the library's name"), ('ADDRESS', 'address'), ('TOWNNAME', 'town'), ('COUNTY', 'county'), ('WEBSITE', 'its website'))),
    Layer("ut_public_libraries", "states/ut/ut_ugrc_public_libraries.json", "Utah UGRC", "Utah Public Libraries (FeatureServer PublicLibraries/0), downloaded 2026-10-31", "library or library branch",
          (('OBJECTID', "the layer's row ID"), ('LIBRARY', "the library's name"), ('TYPE', 'Public, Bookmobile or Tribal'), ('ADDRESS', 'address'), ('CITY', 'city'), ('ZIP', 'ZIP code'), ('COUNTY', 'county'), ('WEBSITE', 'its website'))),
    Layer("wa_statewide_libraries", "states/wa/wa_statewide_libraries.json", "Washington State Library", "Statewide Libraries (FeatureServer Statewide_Libraries/0), downloaded 2026-10-31", "library or library branch",
          (('OBJECTID', "the layer's row ID"), ('LDTC_TypeDescription', 'Public, Academic or Special Libraries'), ('LDLI_Category', 'District, Municipal, ...'), ('Library', "the library system's name"), ('Branch', "the branch's name"), ('SecondaryName', 'another name'), ('LDLI_City', 'city'), ('LDLI_State', 'state'), ('LDLI_Zip', 'ZIP code'), ('LDLI_County', 'county'), ('LDLI_URL', 'its website'))),
    Layer("mn_mde_public_libraries", "states/mn/mn_mde_public_libraries.json", "Minnesota Department of Education",
          "School Program Locations (FeatureServer struc_school_program_locs/0), rows orgtype 89 (public library), loctype Head, "
          "downloaded 2026-10-31", "Minnesota public library (head)",
          (("objectid", "the layer's row ID"), ("orgid", "MDE organisation ID"), ("orgtype", "89 = public library"),
           ("orgnumber", "MDE organisation number"), ("mdename", "the library's name"), ("gisname", "the library's map name"),
           ("loctype", "Head"), ("countyname", "county"), ("web_url", "its website"))),
    Layer("va_vgin_public_libraries", "states/va/va_vgin_public_libraries.json", "Virginia VGIN (Library of Virginia data)",
          "Virginia Landmark Locations (FeatureServer VA_Landmarks/1), rows PlaceType 'Public Library Points', downloaded 2026-10-31",
          "Virginia public library outlet",
          (("OBJECTID", "the layer's row ID"), ("VAPID", "VGIN's place ID"), ("LandmkName", "the library's name"), ("City", "city"),
           ("State", "state"), ("Zip", "ZIP code"), ("URL", "its website"), ("LastCheck", "when VGIN last checked it (epoch ms)"),
           ("PlaceType", "Public Library Points"), ("FIPScode", "county or city FIPS code"), ("FIPSname", "county or city name"))),
)

SPECS = {
    layer.table: catalogue.register(catalogue.Table(
        dataset="raw", name=layer.table, grain=layer.grain, description=f"{layer.publisher}: {layer.edition}, the fields listed, as published",
        columns=tuple(catalogue.Column(f, m, "copy", f"{layer.file}:{f}") for f, m in layer.fields) + PROVENANCE,
        inputs=(layer.file,),
    ))
    for layer in LAYERS
}


def build(run_id: str) -> dict[str, int]:
    con = duckdb.connect()
    counts = {}
    for layer in LAYERS:
        path = paths.DOWNLOADS / layer.file
        features = json.loads(path.read_text(encoding="utf-8"))["features"]
        names = [f for f, _ in layer.fields]
        con.execute(f"CREATE OR REPLACE TEMP TABLE layer ({', '.join(f'{n} VARCHAR' for n in names)}, _row BIGINT)")
        con.executemany(f"INSERT INTO layer VALUES ({', '.join('?' for _ in names)}, ?)",
                        [[None if feature["attributes"].get(n) is None else str(feature["attributes"][n]) for n in names] + [i]
                         for i, feature in enumerate(features, start=1)])
        file_hash = sha256(path)
        select = f"""SELECT {', '.join(names)}, '{layer.file}' AS _file, '' AS _sheet, _row, '{file_hash}' AS _file_sha256,
                            '{(layer.publisher + ": " + layer.edition).replace("'", "''")}' AS _edition FROM layer ORDER BY _row"""
        counts[SPECS[layer.table].key] = tables.write(con, SPECS[layer.table], select, run_id, {layer.file: file_hash})
    return counts
