"""SAM rows keep no people data; website claims are copied as published; matches are one-to-one when accepted."""

import duckdb
import pytest

from ddwb import paths
from ddwb.raw import sam
from ddwb.standard import site_claims


@pytest.fixture(scope="module")
def con():
    if not paths.WAREHOUSE.exists():
        pytest.skip("build first: uv run python -m ddwb.build")
    return duckdb.connect(str(paths.WAREHOUSE), read_only=True)


def test_sam_keeps_only_government_and_tax_exempt_rows_in_the_us(con):
    assert con.execute(f"""select count(*) from raw.{sam.SPEC.name} where "ENTITY STRUCTURE" not in ('2A', '8H')
                           or "PHYSICAL ADDRESS COUNTRY CODE" <> 'USA'""").fetchone()[0] == 0


def test_sam_keeps_no_points_of_contact(con):
    columns = [r[0] for r in con.execute(f"describe raw.{sam.SPEC.name}").fetchall()]
    assert not [c for c in columns if "POC" in c or "POINT OF CONTACT" in c or "MAILING" in c]


def test_sam_rows_are_unique_registrations(con):
    n, ids, rows = con.execute(f"""select count(*), count(distinct ("UNIQUE ENTITY ID", coalesce("ENTITY EFT INDICATOR", ''))),
                                   count(distinct _row) from raw.{sam.SPEC.name}""").fetchone()
    assert n == ids == rows


def test_claims_have_a_website_and_a_unique_id(con):
    n, ids, sites = con.execute("select count(*), count(distinct claim_id), count(website) from standard.site_claims").fetchone()
    assert n == ids == sites


def test_no_federal_dotgov_claims(con):
    assert con.execute(f"""select count(*) from standard.site_claims
                           where list = '{site_claims.DOTGOV}' and kind_codes like 'Federal%'""").fetchone()[0] == 0


def test_texas_gives_one_claim_per_district_from_its_latest_report(con):
    assert con.execute(f"""select count(*) - count(distinct source_id) from standard.site_claims
                           where list = '{site_claims.TEXAS}'""").fetchone()[0] == 0
    assert con.execute(f"""select count(*) from standard.site_claims c join (select spd_publ_id, max(rpt_yr) y
                           from raw.{site_claims.TEXAS} group by 1) t on t.spd_publ_id = c.source_id
                           where c.list = '{site_claims.TEXAS}' and c.report_year <> t.y""").fetchone()[0] == 0


def test_accepted_matches_are_one_to_one(con):
    assert con.execute("""select count(*) from labels.website_matches where status = 'accepted'
                          and (records_with_key <> 1 or hosts_for_record <> 1)""").fetchone()[0] == 0


def test_name_matches_stay_in_the_state_and_skip_department_sites(con):
    assert con.execute("""select count(*) from labels.website_matches m join standard.records r using (record_id)
                          join standard.site_claims c using (claim_id)
                          where m.method = 'name_place' and (r.state is distinct from c.state or c.sub_name is not null)""").fetchone()[0] == 0


def test_uei_matches_are_exact(con):
    assert con.execute("""select count(*) from labels.website_matches m join standard.records r using (record_id)
                          join standard.site_claims c using (claim_id)
                          where m.method = 'uei_exact' and r.ueid is distinct from c.uei""").fetchone()[0] == 0


def test_illinois_keeps_no_people_columns(con):
    columns = {r[0] for r in con.execute("describe raw.il_comptroller_units").fetchall()}
    assert not columns & {"FirstName", "LastName", "Title", "Phone", "Email_GOV", "CEOFName", "CEOEmail", "FOIAEmail"}
    assert con.execute("select count(*) from raw.il_comptroller_units where Email_GOV_domain like '%@%'").fetchone()[0] == 0


def test_mail_domains_are_accepted_only_when_they_look_like_the_units(con):
    from ddwb.labels import websites
    rows = con.execute("""select m.host, c.name, c.kind_codes from labels.website_matches m join standard.site_claims c using (claim_id)
                          where m.status = 'accepted' and m.website_basis = 'email_domain'""").fetchall()
    assert rows and all(websites.domain_ok(h, websites.claim_name(websites.sc.ILLINOIS, n, k)) for h, n, k in rows)


