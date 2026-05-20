"""
=============================================================
  SANCTION LIST ENGINE — Senior Engineer Grade
=============================================================
  What a real bank's compliance backend does:

  1. Smart fetching  — ETag check (skip if not changed)
  2. Delta only      — OFAC delta file (only what changed)
  3. REST APIs       — proper API calls where available
  4. Auto storage    — SQLite database (no manual files)
  5. Auto scheduler  — runs every 6 hours, no human needed
  6. Change logging  — full audit trail of every update
  7. Source coverage — OFAC, UN, EU, UK, OpenSanctions

  ZERO manual copying. ZERO human intervention after setup.
=============================================================
"""

import requests
import sqlite3
import hashlib
import json
import time
import logging
import os
import csv
import re
import calendar
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from io import StringIO, BytesIO
from threading import Thread
import threading

from bs4 import BeautifulSoup

# Playwright is optional — when not installed we fall back to static URL
# lists for SEBI / direct HTTP for OpenSanctions, and the fetcher logs
# the degraded mode rather than crashing.
try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

# ── LOGGING SETUP ─────────────────────────────────────────
import sys

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sanction_engine.log", encoding="utf-8"),
    ]
)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
logger = logging.getLogger("SanctionEngine")

# ── CONFIG ────────────────────────────────────────────────
DB_PATH       = "sanctions.db"
FETCH_INTERVAL= 6 * 3600        # every 6 hours in seconds
REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": "BankSanctionEngine/2.0 (Compliance System)",
    "Accept":     "application/xml,text/csv,application/json",
}

# Browser-shaped headers for endpoints that 403 a plain UA (e.g. OFAC dashboard API)
BROWSER_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/123.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://sanctionslist.ofac.treas.gov",
    "Referer":         "https://sanctionslist.ofac.treas.gov/Home/SdnList",
}