def test_exact_id_methods_match_exact_ids(con):
    assert con.execute("""select count(*) from labels.website_matches m join standard.records r using (record_id)
                          join standard.site_claims c using (claim_id)
                          where (m.method = 'fscskey_exact' and r.agency_id <> coalesce(c.fscskey, c.source_id))
                             or (m.method = 'place_fips_exact' and r.place_fips <> c.place_fips)""").fetchone()[0] == 0


def test_name_matches_respect_the_county_when_both_give_one(con):
    assert con.execute("""select count(*) from labels.website_matches m join standard.records r using (record_id)
                          join standard.site_claims c using (claim_id) where m.method = 'name_place'
                          and ((c.county is not null and r.county_name is not null and upper(trim(c.county)) <> upper(trim(r.county_name)))
                            or (c.county_fips5 is not null and r.county_fips5 is not null and c.county_fips5 <> r.county_fips5))""").fetchone()[0] == 0


def test_map_layers_keep_no_people_fields(con):
    for table in ("ca_calfire_local_fire_districts", "wi_dpi_public_libraries", "vt_public_libraries", "wa_statewide_libraries"):
        columns = {r[0] for r in con.execute(f"describe raw.{table}").fetchall()}
        assert not columns & {"FireChief", "DIRECTOR", "DIRECT_EML", "BRANCH_MGR", "BRMNGR_EML", "EMAIL", "LDLI_email", "PHONE"}


def test_fdid_matches_are_exact_and_in_state(con):
    assert con.execute("""select count(*) from labels.website_matches m join standard.records r using (record_id)
                          join standard.site_claims c using (claim_id)
                          where m.method = 'fdid_exact' and (r.agency_id <> c.source_id or r.state <> c.state)""").fetchone()[0] == 0


def test_usac_and_irs_keep_no_people_columns(con):
    usac = {r[0] for r in con.execute("describe raw.usac_erate_libraries").fetchall()}
    assert not usac & {"account_administrator_name", "general_contact_name", "general_contact_email", "email", "phone_number"}
    irs = {r[0] for r in con.execute("describe raw.irs_990n_filings").fetchall()}
    assert not [c for c in irs if "Officer" in c]


def test_accepted_websites_look_like_web_addresses(con):
    from ddwb.labels import websites
    rows = [r[0] for r in con.execute("select website from labels.website_matches where status = 'accepted'").fetchall()]
    assert rows and all(websites.website_ok(w) for w in rows)


def test_wikidata_is_matched_by_exact_id_only(con):
    assert {r[0] for r in con.execute("""select distinct method from labels.website_matches where claim_id like 'wd_%'""").fetchall()} \
        <= {"place_fips_exact", "county_fips_exact", "nces_exact", "fscskey_exact"}
    assert con.execute("""select count(*) from labels.website_matches m join standard.records r using (record_id)
                          join standard.site_claims c using (claim_id)
                          where (m.method = 'county_fips_exact' and r.county_fips5 <> c.county_fips5)
                             or (m.method = 'nces_exact' and r.agency_id <> c.nces_leaid)""").fetchone()[0] == 0


def test_texas_library_directory_keeps_no_people_columns(con):
    columns = {r[0] for r in con.execute("describe raw.tx_tslac_libraries").fetchall()}
    assert not [c for c in columns if "Director" in c or "Librarian" in c or "Email" in c]


def test_epa_mail_domains_are_domains_only_and_guarded(con):
    assert con.execute("select count(*) from raw.epa_sdwis_email_domains where email_domain like '%@%'").fetchone()[0] == 0
    from ddwb.labels import websites
    rows = con.execute("""select m.host, c.name from labels.website_matches m join standard.site_claims c using (claim_id)
                          where m.method = 'pwsid_exact' and m.status = 'accepted'""").fetchall()
    assert all(websites.domain_ok(h, n) for h, n in rows)