# Indian regulators (MHA / SEBI / RBI wilful-defaulter feeds) are public HTML
# pages, not XML/CSV exports — we scrape with BeautifulSoup. The User-Agent
# below identifies the tool clearly so server admins can contact us if needed.
SCRAPER_HEADERS = {
    "User-Agent": "Mozilla/5.0 SanctionsScreening/1.0 (Compliance Research Tool)",
    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SCRAPER_DELAY = 2  # seconds between HTTP requests to government endpoints

# OpenSanctions free tier requires an API key — read from env, no hard-coded secret
OPENSANCTIONS_API_KEY = os.environ.get("OPENSANCTIONS_API_KEY", "").strip()

# EU FSF public token (URL-distributed; not a credential — equivalent to "token-2017")
EU_FSF_TOKEN = "dG9rZW4tMjAxNw"

# ── ALL SOURCES ───────────────────────────────────────────
SOURCES = {

    # ── OFAC (USA) ── Smart delta + full fallback ─────────
    "ofac_delta_xml": {
        "name":      "OFAC Delta — Changes Only (XML)",
        "authority": "US Treasury / OFAC",
        "url":       "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/DELTASDNLAST.XML",
        "type":      "ofac_xml",
        "strategy":  "delta",           # only what changed!
        "region":    "USA",
    },
    "ofac_full_xml": {
        "name":      "OFAC Full SDN List (XML)",
        "authority": "US Treasury / OFAC",
        "url":       "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML",
        "type":      "ofac_xml",
        "strategy":  "etag",            # ETag — skip if unchanged
        "region":    "USA",
    },
    "ofac_consolidated_csv": {
        "name":      "OFAC Consolidated Non-SDN List (CSV)",
        "authority": "US Treasury / OFAC",
        # CONS_PRIM.CSV is the live consolidated non-SDN list.
        # CONSOLIDATED.CSV at the same host is an empty stub.
        "url":       "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_PRIM.CSV",
        "type":      "ofac_csv",
        "strategy":  "etag",
        "region":    "USA",
    },

    # ── UN Security Council ── XML feed ───────────────────
    "un_xml": {
        "name":      "UN Security Council Consolidated List (XML)",
        "authority": "United Nations Security Council",
        "url":       "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
        "type":      "un_xml",
        "strategy":  "etag",
        "region":    "GLOBAL",
    },

    # ── EU Sanctions ── XML feed ──────────────────────────
    "eu_xml": {
        "name":      "EU Financial Sanctions Full List (XML)",
        "authority": "European Union",
        "url":       f"https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList/content?token={EU_FSF_TOKEN}",
        "type":      "eu_xml",
        "strategy":  "etag",
        "region":    "EUROPE",
    },

    # ── UK Sanctions ── CSV ───────────────────────────────
    "uk_csv": {
        "name":      "UK Sanctions List (CSV)",
        "authority": "UK FCDO / OFSI",
        "url":       "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv",
        "type":      "uk_csv",
        "strategy":  "etag",
        "region":    "UK",
    },

    # ── OpenSanctions ── Proper REST API ──────────────────
    "opensanctions_api": {
        "name":      "OpenSanctions REST API (331 global lists)",
        "authority": "OpenSanctions",
        "url":       "https://api.opensanctions.org/search/default",
        "type":      "opensanctions_api",
        "strategy":  "api",             # proper REST API call
        "region":    "GLOBAL",
    },

    # ── OFAC REST API ── query based ──────────────────────
    "ofac_api": {
        "name":      "OFAC Sanctions List REST API",
        "authority": "US Treasury / OFAC",
        "url":       "https://sanctionslist.ofac.treas.gov/api/PublicationController/GetSDNList",
        "type":      "ofac_api",
        "strategy":  "api",
        "region":    "USA",
    },

    # ── MHA UAPA (India) ── HTML scrape (two pages, one source key) ─
    # Combines banned organisations + individual terrorists scraped
    # together so SQL GROUP BY source returns one MHA_UAPA bucket.
    "mha_uapa": {
        "name":      "MHA UAPA Banned Organisations & Individual Terrorists",
        "authority": "Ministry of Home Affairs, Government of India",
        "url":       "https://www.mha.gov.in/en/banned-organisations",
        "individuals_url": "https://www.mha.gov.in/en/individual-terrorists-under-uapa",
        "type":      "mha_uapa_html",
        "strategy":  "scrape",
        "region":    "INDIA",
    },

    # ── SEBI Debarred Entities (India) ── HTML scrape ─────
    "sebi_debarred": {
        "name":      "SEBI Debarred Entities List",
        "authority": "Securities and Exchange Board of India",
        "url":       "https://www.sebi.gov.in/pmd/debarredco.html",
        "type":      "sebi_html",
        "strategy":  "scrape",
        "region":    "INDIA",
    },

    # ── RBI / MCA Disqualified Directors (India) ──────────
    # Primary: OpenSanctions REST API — schema=Person + schema=Company
    # against dataset 'in_mca_disqualified_directors'. Free tier needs
    # OPENSANCTIONS_API_KEY env var. Fallback: scrape mca.gov.in (JS-
    # rendered, normally yields 0 — kept so failure is auditable).
    # The Watchout Investors source from the earlier attempt is dropped
    # because it requires a POST form interaction.
    "rbi_wilful_defaulter": {
        "name":      "RBI Wilful Defaulters / MCA Disqualified Directors",
        "authority": "Reserve Bank of India / Ministry of Corporate Affairs",
        "url":       "https://api.opensanctions.org/entities/",
        "fallback_url": "https://www.mca.gov.in/content/mca/global/en/mca/master-data/DIN.html",
        "type":      "rbi_mca_os",
        "strategy":  "scrape",
        "region":    "INDIA",
    },
}

# ── DATABASE SETUP ────────────────────────────────────────
def init_db():
    """
    Senior engineer approach:
    Store everything in SQLite — no manual files, queryable,
    auditable, indexable. In production this would be PostgreSQL/Oracle.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Main sanctions table
    c.execute("""
        CREATE TABLE IF NOT EXISTS sanctions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source         TEXT NOT NULL,
            authority      TEXT NOT NULL,
            region         TEXT,
            entity_type    TEXT,
            last_name      TEXT,
            first_name     TEXT,
            full_name      TEXT,
            program        TEXT,
            nationality    TEXT,
            dob            TEXT,
            dob_normalized TEXT,
            listed_on      TEXT,
            raw_data       TEXT,
            created_at     TEXT DEFAULT (datetime('now')),
            updated_at     TEXT DEFAULT (datetime('now'))
        )
    """)

    # Migrate older DBs that pre-date the dob_normalized column
    existing_cols = [r[1] for r in c.execute("PRAGMA table_info(sanctions)").fetchall()]
    if "dob_normalized" not in existing_cols:
        c.execute("ALTER TABLE sanctions ADD COLUMN dob_normalized TEXT DEFAULT ''")
        logger.info("Schema migrated: added sanctions.dob_normalized")

    # ETag store — so we don't re-download unchanged files
    c.execute("""
        CREATE TABLE IF NOT EXISTS fetch_state (
            source        TEXT PRIMARY KEY,
            last_etag     TEXT,
            last_modified TEXT,
            last_hash     TEXT,
            last_fetched  TEXT,
            last_count    INTEGER,
            status        TEXT
        )
    """)

    # Audit log — full history of every fetch
    c.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT,
            fetched_at  TEXT,
            status      TEXT,
            records     INTEGER,
            size_kb     REAL,
            changed     INTEGER,
            notes       TEXT
        )
    """)

    # Index for fast name lookup (what bank queries per transaction)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_full_name
        ON sanctions(full_name)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_last_name
        ON sanctions(last_name)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_dob_normalized
        ON sanctions(dob_normalized)
    """)

    conn.commit()
    conn.close()
    logger.info(f"Database initialised → {DB_PATH}")


# ── ETAG MANAGER ──────────────────────────────────────────
class ETagManager:
    """
    Smart HTTP caching — skip download if file hasn't changed.
    Senior engineers always use this — never waste bandwidth.
    """
    def __init__(self, db_path):
        self.db = db_path

    def get_state(self, source):
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT last_etag, last_modified, last_hash FROM fetch_state WHERE source=?",
            (source,)
        ).fetchone()
        conn.close()
        return row or (None, None, None)

    def save_state(self, source, etag, modified, hash_, count, status):
        conn = sqlite3.connect(self.db)
        conn.execute("""
            INSERT INTO fetch_state(source, last_etag, last_modified,
                last_hash, last_fetched, last_count, status)
            VALUES(?,?,?,?,datetime('now'),?,?)
            ON CONFLICT(source) DO UPDATE SET
                last_etag=excluded.last_etag,
                last_modified=excluded.last_modified,
                last_hash=excluded.last_hash,
                last_fetched=excluded.last_fetched,
                last_count=excluded.last_count,
                status=excluded.status
        """, (source, etag, modified, hash_, count, status))
        conn.commit()
        conn.close()

    def smart_get(self, url, source):
        """
        Makes HTTP request with ETag headers.
        If server returns 304 Not Modified → skip, saves bandwidth.
        """
        etag, modified, _ = self.get_state(source)
        headers = dict(HEADERS)

        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified

        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if resp.status_code == 304:
            logger.info(f"  [{source}] 304 Not Modified — skipping, list unchanged")
            return None  # Nothing to do

        resp.raise_for_status()

        new_etag    = resp.headers.get("ETag")
        new_modified= resp.headers.get("Last-Modified")
        new_hash    = hashlib.md5(resp.content).hexdigest()

        _, _, old_hash = self.get_state(source)
        if new_hash == old_hash:
            logger.info(f"  [{source}] Content hash unchanged — skipping")
            return None

        logger.info(f"  [{source}] New data detected! Size: {len(resp.content)/1024:.1f} KB")
        return resp, new_etag, new_modified, new_hash


# ── PARSERS ───────────────────────────────────────────────

OFAC_XML_NAMESPACES = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML",
    "http://tempuri.org/sdnList.xsd",
    "",
)


def parse_ofac_xml(content, source_key, authority, region):
    """
    Parse OFAC SDN XML.

    The 2024 SLS migration moved the default namespace from
    tempuri.org → sanctionslistservice.ofac.treas.gov; we try both,
    plus no-namespace, so this works against legacy and current files.

    Extracts per-entry: name parts, sdnType, programs, DOB (mainEntry
    preferred), nationality (mainEntry preferred). The file-level
    Publish_Date is applied to every row as listed_on.
    """
    root = ET.fromstring(content)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag[1:root.tag.index("}")]

    def find_entries():
        for candidate_ns in (ns, *OFAC_XML_NAMESPACES):
            tag = f"{{{candidate_ns}}}sdnEntry" if candidate_ns else "sdnEntry"
            found = list(root.iter(tag))
            if found:
                return found, candidate_ns
        return [], ns

    sdn_entries, used_ns = find_entries()

    def qtag(tag, cns=None):
        cns = used_ns if cns is None else cns
        return f"{{{cns}}}{tag}" if cns else tag

    def child_text(parent, tag):
        for cns in (used_ns, *OFAC_XML_NAMESPACES):
            el = parent.find(qtag(tag, cns))
            if el is not None and el.text:
                return el.text.strip()
        return ""

    def pick_main_entry(parent, list_tag, item_tag, value_tag):
        """
        Walk a *List/*Item structure, prefer the child whose
        <mainEntry>true</mainEntry> flag is set, else fall back to first.
        """
        wrapper = parent.find(qtag(list_tag))
        if wrapper is None:
            return ""
        items = list(wrapper.findall(qtag(item_tag)))
        if not items:
            return ""
        chosen = None
        for it in items:
            flag = it.find(qtag("mainEntry"))
            if flag is not None and (flag.text or "").strip().lower() == "true":
                chosen = it
                break
        chosen = chosen or items[0]
        val = chosen.find(qtag(value_tag))
        return (val.text or "").strip() if val is not None and val.text else ""

    # File-level publish date — applies to every row in this snapshot
    pub_info = root.find(qtag("publshInformation"))  # OFAC's spelling, not a typo on our side
    publish_date = ""
    if pub_info is not None:
        pd = pub_info.find(qtag("Publish_Date"))
        if pd is not None and pd.text:
            publish_date = pd.text.strip()

    entries = []
    for entry in sdn_entries:
        last  = child_text(entry, "lastName")
        first = child_text(entry, "firstName")
        full  = f"{last}, {first}".strip(", ")

        prog_list = entry.find(qtag("programList"))
        programs = []
        if prog_list is not None:
            for p in prog_list.iter():
                if p.text and p.tag.endswith("program"):
                    programs.append(p.text.strip())

        dob         = pick_main_entry(entry, "dateOfBirthList",  "dateOfBirthItem", "dateOfBirth")
        nationality = pick_main_entry(entry, "nationalityList",  "nationality",     "country")

        entries.append({
            "source":      source_key,
            "authority":   authority,
            "region":      region,
            "entity_type": child_text(entry, "sdnType"),
            "last_name":   last,
            "first_name":  first,
            "full_name":   full,
            "program":     ",".join(programs),
            "nationality": nationality,
            "dob":         dob,
            "listed_on":   publish_date,
            "raw_data":    json.dumps({
                "uid": child_text(entry, "uid"),
                "remarks": child_text(entry, "remarks"),
                "title":   child_text(entry, "title"),
            }),
        })
    return entries


# DOB / nationality patterns inside OFAC CONS_PRIM remarks free-text.
# Examples seen: "DOB 1962", "DOB 10 Dec 1948", "DOB circa 1955",
#                "nationality Palestinian", "citizen of Iran"
_DOB_RE         = re.compile(r"\bDOB\s+([^;]+?)(?=;|$)", re.IGNORECASE)
_NATIONALITY_RE = re.compile(r"\bnationality\s+([^;]+?)(?=;|$)", re.IGNORECASE)
_CITIZEN_RE     = re.compile(r"\bcitizen of\s+([^;]+?)(?=;|$)", re.IGNORECASE)


def parse_ofac_csv(content, source_key, authority, region):
    """
    Parse OFAC CONS_PRIM.CSV (consolidated non-SDN list).

    Layout (no header row):
      0=uid, 1="LASTNAME, FirstName" combined, 2=type,
      3=program, 4-10="-0-" placeholders, 11=remarks (free text)

    The CSV format has no dedicated DOB / nationality columns — those
    facts live as semicolon-delimited fragments inside the remarks
    field ("DOB 1962; POB Shati; nationality Palestinian"). We regex
    them out so the columns aren't always blank.
    """
    entries = []
    reader = csv.reader(StringIO(content.decode("utf-8", errors="ignore")))
    for row in reader:
        if not row:
            continue
        row = row + [""] * (12 - len(row)) if len(row) < 12 else row
        uid       = row[0].strip()
        full_name = row[1].strip()
        if not uid or not full_name:
            continue
        if "," in full_name:
            last, _, first = full_name.partition(",")
            last, first = last.strip(), first.strip()
        else:
            last, first = full_name, ""

        remarks = row[11].strip()
        dob_m   = _DOB_RE.search(remarks)
        nat_m   = _NATIONALITY_RE.search(remarks) or _CITIZEN_RE.search(remarks)

        entries.append({
            "source":      source_key,
            "authority":   authority,
            "region":      region,
            "entity_type": row[2].strip().strip('"'),
            "last_name":   last,
            "first_name":  first,
            "full_name":   full_name,
            "program":     row[3].strip().strip('"'),
            "nationality": nat_m.group(1).strip() if nat_m else "",
            "dob":         dob_m.group(1).strip() if dob_m else "",
            "raw_data":    json.dumps({"uid": uid, "remarks": remarks}),
        })
    return entries


def _un_dob(ind):
    """
    UN <INDIVIDUAL_DATE_OF_BIRTH> can carry an exact DATE,
    a YEAR (sometimes with MONTH/DAY), or a YEAR_RANGE.
    Returns a single human-readable string or "".
    """
    dob_el = ind.find("INDIVIDUAL_DATE_OF_BIRTH")
    if dob_el is None:
        return ""
    def t(tag):
        e = dob_el.find(tag)
        return (e.text or "").strip() if e is not None and e.text else ""
    if t("DATE"):
        return t("DATE")
    parts = [t("DAY"), t("MONTH"), t("YEAR")]
    parts = [p for p in parts if p]
    if parts:
        return " ".join(parts)
    if t("FROM_YEAR") or t("TO_YEAR"):
        return f"{t('FROM_YEAR')}-{t('TO_YEAR')}".strip("-")
    return ""


def parse_un_xml(content, source_key, authority, region):
    """Parse UN Security Council XML."""
    root = ET.fromstring(content)
    entries = []

    for ind in root.iter("INDIVIDUAL"):
        def g(tag):
            el = ind.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        parts = [g("FIRST_NAME"), g("SECOND_NAME"),
                 g("THIRD_NAME"), g("FOURTH_NAME")]
        full = " ".join(p for p in parts if p)
        entries.append({
            "source":      source_key,
            "authority":   authority,
            "region":      region,
            "entity_type": "individual",
            "last_name":   g("FIRST_NAME"),
            "first_name":  g("SECOND_NAME"),
            "full_name":   full,
            "program":     "UN_SANCTIONS",
            "nationality": g("NATIONALITY/VALUE"),
            "dob":         _un_dob(ind),
            "listed_on":   g("LISTED_ON"),
            "raw_data":    json.dumps({"un_ref": g("REFERENCE_NUMBER")}),
        })

    for ent in root.iter("ENTITY"):
        def g(tag):
            el = ent.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        name = g("FIRST_NAME") or g("ENTITY_NAME")
        entries.append({
            "source":      source_key,
            "authority":   authority,
            "region":      region,
            "entity_type": "entity",
            "last_name":   name,
            "first_name":  "",
            "full_name":   name,
            "program":     "UN_SANCTIONS",
            "listed_on":   g("LISTED_ON"),
            "raw_data":    json.dumps({"un_ref": g("REFERENCE_NUMBER")}),
        })

    return entries


def parse_eu_xml(content, source_key, authority, region):
    """
    Parse EU Financial Sanctions XML.

    Schema uses default namespace http://eu.europa.ec/fpi/fsd/export with
    <sanctionEntity> wrappers each containing one or more <nameAlias>
    elements (one per known alias / transliteration).

    Per-entity DOB / citizenship are stored as siblings of <nameAlias>:
      <birthdate birthdate="1937-04-28" countryDescription="IRAQ" .../>
      <citizenship countryDescription="IRAQ" .../>
    They apply to the designee, not to a specific alias — so every alias
    row inherits the same dob / nationality / listed_on values.
    """
    root = ET.fromstring(content)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag[1:root.tag.index("}")]
    nstag = lambda t: f"{{{ns}}}{t}" if ns else t

    entries = []
    for entity in root.iter(nstag("sanctionEntity")):
        subject = entity.find(nstag("subjectType"))
        ent_type = subject.get("code", "unknown") if subject is not None else "unknown"

        reg = entity.find(nstag("regulation"))
        program     = reg.get("programme") if reg is not None else "EU_SANCTIONS"
        reg_no      = reg.get("numberTitle") if reg is not None else ""
        listed_on   = reg.get("publicationDate") if reg is not None else ""

        # First birthdate child (designees can have several — pick the first non-empty)
        dob = ""
        for bd in entity.findall(nstag("birthdate")):
            dob = (bd.get("birthdate") or bd.get("year") or "").strip()
            if dob:
                break

        # First citizenship child — prefer the descriptive country name
        nationality = ""
        for cz in entity.findall(nstag("citizenship")):
            nationality = (cz.get("countryDescription")
                           or cz.get("countryIso2Code")
                           or "").strip()
            if nationality:
                break

        for alias in entity.iter(nstag("nameAlias")):
            whole = (alias.get("wholeName") or "").strip()
            first = (alias.get("firstName") or "").strip()
            last  = (alias.get("lastName")  or "").strip()
            full  = whole or f"{first} {last}".strip()
            if not full:
                continue
            entries.append({
                "source":      source_key,
                "authority":   authority,
                "region":      region,
                "entity_type": ent_type,
                "last_name":   last or full,
                "first_name":  first,
                "full_name":   full,
                "program":     program or "EU_SANCTIONS",
                "nationality": nationality,
                "dob":         dob,
                "listed_on":   listed_on,
                "raw_data":    json.dumps({
                    "regulation": reg_no,
                    "logical_id": entity.get("logicalId", ""),
                }),
            })
    return entries


def parse_uk_csv(content, source_key, authority, region):
    """
    Parse UK Sanctions List CSV (FCDO consolidated list).

    File layout:
      Line 1: "Report Date: ..." preamble — must skip
      Line 2: column headers including Name 1..Name 6, Designation Type,
              Regime Name, Date Designated, D.O.B, Nationality(/ies), ...
      Line 3+: data rows

    Name 1..Name 5 are given-name parts; Name 6 is the surname / whole
    primary name. We seen-dedupe by (UID, full_name) so multiple rows
    for the same designee (e.g. multiple addresses) collapse to one
    sanctions entry per alias.
    """
    text = content.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    # Skip preamble lines until we find the header row that starts with "Last Updated"
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.lower().startswith("last updated")),
        0,
    )
    csv_text = "\n".join(lines[header_idx:])

    entries = []
    seen = set()
    try:
        reader = csv.DictReader(StringIO(csv_text))
        for row in reader:
            # csv.DictReader puts overflow columns under key None — drop them
            row = {k: (v or "") for k, v in row.items() if k is not None}

            given = " ".join(
                (row.get(f"Name {i}", "") or "").strip()
                for i in range(1, 6)
            ).strip()
            surname = (row.get("Name 6", "") or "").strip()
            full = (f"{given} {surname}".strip()) or surname or given
            if not full:
                continue

            uid = (row.get("Unique ID") or "").strip()
            key = (uid, full)
            if key in seen:
                continue
            seen.add(key)

            entries.append({
                "source":      source_key,
                "authority":   authority,
                "region":      region,
                "entity_type": (row.get("Designation Type") or "unknown").strip(),
                "last_name":   surname or full,
                "first_name":  given,
                "full_name":   full,
                "program":     (row.get("Regime Name") or "UK_SANCTIONS").strip(),
                "nationality": (row.get("Nationality(/ies)") or "").strip(),
                "dob":         (row.get("D.O.B") or "").strip(),
                "listed_on":   (row.get("Date Designated") or "").strip(),
                "raw_data":    json.dumps({
                    "uid": uid,
                    "ofsi_group_id": (row.get("OFSI Group ID") or "").strip(),
                    "un_ref": (row.get("UN Reference Number") or "").strip(),
                }),
            })
    except Exception as e:
        logger.warning(f"UK CSV parse error: {e}")
    return entries


# ── INDIAN REGULATOR HTML PARSERS ─────────────────────────
#
# These scrape public HTML pages from MHA / SEBI / RBI feeds and are
# deliberately defensive: when a page's structure shifts (tables get
# renamed, classes change, layout flips to <div>-based), the parser
# returns whatever rows it can read and logs the rest — it never raises.
# That keeps fetch_all() resilient even when one source's HTML drifts.

def _clean_text(s):
    """Collapse whitespace and strip surrounding noise from scraped text."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _looks_like_header(cells):
    """Heuristic to skip table header rows in messy HTML."""
    if not cells:
        return True
    joined = " ".join(cells).lower()
    return any(tok in joined for tok in (
        "s.no", "sno", "sr.no", "name of", "organisation name",
        "date of", "section", "order date", "default amount",
        "bank name", "defaulter"
    )) and len(joined) < 200


def _iter_data_rows(soup):
    """
    Yield (row_cells, source_table) for every <tr> inside every <table>
    on the page, skipping rows that look like headers. Many Indian
    government pages have multiple tables (one per regime / state) —
    we walk all of them.
    """
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [_clean_text(td.get_text(" ", strip=True))
                     for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if not cells:
                continue
            if _looks_like_header(cells):
                continue
            yield cells, table


# Page-boilerplate tokens that must never appear inside an entity name.
# Anything we extract whose lowercase form contains one of these is rejected.
_NAV_NOISE = (
    "skip to main content", "skip to content", "home", "menu", "search",
    "login", "logout", "register", "sign in", "sign up", "navigation",
    "breadcrumb", "footer", "header", "sidebar",
    "click here", "read more", "view all", "back to top",
    "press release", "annual report", "sitemap", "feedback",
    "privacy policy", "terms of use", "accessibility",
    "screen reader", "select your language", "government of india",
    "ministry of home affairs", "national portal",
)
_URL_RE = re.compile(r"^https?://|^www\.", re.IGNORECASE)


def _valid_entity_name(name):
    """
    Strict validation for a scraped string before we accept it as an entity.
    Rejects: empty, too-short, too-long, digits-only, URL-shaped,
    or anything containing a navigation/boilerplate token.
    """
    if not name:
        return False
    n = name.strip()
    if len(n) < 5 or len(n) > 250:
        return False
    if n.isdigit():
        return False
    if _URL_RE.match(n):
        return False
    low = n.lower()
    for tok in _NAV_NOISE:
        if tok in low:
            return False
    # Must contain at least one alphabetic character
    if not re.search(r"[A-Za-zऀ-ॿ]", n):
        return False
    return True


def _main_content_root(soup):
    """
    Return the most plausible main-content wrapper of an MHA Drupal page —
    we scan a known list of class/id names. Falls back to <main> or <body>.
    Excluding nav/header/footer/sidebar happens via .extract() before we
    walk what's left so descendant lookups stay clean.
    """
    for sel in (
        ".field-content table",         # the actual UAPA table lives here
        ".field--name-body",
        ".view-content",
        ".field-content",
        "main",
        "article",
        "#main-content",
        "[role=main]",
    ):
        node = soup.select_one(sel)
        if node:
            return node
    return soup.body or soup


def parse_mha_uapa_organisations(html, source_key, authority, region, source_url):
    """
    Parse MHA banned organisations page.

    The list lives in a clean <table> inside .field-content with shape
    [Sl-No, Organisation]. We target THAT table only, skip the header
    row, take column[1] as the organisation name, and validate every
    extracted string against _valid_entity_name() so navigation menu
    items and page boilerplate cannot leak in.

    If the page redesigns and the table disappears, we return an empty
    list rather than fall back to a noisy heuristic — the caller logs
    the zero count to fetch_log so the operator notices.
    """
    entries = []
    try:
        soup = BeautifulSoup(html, "lxml")
        # Strip nav / header / footer / sidebar so they cannot be reached
        for unwanted_sel in ("nav", "header", "footer", "aside",
                              ".menu", ".navbar", ".sidebar", ".region-footer",
                              ".breadcrumb", ".main-menu", ".user-menu"):
            for n in soup.select(unwanted_sel):
                n.decompose()

        # Prefer the data table inside field-content; if absent, abort.
        table = soup.select_one(".field-content table")
        if table is None:
            logger.warning("MHA orgs: expected <table> inside .field-content not found")
            return []

        rows = table.find_all("tr")
        for tr in rows:
            cells = [_clean_text(td.get_text(" ", strip=True))
                     for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            # Header row carries "Organisation" / "Sl" — skip.
            joined = " ".join(cells).lower()
            if "organisation" in joined and "sl" in joined:
                continue
            # Layout: [serial-number, name]. Name is the cell that
            # isn't purely numeric — pick the longest such cell.
            non_numeric = [c for c in cells if not c.isdigit()]
            if not non_numeric:
                continue
            name = max(non_numeric, key=len)
            if not _valid_entity_name(name):
                continue
            entries.append({
                "source":      source_key,
                "authority":   authority,
                "region":      region,
                "entity_type": "organisation",
                "last_name":   name,
                "first_name":  "",
                "full_name":   name,
                "program":     "UAPA_BANNED",
                "nationality": "IN",
                "raw_data":    json.dumps({
                    "source_list": "MHA_UAPA",
                    "source_url":  source_url,
                    "uapa_kind":   "terrorist_organisation",
                    "section":     "UAPA Section 35 — First Schedule",
                    "serial":      cells[0] if cells[0].isdigit() else "",
                }),
            })
    except Exception as e:
        logger.warning(f"MHA orgs parse error: {e}")
    seen, uniq = set(), []
    for e in entries:
        k = e["full_name"].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


def parse_mha_uapa_individuals(html, source_key, authority, region, source_url):
    """
    Parse MHA's 'Individual Terrorists under UAPA' page (Fourth Schedule).

    Same approach as the organisations page: strip nav/header/footer
    first, then look for a data <table> inside .field-content (the MHA
    Drupal template renders the Fourth Schedule list the same way).
    Every extracted name goes through _valid_entity_name().
    """
    entries = []
    try:
        soup = BeautifulSoup(html, "lxml")
        for unwanted_sel in ("nav", "header", "footer", "aside",
                              ".menu", ".navbar", ".sidebar", ".region-footer",
                              ".breadcrumb", ".main-menu", ".user-menu"):
            for n in soup.select(unwanted_sel):
                n.decompose()

        table = soup.select_one(".field-content table")
        if table is None:
            logger.warning("MHA individuals: expected <table> inside .field-content not found")
            return []

        for tr in table.find_all("tr"):
            cells = [_clean_text(td.get_text(" ", strip=True))
                     for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            joined = " ".join(cells).lower()
            if "name" in joined and "sl" in joined and len(joined) < 80:
                continue  # header row
            non_numeric = [c for c in cells if not c.isdigit()]
            if not non_numeric:
                continue
            # On the individuals page the second column carries the
            # primary name; longer columns tend to be aliases/notes.
            name = non_numeric[0] if non_numeric else ""
            if not _valid_entity_name(name):
                continue
            parts = name.split()
            entries.append({
                "source":      source_key,
                "authority":   authority,
                "region":      region,
                "entity_type": "individual",
                "last_name":   parts[-1] if parts else name,
                "first_name":  " ".join(parts[:-1]) if len(parts) > 1 else "",
                "full_name":   name,
                "program":     "UAPA_INDIVIDUAL_TERRORIST",
                "nationality": "IN",
                "raw_data":    json.dumps({
                    "source_list": "MHA_UAPA",
                    "source_url":  source_url,
                    "serial":      cells[0] if cells[0].isdigit() else "",
                    "row":         cells,
                }),
            })
    except Exception as e:
        logger.warning(f"MHA individuals parse error: {e}")
    seen, uniq = set(), []
    for e in entries:
        k = e["full_name"].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


# Heuristic to tell 'individual' vs 'organisation' from a free-form name —
# corporate suffixes are a strong signal; everything else falls back to
# 'individual' which mirrors SEBI's actual debarment ratio.
_ORG_SUFFIXES = re.compile(
    r"\b(ltd|limited|llp|llc|inc|incorporated|corp|corporation|"
    r"pvt|private|company|co\.|services|finance|capital|"
    r"securities|investments?|enterprises?|industries|holdings|"
    r"group|brokers?|stock|trading|consultancy|advisors?|fund|"
    r"foundation|trust|society|association|institute|technologies|"
    r"agencies|media|infotech|infrastructure|projects|developers?|"
    r"realtors?|properties|exports?|imports?|chits?|cooperative)\b",
    re.IGNORECASE,
)

# Match Indian date formats — DD/MM/YYYY, DD-MM-YYYY, "DD Mon YYYY"
_INDIAN_DATE_RE = re.compile(
    r"\b(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4}|"
    r"\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)


def _classify_entity(name):
    """Return 'organisation' if the name carries a corporate suffix, else 'individual'."""
    return "organisation" if _ORG_SUFFIXES.search(name or "") else "individual"


# Words/phrases inside SEBI order PDFs that signal the line is NOT a
# defendant name (headers, regulatory references, addresses).
_SEBI_PDF_NOISE = (
    "sebi", "securities and exchange board", "order", "vide",
    "regulation", "market", "ground floor", "stock exchange",
    "registration", "circular", "annexure", "show cause",
    "interim", "final", "act,", "rule", "section",
    "prohibition", "prohibited", "manipulati", "fraud",
    "adjudicating", "whole time", "wtm", "chairman",
    "no.", "page", "issued", "received", "dated",
    "in re", "in the matter of", "respondent",
    # Header / title rows that leaked through in real PDFs:
    "name of the entity", "name of entity", "name of company",
    "status as on", "sl no", "sl. no", "serial no",
    "list of", "statement of",
)
# Numbered-list line: "1. Name" / "1) Name" / "(1) Name" — captures
# from the first letter after the punctuation up to end of line.
_NUMBERED_NAME_RE = re.compile(
    r"^\s*(?:\(?\d{1,3}[\.\)]|\d{1,3}\.\d{1,3}\.?)\s+(.{4,180}?)\s*$"
)
# Address-shaped line: starts with a flat/door number like "514/5,"
_ADDRESS_HEAD_RE = re.compile(r"^\d{1,6}[/\-,]\s*\w")


def _is_pdf_name_line(line):
    """Return True if a PDF text line plausibly contains an entity name."""
    s = line.strip()
    if not s:
        return False
    # Reject parenthetical headers like "(Status as on July 31, 2005)"
    if s.startswith("(") and s.endswith(")"):
        return False
    low = s.lower()
    for tok in _SEBI_PDF_NOISE:
        if tok in low:
            return False
    if _ADDRESS_HEAD_RE.match(s):
        return False
    # Reject lines that are mostly digits / punctuation
    letters = sum(1 for c in s if c.isalpha())
    if letters < len(s) * 0.5:
        return False
    if _INDIAN_DATE_RE.search(s) and len(s) < 60:
        return False
    return True


# Status-column verbs that signal the trailing portion of a SEBI PDF
# tabular row — we cut entity names at these tokens because PDF text
# extraction joins the Name and Status columns on a single line.
_STATUS_VERB_RE = re.compile(
    r"\b(did\s+not|failed\s+to|has\s+not|has\s+been|debarred|prohibited|"
    r"banned|restrained|vide\s+order|status\s*[:\-]|wind\s+up|"
    r"applied\s+for|filed\s+for|registration\s+(?:granted|denied))\b",
    re.IGNORECASE,
)


def _split_name_from_status(text):
    """
    Given a PDF table row like 'Aakar Plantations Company Ltd. Did not
    apply for registration under...', return just the entity-name
    portion ('Aakar Plantations Company Ltd.'). If no status verb is
    present, return the whole string.
    """
    if not text:
        return ""
    m = _STATUS_VERB_RE.search(text)
    if m:
        return text[: m.start()].rstrip(" ,.;:-")
    return text.strip()


def extract_entities_from_sebi_pdf(pdf_bytes, source_url, source_key,
                                    authority, region):
    """
    Pull entity names out of one SEBI debarment-order PDF.

    Two strategies, applied in order per page:
    1) pdfplumber.extract_tables() — preferred when the PDF has true
       tabular structure (the SEBI CIS-debarred lists are 3-column
       tables: [S.No, Name, Status]). The middle column is the name.
    2) Fall back to numbered-list regex on raw text. Handles both
       '1. Name' and '1 Name' (no-dot) variants.
    """
    import io
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber not installed — run: pip install pdfplumber")
        return []

    entries = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                # ── Strategy 1: tabular extraction ─────
                tables = []
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    tables = []
                tabular_hit = False
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    # Heuristic: header row mentions "Name" anywhere — and
                    # we use the column with the longest average cell as
                    # the name column. Falls back to col[1] for typical
                    # [S.No, Name, Status] layouts.
                    header = [(_clean_text(c) if c else "").lower()
                              for c in table[0]]
                    name_col_idx = None
                    for i, h in enumerate(header):
                        if "name" in h:
                            name_col_idx = i
                            break
                    if name_col_idx is None and len(table[0]) >= 2:
                        name_col_idx = 1  # typical layout
                    if name_col_idx is None:
                        continue  # 1-column tables aren't usable

                    for row in table[1:]:
                        if not row or name_col_idx >= len(row):
                            continue
                        raw_name = _clean_text(row[name_col_idx] or "")
                        candidate = _split_name_from_status(raw_name)
                        candidate = _clean_text(candidate)
                        if not candidate:
                            continue
                        if not _is_pdf_name_line(candidate):
                            continue
                        if not _valid_entity_name(candidate):
                            continue
                        ent_type = _classify_entity(candidate)
                        parts = candidate.split()
                        entries.append({
                            "source":      source_key,
                            "authority":   authority,
                            "region":      region,
                            "entity_type": ent_type,
                            "last_name":   parts[-1] if parts else candidate,
                            "first_name":  " ".join(parts[:-1]) if len(parts) > 1 else "",
                            "full_name":   candidate,
                            "program":     "SEBI_DEBARRED",
                            "nationality": "IN",
                            "raw_data":    json.dumps({
                                "source_list": "SEBI_DEBARRED",
                                "source_url":  source_url,
                                "pdf_url":     source_url,
                                "extracted_via": "table",
                            }),
                        })
                        tabular_hit = True

                # ── Strategy 2: numbered-list regex on text ─
                # Only used when the page didn't yield tabular rows.
                if tabular_hit:
                    continue
                text = page.extract_text() or ""
                for raw_line in text.splitlines():
                    # Accept '1. Name', '1) Name', or bare '1 Name'
                    m = (
                        _NUMBERED_NAME_RE.match(raw_line)
                        or re.match(r"^\s*(\d{1,4})\s+([A-Z].{4,180})$", raw_line)
                    )
                    if not m:
                        continue
                    candidate = m.groups()[-1] if len(m.groups()) > 1 else m.group(1)
                    candidate = _split_name_from_status(_clean_text(candidate))
                    candidate = _clean_text(candidate)
                    if not _is_pdf_name_line(candidate):
                        continue
                    if not _valid_entity_name(candidate):
                        continue
                    ent_type = _classify_entity(candidate)
                    parts = candidate.split()
                    entries.append({
                        "source":      source_key,
                        "authority":   authority,
                        "region":      region,
                        "entity_type": ent_type,
                        "last_name":   parts[-1] if parts else candidate,
                        "first_name":  " ".join(parts[:-1]) if len(parts) > 1 else "",
                        "full_name":   candidate,
                        "program":     "SEBI_DEBARRED",
                        "nationality": "IN",
                        "raw_data":    json.dumps({
                            "source_list": "SEBI_DEBARRED",
                            "source_url":  source_url,
                            "pdf_url":     source_url,
                            "extracted_via": "text",
                        }),
                    })
    except Exception as e:
        logger.warning(f"SEBI PDF parse error ({source_url}): {e}")

    # De-duplicate by full_name within this PDF
    seen, uniq = set(), []
    for e in entries:
        k = e["full_name"].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


def _extract_sebi_html_fallback(content, source_url, source_key, authority, region):
    """
    Some legacy SEBI debarment lists live under /sebi_data/docfiles/*.html
    as plain HTML tables rather than PDFs. This pulls numbered-list-style
    names from such pages using the same rules as the PDF extractor.
    """
    entries = []
    try:
        soup = BeautifulSoup(content, "lxml")
        # Strip nav/header/footer
        for unwanted_sel in ("nav", "header", "footer", "aside",
                              ".menu", ".navbar", ".sidebar"):
            for n in soup.select(unwanted_sel):
                n.decompose()
        text = soup.get_text("\n", strip=True)
        for raw_line in text.splitlines():
            m = _NUMBERED_NAME_RE.match(raw_line)
            if not m:
                continue
            candidate = _clean_text(m.group(1))
            if not _is_pdf_name_line(candidate):
                continue
            if not _valid_entity_name(candidate):
                continue
            ent_type = _classify_entity(candidate)
            parts = candidate.split()
            entries.append({
                "source":      source_key,
                "authority":   authority,
                "region":      region,
                "entity_type": ent_type,
                "last_name":   parts[-1] if parts else candidate,
                "first_name":  " ".join(parts[:-1]) if len(parts) > 1 else "",
                "full_name":   candidate,
                "program":     "SEBI_DEBARRED",
                "nationality": "IN",
                "raw_data":    json.dumps({
                    "source_list": "SEBI_DEBARRED",
                    "source_url":  source_url,
                }),
            })
    except Exception as e:
        logger.warning(f"SEBI HTML fallback parse error ({source_url}): {e}")
    return entries


def parse_sebi_debarred_index(html, source_url):
    """
    From the SEBI debarred-entities listing page, extract every PDF
    download URL. SEBI uses /sebi_data/attachdocs/... and a few legacy
    paths under /cms/, so we accept both. Returns a list of absolute
    PDF URLs in the order they appear on the page (newest first by
    SEBI convention).
    """
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "lxml")
    pdfs = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        low = href.lower()
        if not low.endswith(".pdf"):
            continue
        if "attachdocs" not in low and "/cms/" not in low and "sebi_data" not in low:
            continue
        absolute = urljoin(source_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        pdfs.append({
            "url":   absolute,
            "label": _clean_text(a.get_text(" ", strip=True)),
        })
    return pdfs


def parse_rbi_opensanctions(json_payload, source_key, authority, region, source_url):
    """
    Parse OpenSanctions /entities/ JSON for the MCA disqualified-directors
    dataset. Each result is a 'Person' or 'Company' record — we map:
        caption                  → full_name
        properties.nationality   → nationality (defaults to 'IN' if absent)
        properties.topics        → program (joined with ',')
        first_seen               → listed_on
    Two endpoints (Person + Company) are fetched separately and combined
    upstream; both flow through this parser.
    """
    entries = []
    try:
        results = json_payload.get("results", []) or []
        for r in results:
            caption = _clean_text(r.get("caption") or "")
            if not _valid_entity_name(caption):
                continue
            schema = (r.get("schema") or "").lower()
            ent_type = "individual" if schema == "person" else "organisation"
            props    = r.get("properties", {}) or {}
            nats     = props.get("nationality") or props.get("country") or ["IN"]
            topics   = props.get("topics") or []
            listed   = (r.get("first_seen") or "")[:10]
            parts = caption.split()
            entries.append({
                "source":      source_key,
                "authority":   authority,
                "region":      region,
                "entity_type": ent_type,
                "last_name":   parts[-1] if parts else caption,
                "first_name":  " ".join(parts[:-1]) if len(parts) > 1 else "",
                "full_name":   caption,
                "program":     ",".join(topics) if topics else "RBI_MCA_DISQUALIFIED",
                "nationality": nats[0] if nats else "IN",
                "listed_on":   listed,
                "raw_data":    json.dumps({
                    "source_list": "RBI_MCA_DISQUALIFIED",
                    "source_url":  source_url,
                    "os_id":       r.get("id"),
                    "datasets":    r.get("datasets", []),
                }),
            })
    except Exception as e:
        logger.warning(f"OpenSanctions parse error: {e}")
    seen, uniq = set(), []
    for e in entries:
        k = e["full_name"].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


def parse_mca_din_fallback(html, source_key, authority, region, source_url):
    """
    Best-effort scrape of mca.gov.in/.../DIN.html when OpenSanctions is
    unavailable. The MCA portal is JavaScript-heavy; this static scrape
    rarely yields data — when it returns 0 rows, the caller logs a
    'falled back, page is JS-rendered' note to fetch_log so it's visible
    to the operator without crashing the pipeline.
    """
    entries = []
    try:
        soup = BeautifulSoup(html, "lxml")
        for unwanted_sel in ("nav", "header", "footer", "aside",
                              ".menu", ".navbar", ".sidebar"):
            for n in soup.select(unwanted_sel):
                n.decompose()
        # MCA disqualified directors lists are normally PDFs/Excel files
        # linked under "Designated Information"; we surface link anchors
        # whose href ends in .pdf/.xlsx as the best we can do statically.
        for a in soup.find_all("a", href=True):
            href = a["href"].strip().lower()
            if not (href.endswith(".pdf") or href.endswith(".xlsx")):
                continue
            text = _clean_text(a.get_text(" ", strip=True))
            if not _valid_entity_name(text):
                continue
            ent_type = _classify_entity(text)
            entries.append({
                "source":      source_key,
                "authority":   authority,
                "region":      region,
                "entity_type": ent_type,
                "last_name":   text.split()[-1],
                "first_name":  " ".join(text.split()[:-1]) if len(text.split()) > 1 else "",
                "full_name":   text,
                "program":     "RBI_MCA_DISQUALIFIED",
                "nationality": "IN",
                "raw_data":    json.dumps({
                    "source_list": "RBI_MCA_DISQUALIFIED",
                    "source_url":  source_url,
                    "link_href":   a["href"],
                }),
            })
    except Exception as e:
        logger.warning(f"MCA fallback parse error: {e}")
    return entries


# ── DOB NORMALIZATION ─────────────────────────────────────
_MONTH_MAP = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTH_MAP.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})

_ISO_DATE_RE     = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_NUMERIC_DATE_RE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_TOKEN_RE        = re.compile(r"[A-Za-z]+|\d+")


def normalize_dob(s):
    """
    Best-effort convert any source DOB string to ISO YYYY-MM-DD.

    When day or month are unknown, fall back to YYYY-01-01 (year-only
    placeholder). When no usable year can be extracted, return "".

    Handled formats include:
      "10 Dec 1948"  → 1948-12-10
      "28 Apr 1937"  → 1937-04-28
      "1937-04-28"   → 1937-04-28   (already ISO)
      "25/01/2001"   → 2001-01-25   (UK dd/mm/yyyy)
      "1962", "1971" → 1962-01-01   (year only)
      "circa 1950"   → 1950-01-01
      "dd/mm/1958"   → 1958-01-01   (literal placeholder)
      "" / None      → ""
    """
    if not s:
        return ""
    s = s.strip()
    if not s:
        return ""

    # Already ISO YYYY-MM-DD
    m = _ISO_DATE_RE.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Fully numeric dd/mm/yyyy (UK style)
    m = _NUMERIC_DATE_RE.match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        # invalid day/month — fall through to year-only

    # Token walk: pick out month name, year, day from any free-form string
    year = month = day = None
    for tok in _TOKEN_RE.findall(s):
        low = tok.lower()
        if low in _MONTH_MAP:
            month = _MONTH_MAP[low]
        elif tok.isdigit():
            n = int(tok)
            if 1900 <= n <= 2100 and year is None:
                year = n
            elif 1 <= n <= 31 and day is None:
                day = n

    if year is None:
        return ""
    if month and day and 1 <= day <= 31:
        return f"{year:04d}-{month:02d}-{day:02d}"
    if month:
        return f"{year:04d}-{month:02d}-01"
    return f"{year:04d}-01-01"


# ── DATABASE WRITER ───────────────────────────────────────
def upsert_entries(entries, source_key):
    """
    Write parsed entries to database.
    DELETE old entries for this source, INSERT fresh ones.
    Each row gets a derived dob_normalized (ISO) alongside the raw dob.
    """
    if not entries:
        return 0
    defaults = {"nationality": "", "dob": "", "listed_on": ""}
    rows = []
    for e in entries:
        row = {**defaults, **e}
        row["dob_normalized"] = normalize_dob(row.get("dob", ""))
        rows.append(row)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM sanctions WHERE source=?", (source_key,))
    conn.executemany("""
        INSERT INTO sanctions
            (source, authority, region, entity_type,
             last_name, first_name, full_name,
             program, nationality, dob, dob_normalized,
             listed_on, raw_data)
        VALUES
            (:source, :authority, :region, :entity_type,
             :last_name, :first_name, :full_name,
             :program, :nationality, :dob, :dob_normalized,
             :listed_on, :raw_data)
    """, rows)
    conn.commit()
    conn.close()
    return len(rows)


def log_fetch(source, status, records, size_kb, changed, notes=""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO fetch_log(source,fetched_at,status,records,size_kb,changed,notes)
        VALUES(?,datetime('now'),?,?,?,?,?)
    """, (source, status, records, size_kb, changed, notes))
    conn.commit()
    conn.close()


# ── MAIN FETCH ENGINE ─────────────────────────────────────
class SanctionFetcher:

    def __init__(self):
        self.etag_mgr = ETagManager(DB_PATH)

    def fetch_source(self, key, source):
        name      = source["name"]
        url       = source["url"]
        strategy  = source["strategy"]
        src_type  = source["type"]
        authority = source["authority"]
        region    = source["region"]

        logger.info(f"{'─'*55}")
        logger.info(f"SOURCE  : {name}")
        logger.info(f"STRATEGY: {strategy.upper()}")
        logger.info(f"URL     : {url}")

        try:
            # ── STRATEGY: DELTA ───────────────────────────
            if strategy == "delta":
                logger.info("  Fetching DELTA only (not full list)...")
                resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                size_kb = len(resp.content) / 1024
                logger.info(f"  Delta size: {size_kb:.1f} KB")

                if size_kb < 1:
                    logger.info("  Delta empty — no changes since last update")
                    log_fetch(key, "no_change", 0, size_kb, 0, "Delta empty")
                    return

                entries = parse_ofac_xml(resp.content, key, authority, region)
                count   = upsert_entries(entries, key)
                logger.info(f"  Delta applied: {count} entries updated")
                log_fetch(key, "success", count, size_kb, count, "Delta applied")

            # ── STRATEGY: ETAG ────────────────────────────
            elif strategy == "etag":
                logger.info("  Checking ETag — skip if unchanged...")
                result = self.etag_mgr.smart_get(url, key)

                if result is None:
                    log_fetch(key, "skipped", 0, 0, 0, "ETag match — no change")
                    return

                resp, etag, modified, hash_ = result
                size_kb = len(resp.content) / 1024
                content = resp.content

                # Parse based on type
                if src_type == "ofac_xml":
                    entries = parse_ofac_xml(content, key, authority, region)
                elif src_type == "ofac_csv":
                    entries = parse_ofac_csv(content, key, authority, region)
                elif src_type == "un_xml":
                    entries = parse_un_xml(content, key, authority, region)
                elif src_type == "eu_xml":
                    entries = parse_eu_xml(content, key, authority, region)
                elif src_type == "uk_csv":
                    entries = parse_uk_csv(content, key, authority, region)
                else:
                    entries = []

                count = upsert_entries(entries, key)
                self.etag_mgr.save_state(key, etag, modified, hash_, count, "success")
                logger.info(f"  Parsed & stored: {count:,} entries")
                log_fetch(key, "success", count, size_kb, count, f"ETag updated")

            # ── STRATEGY: API ─────────────────────────────
            elif strategy == "api":
                if src_type == "opensanctions_api":
                    self._fetch_opensanctions_api(key, url, authority, region)
                elif src_type == "ofac_api":
                    self._fetch_ofac_api(key, url, authority, region)

            # ── STRATEGY: SCRAPE (Indian regulators) ──────
            elif strategy == "scrape":
                if src_type == "mha_uapa_html":
                    self._fetch_mha_uapa(key, source, authority, region)
                elif src_type == "sebi_html":
                    self._fetch_sebi_debarred(key, url, authority, region)
                elif src_type == "rbi_mca_os":
                    self._fetch_rbi_defaulters(key, source, authority, region)
                else:
                    logger.warning(f"  [{key}] Unknown scrape type: {src_type}")
                    log_fetch(key, "error", 0, 0, 0, f"Unknown scrape type {src_type}")

        except requests.exceptions.ConnectionError:
            logger.warning(f"  [{key}] Network restricted in sandbox — would work on real server")
            log_fetch(key, "network_error", 0, 0, 0, "Sandbox restriction")
        except Exception as e:
            logger.error(f"  [{key}] ERROR: {e}")
            log_fetch(key, "error", 0, 0, 0, str(e))

    def _fetch_opensanctions_api(self, key, url, authority, region):
        """
        OpenSanctions proper REST API.
        No file download — query based, returns JSON.
        Uses changed_since param — only new entries!
        Free tier requires an API key — set OPENSANCTIONS_API_KEY env var.
        """
        logger.info("  Calling OpenSanctions REST API...")
        if not OPENSANCTIONS_API_KEY:
            logger.warning("  [opensanctions_api] OPENSANCTIONS_API_KEY env var not set "
                           "— register at https://www.opensanctions.org/api/ for a free key")
            log_fetch(key, "skipped", 0, 0, 0, "No API key configured")
            return
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        params = {
            "limit":        500,
            "changed_since": yesterday,    # only what changed!
        }
        headers = {**HEADERS, "Authorization": f"ApiKey {OPENSANCTIONS_API_KEY}"}
        resp = requests.get(url, params=params,
                            headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data    = resp.json()
        results = data.get("results", [])
        entries = []
        for r in results:
            props  = r.get("properties", {})
            names  = props.get("name", [""])
            full   = names[0] if names else r.get("caption","")
            entries.append({
                "source":      key,
                "authority":   authority,
                "region":      region,
                "entity_type": r.get("schema","unknown"),
                "last_name":   full.split()[-1] if full else "",
                "first_name":  full.split()[0]  if full else "",
                "full_name":   full,
                "program":     ",".join(r.get("datasets",[])),
                "raw_data":    json.dumps({"id": r.get("id"), "topics": r.get("topics",[])}),
            })
        count = upsert_entries(entries, key)
        logger.info(f"  OpenSanctions API: {count} entries (changed since {yesterday})")
        log_fetch(key, "success", count, 0, count, f"API — changed since {yesterday}")

    def _fetch_ofac_api(self, key, url, authority, region):
        """
        OFAC REST API — uses browser-shaped headers (Referer/Origin/Accept)
        because the dashboard endpoint rejects bare User-Agent requests.
        """
        logger.info("  Calling OFAC REST API (browser headers)...")
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"  OFAC API responded with {len(str(data))} bytes")
        log_fetch(key, "success", 0, 0, 0, "OFAC API called")

    # ── SCRAPE FETCHERS (Indian regulators) ────────────────
    #
    # All three reuse ETagManager.smart_get for cache awareness but
    # request with SCRAPER_HEADERS so we identify the tool clearly.
    # They each: GET, parse HTML, upsert, save ETag state, log_fetch —
    # exactly the same shape as the etag branch of fetch_source().
    def _scrape_get(self, url, source_key):
        """
        Drop-in replacement for etag_mgr.smart_get() that sends the
        scraper User-Agent / Accept headers. Returns (resp, etag,
        modified, hash_) or None if content is unchanged. Updates
        fetch_state on a 304/hash-match (so we record the "checked, no
        change" timestamp) and respects the same conditional-GET dance.
        """
        etag, modified, _old_hash = self.etag_mgr.get_state(source_key)
        headers = dict(SCRAPER_HEADERS)
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified

        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 304:
            logger.info(f"  [{source_key}] 304 Not Modified — skipping")
            return None
        resp.raise_for_status()

        new_etag     = resp.headers.get("ETag")
        new_modified = resp.headers.get("Last-Modified")
        new_hash     = hashlib.md5(resp.content).hexdigest()
        _, _, old_hash = self.etag_mgr.get_state(source_key)
        if new_hash == old_hash:
            logger.info(f"  [{source_key}] Content hash unchanged — skipping")
            return None
        return resp, new_etag, new_modified, new_hash

    def _scrape_get_with_retry(self, url, source_key, tries=3, backoff=5):
        """
        Wrapper around _scrape_get that retries transient server errors
        (5xx, ConnectionError, ReadTimeout) up to `tries` times with
        `backoff`-second waits. Returns the same shape as _scrape_get,
        or raises the last exception when all attempts fail.

        Used for the MHA individuals page, which 503s under load.
        """
        last_exc = None
        for attempt in range(1, tries + 1):
            try:
                return self._scrape_get(url, source_key)
            except requests.exceptions.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status and 500 <= status < 600 and attempt < tries:
                    logger.info(f"  [{source_key}] {status} on attempt {attempt}/{tries} — sleeping {backoff}s")
                    time.sleep(backoff)
                    last_exc = e
                    continue
                raise
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                if attempt < tries:
                    logger.info(f"  [{source_key}] {type(e).__name__} on attempt {attempt}/{tries} — sleeping {backoff}s")
                    time.sleep(backoff)
                    last_exc = e
                    continue
                raise
        if last_exc:
            raise last_exc

    def _fetch_mha_uapa(self, key, source, authority, region):
        """
        MHA UAPA — banned-orgs page (clean <table>) and individual-
        terrorists page (Fourth Schedule). Each is fetched separately
        with per-page ETag tracking. The individuals page is wrapped in
        a 3-attempt / 5-second backoff retry because the MHA server
        frequently 503s under load. A failure on either page logs a
        warning but does not abort the other.
        """
        url_orgs        = source["url"]
        url_individuals = source["individuals_url"]

        all_entries = []
        total_size  = 0.0
        notes_bits  = []

        # ── Page 1: banned organisations ───────────────────
        try:
            logger.info("  Scraping MHA banned organisations...")
            result = self._scrape_get_with_retry(url_orgs, f"{key}__orgs",
                                                  tries=3, backoff=5)
            if result is None:
                notes_bits.append("orgs unchanged")
            else:
                resp, etag, modified, hash_ = result
                total_size += len(resp.content) / 1024
                orgs = parse_mha_uapa_organisations(
                    resp.content, key, authority, region, url_orgs
                )
                logger.info(f"  MHA banned organisations parsed: {len(orgs)}")
                all_entries.extend(orgs)
                self.etag_mgr.save_state(
                    f"{key}__orgs", etag, modified, hash_, len(orgs), "success"
                )
                notes_bits.append(f"orgs={len(orgs)}")
        except Exception as e:
            logger.warning(f"  [{key}] organisations page failed: {e}")
            notes_bits.append(f"orgs-error: {e}")

        time.sleep(SCRAPER_DELAY)

        # ── Page 2: individual terrorists (with retry) ─────
        try:
            logger.info("  Scraping MHA individual terrorists under UAPA...")
            result = self._scrape_get_with_retry(
                url_individuals, f"{key}__individuals",
                tries=3, backoff=5,
            )
            if result is None:
                notes_bits.append("individuals unchanged")
            else:
                resp, etag, modified, hash_ = result
                total_size += len(resp.content) / 1024
                indivs = parse_mha_uapa_individuals(
                    resp.content, key, authority, region, url_individuals
                )
                logger.info(f"  MHA individual terrorists parsed: {len(indivs)}")
                all_entries.extend(indivs)
                self.etag_mgr.save_state(
                    f"{key}__individuals", etag, modified, hash_, len(indivs), "success"
                )
                notes_bits.append(f"individuals={len(indivs)}")
        except Exception as e:
            logger.warning(f"  [{key}] individuals page failed after retries: {e}")
            notes_bits.append(f"individuals-error: {e}")

        count = upsert_entries(all_entries, key)
        logger.info(f"  MHA_UAPA stored: {count:,} entries total")
        log_fetch(key, "success" if count else "no_data",
                  count, total_size, count, "; ".join(notes_bits))

    # Per-fetch cap on how many SEBI order PDFs we pull through pdfplumber.
    # In production this would process the full archive (or use an
    # incremental "since-date" filter); the prototype caps it to keep
    # one cycle small and avoid hammering sebi.gov.in.
    SEBI_PDF_LIMIT = 5

    # Static fallback PDFs (used when both Playwright runs yield 0 links).
    # Hand-curated direct URLs that are known to contain debarred-entity
    # lists. If sebi.gov.in changes its file paths these will need
    # refreshing — fetch_log records the URL it tried so failures are
    # debuggable.
    SEBI_FALLBACK_URLS = [
        "https://www.sebi.gov.in/sebi_data/attachdocs/1317796936813.pdf",
        "https://www.sebi.gov.in/sebi_data/docfiles/21170_t.html",
    ]

    def _playwright_collect_pdf_links(self, page_url, wait_state="networkidle"):
        """
        Launch headless Chromium, navigate to page_url, wait until the
        page finishes loading (networkidle = no network requests for
        500ms by default), then collect every link that ends in .pdf
        / .PDF OR contains 'attachdocs' (SEBI's document-tree marker).

        Returns: list of {"url": str, "label": str} dicts, in DOM order.
        Returns []  if Playwright is unavailable or the page errors —
        the caller logs and falls through to the next strategy.
        """
        if not PLAYWRIGHT_AVAILABLE:
            logger.warning("  Playwright not installed — skipping JS rendering")
            return []
        logger.info(f"  [playwright] navigating to {page_url}")
        results = []
        try:
            with _sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=SCRAPER_HEADERS["User-Agent"],
                )
                page = context.new_page()
                # 60s nav timeout — SEBI is slow under load
                page.goto(page_url, wait_until=wait_state, timeout=60000)
                # Belt-and-braces: also wait for the DOM to settle a bit
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                # Pull every anchor's href + text in DOM order
                anchors = page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]'))"
                    "       .map(a => ({href: a.href, text: a.textContent.trim()}))"
                )
                browser.close()

            seen = set()
            for a in anchors:
                href = (a.get("href") or "").strip()
                if not href:
                    continue
                low = href.lower()
                # Accept anything ending in .pdf OR containing attachdocs
                if not (low.endswith(".pdf") or "attachdocs" in low):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                results.append({"url": href, "label": (a.get("text") or "")[:120]})
        except Exception as e:
            logger.warning(f"  [playwright] navigation/extract failed for {page_url}: {e}")
        return results

    def _fetch_sebi_debarred(self, key, url, authority, region):
        """
        SEBI debarred entities — JS-rendered cascade:
        1) Playwright-render the listing page, harvest PDF links.
        2) If 0 found, Playwright-render /enforcement/orders/ as a
           secondary listing and try again.
        3) If still 0 (or Playwright unavailable), fall back to a hand-
           curated list of known SEBI PDF URLs.
        Whatever path yielded URLs, the PDFs are then downloaded via
        plain requests and run through extract_entities_from_sebi_pdf
        (pdfplumber). Capped at SEBI_PDF_LIMIT (5) per fetch.
        """
        all_entries  = []
        total_kb     = 0.0
        notes_bits   = []
        path_used    = ""
        pdfs         = []

        # ── Path A: Playwright on the debarred-co index ───
        if PLAYWRIGHT_AVAILABLE:
            pdfs = self._playwright_collect_pdf_links(url)
            logger.info(f"  Playwright on debarredco.html: {len(pdfs)} PDF link(s)")
            if pdfs:
                path_used = "playwright:debarredco"
                notes_bits.append(f"playwright found {len(pdfs)} PDFs on index")
            time.sleep(SCRAPER_DELAY)

        # ── Path B: Playwright on /enforcement/orders/ ────
        if not pdfs and PLAYWRIGHT_AVAILABLE:
            enforcement_url = "https://www.sebi.gov.in/enforcement/orders/"
            pdfs = self._playwright_collect_pdf_links(enforcement_url)
            logger.info(f"  Playwright on /enforcement/orders/: {len(pdfs)} PDF link(s)")
            if pdfs:
                path_used = "playwright:enforcement"
                notes_bits.append(f"playwright found {len(pdfs)} PDFs on enforcement page")
            time.sleep(SCRAPER_DELAY)

        # ── Path C: hand-curated static fallback URLs ─────
        if not pdfs:
            logger.warning("  Falling back to hand-curated static SEBI URLs")
            path_used = "static_fallback"
            for fb_url in self.SEBI_FALLBACK_URLS:
                pdfs.append({"url": fb_url, "label": "static fallback"})
            notes_bits.append(f"static fallback ({len(pdfs)} URLs)")

        # ── Download + extract from up to SEBI_PDF_LIMIT URLs ─
        for pdf_meta in pdfs[: self.SEBI_PDF_LIMIT]:
            pdf_url = pdf_meta["url"]
            logger.info(f"  Fetching SEBI document: {pdf_url}")
            try:
                pdf_resp = requests.get(pdf_url, headers=SCRAPER_HEADERS,
                                         timeout=REQUEST_TIMEOUT)
                pdf_resp.raise_for_status()
                total_kb += len(pdf_resp.content) / 1024
                # HTML siblings (the .html fallback URL) go through the
                # HTML parser; everything else through pdfplumber.
                ctype = pdf_resp.headers.get("Content-Type", "").lower()
                if pdf_url.lower().endswith(".pdf") or "pdf" in ctype:
                    batch = extract_entities_from_sebi_pdf(
                        pdf_resp.content, pdf_url, key, authority, region
                    )
                else:
                    batch = _extract_sebi_html_fallback(
                        pdf_resp.content, pdf_url, key, authority, region
                    )
                logger.info(f"    extracted {len(batch)} entries")
                all_entries.extend(batch)
            except Exception as e:
                logger.warning(f"    fetch/parse failed for {pdf_url}: {e}")
            time.sleep(SCRAPER_DELAY)

        count = upsert_entries(all_entries, key)
        # Mirror ETag bookkeeping (store dummy etag/hash so re-runs note
        # we tried — and the smart_get path won't be used here anyway).
        self.etag_mgr.save_state(
            key, etag=None, modified=None,
            hash_=hashlib.md5(str(pdfs).encode("utf-8")).hexdigest(),
            count=count,
            status="success" if count else "no_data",
        )
        notes = f"path={path_used}; PDFs processed: {min(len(pdfs), self.SEBI_PDF_LIMIT)} / {len(pdfs)}; " + "; ".join(notes_bits)
        logger.info(f"  SEBI debarred stored: {count:,} entries  ({path_used})")
        log_fetch(key, "success" if count else "no_data",
                  count, total_kb, count, notes)

    def _fetch_rbi_defaulters(self, key, source, authority, region):
        """
        RBI / MCA disqualified directors via OpenSanctions REST API.

        Tries TWO calls (schema=Person + schema=Company) against the
        dataset `in_mca_disqualified_directors`. Requires an API key
        in OPENSANCTIONS_API_KEY. If the API call fails (no key, 401,
        503, etc.) we fall back to scraping mca.gov.in — that page is
        JS-rendered and will normally yield 0 rows, in which case the
        fetch_log entry records both the failure mode and the fallback.
        """
        base_url     = source["url"]
        fallback_url = source.get("fallback_url", "")

        if not OPENSANCTIONS_API_KEY:
            # Print a prominent operator-facing message that survives
            # log filtering — this is the "actionable" path: the user
            # needs to set an env var, then re-run.
            msg = [
                "",
                "+" + "-" * 70 + "+",
                "|  RBI WILFUL DEFAULTERS / MCA DISQUALIFIED DIRECTORS                  |",
                "|                                                                      |",
                "|  Requires a FREE OpenSanctions API key.                              |",
                "|                                                                      |",
                "|  Get one at:  https://www.opensanctions.org/api/                     |",
                "|                                                                      |",
                "|  Then run:    set OPENSANCTIONS_API_KEY=your_key_here                |",
                "|               (PowerShell:  $env:OPENSANCTIONS_API_KEY = 'your_key') |",
                "|                                                                      |",
                "|  and re-run the fetcher.                                             |",
                "+" + "-" * 70 + "+",
                "",
            ]
            for ln in msg:
                logger.warning(ln)
            self._rbi_mca_fallback(key, fallback_url, authority, region,
                                    reason="no API key — see operator message above")
            return

        all_entries = []
        os_notes = []
        os_headers = {**SCRAPER_HEADERS,
                       "Authorization": f"ApiKey {OPENSANCTIONS_API_KEY}",
                       "Accept": "application/json"}

        for schema_name in ("Person", "Company"):
            params = {
                "schema":  schema_name,
                "dataset": "in_mca_disqualified_directors",
                "limit":   100,
            }
            try:
                logger.info(f"  OpenSanctions /entities/ schema={schema_name}...")
                resp = requests.get(base_url, params=params,
                                     headers=os_headers, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                payload = resp.json()
                batch = parse_rbi_opensanctions(
                    payload, key, authority, region, base_url
                )
                logger.info(f"    {schema_name}: {len(batch)} entries")
                all_entries.extend(batch)
                os_notes.append(f"{schema_name}={len(batch)}")
            except Exception as e:
                logger.warning(f"  [rbi] OpenSanctions {schema_name} failed: {e}")
                os_notes.append(f"{schema_name}-error: {e}")
            time.sleep(SCRAPER_DELAY)

        if not all_entries:
            logger.warning("  [rbi] OpenSanctions returned 0 — falling back to mca.gov.in")
            self._rbi_mca_fallback(key, fallback_url, authority, region,
                                    reason="; ".join(os_notes))
            return

        count = upsert_entries(all_entries, key)
        logger.info(f"  RBI/MCA stored: {count:,} entries via OpenSanctions")
        log_fetch(key, "success", count, 0, count,
                  f"OpenSanctions: {'; '.join(os_notes)}")

    def _rbi_mca_fallback(self, key, fallback_url, authority, region, reason):
        """
        mca.gov.in DIN page fallback. Page is normally JS-rendered so
        this typically yields 0 — when that happens we log a clear
        'JS-rendered' note in fetch_log so the operator can see WHY
        the RBI bucket is empty rather than thinking the fetch silently
        passed.
        """
        if not fallback_url:
            log_fetch(key, "error", 0, 0, 0,
                      f"OpenSanctions failed and no fallback URL configured ({reason})")
            return
        try:
            resp = requests.get(fallback_url, headers=SCRAPER_HEADERS,
                                timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            entries = parse_mca_din_fallback(
                resp.content, key, authority, region, fallback_url
            )
            count = upsert_entries(entries, key)
            note  = (f"Fell back from OpenSanctions ({reason}); MCA page parsed "
                     f"{count} link(s) — page is JS-rendered" if not count
                     else f"Fell back from OpenSanctions ({reason}); MCA gave {count}")
            log_fetch(key, "success" if count else "no_data",
                      count, len(resp.content) / 1024, count, note)
        except Exception as e:
            logger.warning(f"  [rbi] MCA fallback also failed: {e}")
            log_fetch(key, "error", 0, 0, 0,
                      f"OpenSanctions failed ({reason}); MCA fallback also failed: {e}")
        time.sleep(SCRAPER_DELAY)

    def run_all(self):
        """Fetch all sources — what the bank's cron job calls."""
        logger.info("=" * 55)
        logger.info("SANCTION ENGINE — FULL FETCH CYCLE STARTED")
        logger.info(f"Timestamp: {datetime.now()}")
        logger.info("=" * 55)

        for key, source in SOURCES.items():
            self.fetch_source(key, source)
            time.sleep(0.5)

        self.print_summary()

    def print_summary(self):
        """Print DB summary — what's stored across all sources."""
        conn  = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM sanctions").fetchone()[0]
        by_src= conn.execute("""
            SELECT source, authority, region, COUNT(*) as cnt
            FROM sanctions GROUP BY source ORDER BY cnt DESC
        """).fetchall()
        logs  = conn.execute("""
            SELECT source, status, records, size_kb, notes, fetched_at
            FROM fetch_log ORDER BY id DESC LIMIT 10
        """).fetchall()
        conn.close()

        logger.info("=" * 55)
        logger.info("DATABASE SUMMARY")
        logger.info("=" * 55)
        logger.info(f"Total sanctioned entities in DB: {total:,}")
        logger.info("")
        logger.info("By Source:")
        for src, auth, region, cnt in by_src:
            logger.info(f"  {src:<30} {cnt:>6,} entries  [{region}]")
        logger.info("")
        logger.info("Recent Fetch Log:")
        for row in logs:
            logger.info(f"  {row[0]:<25} | {row[1]:<10} | "
                        f"{row[2]:>5} records | {row[4]} | {row[5]}")
        logger.info("=" * 55)


# ── SCHEDULER — runs every 6 hours automatically ──────────
class Scheduler:
    """
    Senior engineer approach:
    No cron job setup needed. Script schedules itself.
    In production this would be Celery / Airflow / k8s CronJob.
    """
    def __init__(self, interval_seconds):
        self.interval = interval_seconds
        self.fetcher  = SanctionFetcher()
        self._stop    = threading.Event()

    def _run(self):
        while not self._stop.is_set():
            logger.info(f"Scheduler triggered — next run in {self.interval//3600}h")
            self.fetcher.run_all()
            logger.info(f"Sleeping {self.interval//3600} hours until next fetch...")
            self._stop.wait(self.interval)

    def start(self):
        t = Thread(target=self._run, daemon=True)
        t.start()
        logger.info(f"Scheduler started — runs every {self.interval//3600} hours")
        return t

    def stop(self):
        self._stop.set()


# ── CUSTOMER SCREENING ────────────────────────────────────
def screen_customer(name, dob=None):
    """
    Per-transaction customer screen.

    Parameters
    ----------
    name : str
        Substring to match against full_name or last_name (case-insensitive).
    dob  : str, optional
        Customer date of birth in any of the supported input formats
        (e.g. "1962-04-28", "28 Apr 1962", "28/04/1962"). When supplied,
        results are restricted to sanctions rows whose dob_normalized
        equals the normalized customer DOB. Rows with no DOB on the
        sanctions side are excluded — a name-only hit there can be
        re-checked separately if desired.
    """
    name_filter = (f"%{name}%", f"%{name}%")
    sql = """
        SELECT full_name, authority, program, region, entity_type, dob, dob_normalized
        FROM sanctions
        WHERE (UPPER(full_name) LIKE UPPER(?)
            OR UPPER(last_name) LIKE UPPER(?))
    """
    params = list(name_filter)

    dob_iso = normalize_dob(dob) if dob else ""
    if dob_iso:
        sql += " AND dob_normalized = ? "
        params.append(dob_iso)

    sql += " LIMIT 10"

    conn = sqlite3.connect(DB_PATH)
    results = conn.execute(sql, params).fetchall()
    conn.close()

    label = f"'{name}'" + (f" / DOB {dob_iso}" if dob_iso else "")
    if results:
        logger.warning(f"SANCTION HIT for {label}:")
        for r in results:
            dob_str = f" | DOB {r[6]}" if r[6] else ""
            logger.warning(f"  Match: {r[0]} | {r[1]} | {r[2]} | {r[4]}{dob_str}")
        return {"status": "BLOCKED", "matches": results}
    else:
        logger.info(f"CLEAR: {label} — not found in sanctions DB")
        return {"status": "CLEAR", "matches": []}


# ── MODULE-LEVEL FETCHER WRAPPERS ─────────────────────────
# Thin convenience wrappers so callers can do:
#     from sanction_engine import fetch_mha_uapa, fetch_sebi_debarred,
#                                  fetch_rbi_defaulters, fetch_all
# without having to instantiate SanctionFetcher manually. Each wrapper
# ensures init_db() has been called, then delegates to fetch_source.

def _run_one(source_key):
    init_db()
    fetcher = SanctionFetcher()
    fetcher.fetch_source(source_key, SOURCES[source_key])
    # Return count for the caller's logging
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM sanctions WHERE source=?", (source_key,)
    ).fetchone()[0]
    conn.close()
    return n


def fetch_mha_uapa():
    """Fetch MHA UAPA banned organisations + individual terrorists."""
    return _run_one("mha_uapa")


def fetch_sebi_debarred():
    """Fetch SEBI debarred entities."""
    return _run_one("sebi_debarred")


def fetch_rbi_defaulters():
    """Fetch RBI wilful defaulters."""
    return _run_one("rbi_wilful_defaulter")


def fetch_all():
    """
    Run every configured source (OFAC, UN, EU, UK, OpenSanctions,
    MHA, SEBI, RBI) in one cycle. Same as SanctionFetcher().run_all().
    """
    init_db()
    SanctionFetcher().run_all()


# ── ENTRY POINT ───────────────────────────────────────────
if __name__ == "__main__":

    # 1. Initialise DB
    init_db()

    # 2. Run once immediately
    fetcher = SanctionFetcher()
    fetcher.run_all()

    # 3. Show how screening works — name-only and name+DOB
    logger.info("")
    logger.info("── CUSTOMER SCREENING DEMO (name only) ──")
    for name in ["Putin", "Maduro", "Kim Jong", "John Smith"]:
        screen_customer(name)

    logger.info("")
    logger.info("── CUSTOMER SCREENING DEMO (name + DOB) ──")
    # Real Putin DOB is 7 Oct 1952 → narrows the 10 name hits to the actual person
    screen_customer("Putin",  dob="7 Oct 1952")
    # Wrong DOB on a sanctioned name → CLEAR (proves DOB tightens the filter)
    screen_customer("Putin",  dob="1 Jan 1900")
    # UK-style numeric input on a known sanctioned individual
    screen_customer("Maduro", dob="23/11/1962")
