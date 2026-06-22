#!/usr/bin/env python3
"""
Job Digest Agent
════════════════
Finds EMT/Clinical, Paramedic, Medical Assistant, and Physician Assistant job
listings within 50 miles of Palisade, CO and emails a formatted HTML digest.

Runs automatically via GitHub Actions every two days.
"""

import hashlib
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from geopy.distance import geodesic
from geopy.geocoders import Nominatim

try:
    import pandas as pd
    from jobspy import scrape_jobs as jobspy_scrape
    JOBSPY_AVAILABLE = True
except ImportError:
    JOBSPY_AVAILABLE = False


# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION  ← The only section you normally need to edit
# ═════════════════════════════════════════════════════════════════════════════
#
# ┌─ HOW TO ADD / REMOVE EMAIL RECIPIENTS ─────────────────────────────────┐
# │                                                                         │
# │  To ADD someone:    add a new line inside EMAIL_RECIPIENTS, e.g.:      │
# │      "newperson@example.com",                                           │
# │                                                                         │
# │  To REMOVE someone: delete or comment out (#) their line below.        │
# │                                                                         │
# │  After editing, commit the change and push — GitHub Actions will pick  │
# │  it up automatically on the next scheduled run.                        │
# └─────────────────────────────────────────────────────────────────────────┘
EMAIL_RECIPIENTS: List[str] = [
    "ChloeRittenhouse@gmail.com",
    "bcmurphy21@gmail.com",
]

EMAIL_SENDER  = "ChloeRittenhouse@gmail.com"
EMAIL_SUBJECT = "Job Digest — EMT/Clinical, Paramedic, MA & PA Roles within 50mi of Palisade, CO"

# Geography
PALISADE_COORDS    = (39.1086, -108.3481)   # lat/lon of Palisade, CO
SEARCH_CENTER      = "Grand Junction, CO"   # nearest city used as jobspy search anchor
MAX_DISTANCE_MILES = 50
DAYS_TO_SEARCH     = 30                     # lookback window (days) for active listings

# Internal
STATE_FILE         = "seen_jobs.json"

# ═════════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Job classification rules
# ─────────────────────────────────────────────────────────────────────────────

# Title substrings that qualify a listing as an EMT/clinical role.
# "emt" and "wfr" use word-boundary regex (see classify()) to avoid
# substring matches like "cement" or "dwfr".
EMT_TITLE_INCLUDE = [
    "emt",                              # matched with \b word boundary
    "emergency medical technician",
    "er tech", "ed tech",
    "emergency department tech", "emergency room tech",
    "trauma tech",
    "urgent care tech", "urgent care technician",
    "ski patrol",
    "search and rescue", "sar tech",
    "flight medic",                     # intentionally excludes generic "flight crew"
    "wilderness first responder", "wfr",  # wfr matched with \b word boundary
    "critical care tech",
]

# Description-only fallback: credential terms that confirm EMT is required/preferred.
# Deliberately narrow — "emergency medical" and "first responder" are omitted because
# they also appear in fitness, security, and facility-management job descriptions.
EMT_DESC_INCLUDE = [
    r"\bemt\b",
    r"\bemt-b\b", r"\bemt-basic\b", r"\bemt-advanced\b", r"\bemt-iv\b",
    r"\bemergency medical technician\b",
    r"\bparamedic\b",
    r"\bwilderness first responder\b",
    r"\bwfr\b",
]

# Paramedic title substrings. "emt-p" is a safe substring; the short abbreviations
# in PARAMEDIC_TITLE_ABBR are matched with \b word boundaries (see classify()) to
# avoid matching inside unrelated words.
PARAMEDIC_TITLE_INCLUDE = [
    "paramedic",                        # covers "flight paramedic", "community paramedic", etc.
    "emt-p", "emt-paramedic",
    "critical care paramedic",
]
PARAMEDIC_TITLE_ABBR = [
    "ccp",                              # critical care paramedic
]

# Medical Assistant title substrings. Short credential abbreviations in
# MA_TITLE_ABBR are matched with \b word boundaries to avoid matching inside
# words like "pharma" (which contains "rma").
MA_TITLE_INCLUDE = [
    "medical assistant",
    "certified medical assistant",
    "registered medical assistant",
    "clinical medical assistant",
]
MA_TITLE_ABBR = [
    "cma", "rma", "ma-c",
]

# Within the Medical Assistant category, distinguish listings where a national
# certification (CMA / RMA / "nationally certified") is clearly REQUIRED from
# those where it is merely preferred / optional / not required. Matched against
# the description (case-insensitive). Anything ambiguous falls into the
# "preferred / not required" bucket so the "required" bucket stays high-confidence.
CMA_REQUIRED_PATTERNS = [
    r"\bcma\b.{0,60}\brequired\b",
    r"\brequired\b.{0,60}\bcma\b",
    r"\bcertified medical assistant\b.{0,60}\brequired\b",
    r"\brequired\b.{0,60}\bcertified medical assistant\b",
    r"\b(?:national|nationally)\b.{0,40}\bcertif\w+.{0,20}\brequired\b",
    r"\b(?:national|nationally)\s+certif\w+\s+required\b",
    r"\bcertification\s+required\b",
    r"\bmust\s+(?:be\s+)?(?:a\s+)?(?:nationally\s+)?certif\w+",
    r"\bmust\s+(?:have|hold|possess)\b.{0,40}\b(?:cma|rma|certification)\b",
    r"\bcma\s*\(?aama\)?\b.{0,40}\brequired\b",
]
# Signals that certification is optional → keep in the "preferred / not required"
# bucket even if a "required" word appears elsewhere (e.g. "CMA preferred, BLS required").
CMA_OPTIONAL_PATTERNS = [
    r"\bcertification\s+(?:is\s+)?preferred\b",
    r"\bcma\b.{0,30}\bpreferred\b",
    r"\bpreferred\b.{0,30}\b(?:cma|certification)\b",
    r"\bcertification\s+(?:is\s+)?not\s+required\b",
    r"\bno\s+certification\s+required\b",
    r"\bwilling\s+to\s+train\b",
    r"\bor\s+equivalent\b",
    r"\bregistered\s+or\s+certified\b",
    r"\bcertified\s+or\s+registered\b",
]

# PA title substrings
PA_TITLE_INCLUDE = [
    "physician assistant", "pa-c", "physician associate",
]

# Regex patterns that indicate a strict MA/CNA requirement (no EMT path offered).
# NOTE: This only gates the EMT/PA classification paths now that Medical Assistant
# is a desired role in its own right — MA-titled listings are classified before
# this exclusion is consulted (see classify()).
MA_CNA_REQUIRED_PATTERNS = [
    r"(?:cna|certified nursing assistant)\b.{0,80}required",
    r"(?:medical assistant|(?<!\w)ma(?!\w)).{0,80}required",
    r"required.{0,80}(?:cna|certified nursing assistant)\b",
    r"required.{0,80}(?:medical assistant)\b",
    r"must\s+(?:have|hold|possess).{0,80}(?:cna|medical assistant)\b",
    r"(?:cna|medical assistant)\s+(?:license|certification|cert)\s+required",
]

# Title substrings that always exclude a listing
EXCLUDE_TITLE_HARD = [
    "ambulance driver", "transport driver", "transport technician",
    "ems transport", "basic life support transport", "bls transport",
    "firefighter", "fire fighter", "wildland fire", "fire protection officer",
    # Common false-positive technical roles
    "electrical", "electrician", "hvac", "plumber", "plumbing",
    "fitness coordinator", "fitness director", "personal trainer",
    "information technology", "it technician", "it tech",
    "facility tech", "maintenance tech", "installation tech",
]

# Work-type substrings that indicate remote/hybrid (checked against the full
# combined text: title + description + employment type).
EXCLUDE_REMOTE_KEYWORDS = [
    "remote", "hybrid", "work from home", "wfh",
    "telehealth only", "virtual only", "fully virtual",
]

# Title-only remote/online signals. Checked against the title alone so that a
# description mentioning "apply online" doesn't wrongly exclude an in-person job,
# while titles like "Online Medical Assistant" or "Virtual Paramedic" are dropped.
EXCLUDE_TITLE_REMOTE = [
    "online", "virtual", "telehealth", "remote", "work from home", "wfh",
]


def _txt(*parts: Optional[str]) -> str:
    """Combine and lowercase multiple strings for keyword matching."""
    return " ".join((p or "").lower() for p in parts)


def _matches_any(text: str, substrings: List[str]) -> bool:
    """True if any plain substring appears in text."""
    return any(kw in text for kw in substrings)


def _matches_any_word(text: str, words: List[str]) -> bool:
    """True if any term matches text on \\b word boundaries (safe for abbreviations)."""
    return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)


def classify(title: str, description: str = "", employment_type: str = "") -> Optional[str]:
    """
    Classify a job listing.
    Returns 'emt_clinical', 'paramedic', 'ma_cma_required', 'ma', 'pa',
    or None (exclude).

    Medical Assistant listings split into:
      • 'ma_cma_required' — national certification (CMA/RMA) clearly required
      • 'ma'              — certification preferred / optional / not required
    """
    combined = _txt(title, description, employment_type)
    t = title.lower()

    # Hard exclusions ─────────────────────────────────────────────────────────

    for kw in EXCLUDE_REMOTE_KEYWORDS:
        if kw in combined:
            return None

    # Title-level online/virtual exclusion (e.g. "Online Medical Assistant")
    for kw in EXCLUDE_TITLE_REMOTE:
        if kw in t:
            return None

    for kw in EXCLUDE_TITLE_HARD:
        if kw in t:
            return None

    if re.search(r"\bfire(?:fighter| department| district)\b", t):
        return None

    # Pure transport/ambulance roles (allow flight)
    if re.search(r"\b(?:ambulance|transport)\b", t) and "flight" not in t:
        return None

    # Paramedic classification ──────────────────────────────────────────────────
    # Checked before the MA/CNA exclusion and EMT path so paramedic-titled roles
    # land in their own category.
    if _matches_any(t, PARAMEDIC_TITLE_INCLUDE) or _matches_any_word(t, PARAMEDIC_TITLE_ABBR):
        return "paramedic"

    # Medical Assistant classification ───────────────────────────────────────────
    # Checked before the MA/CNA "required" exclusion because Medical Assistant is
    # now a desired role rather than a disqualifier. Split by whether a national
    # certification is clearly required vs preferred/optional.
    if _matches_any(t, MA_TITLE_INCLUDE) or _matches_any_word(t, MA_TITLE_ABBR):
        return "ma_cma_required" if _cma_required(title, description) else "ma"

    # Exclude if strict MA/CNA required and no EMT path exists (gates EMT/PA only)
    if _requires_ma_cna_without_emt(combined):
        return None

    # PA classification ────────────────────────────────────────────────────────
    for kw in PA_TITLE_INCLUDE:
        if kw in t:
            return "pa"

    # EMT/Clinical classification ──────────────────────────────────────────────
    for kw in EMT_TITLE_INCLUDE:
        if kw in ("emt", "wfr"):
            # Word-boundary match: "emt" must not be a substring of another word
            if re.search(r"\b" + re.escape(kw) + r"\b", t):
                return "emt_clinical"
        elif kw in t:
            return "emt_clinical"

    # Description-only fallback ────────────────────────────────────────────────
    # Only qualify if an EMT/paramedic credential is explicitly required OR
    # preferred — not just mentioned in passing (e.g. "call EMT if injured").
    desc = description.lower()
    cred_in_desc = any(re.search(pat, desc) for pat in EMT_DESC_INCLUDE)
    if cred_in_desc:
        # Require the credential to appear near a requirement/preference signal
        requirement_context = re.search(
            r"\bemt\b.{0,120}\b(?:required|preferred|certification|licensed|certified|must have|minimum)\b"
            r"|\b(?:required|preferred|must have|minimum).{0,120}\bemt\b"
            r"|\bparamedic\b.{0,80}\b(?:required|preferred|certification|licensed|certified)\b"
            r"|\b(?:required|preferred).{0,80}\bparamedic\b"
            r"|\bwilderness first responder\b.{0,80}\b(?:required|preferred|wfr)\b"
            r"|\b(?:required|preferred).{0,80}\bwilderness first responder\b",
            desc,
            re.IGNORECASE,
        )
        if requirement_context:
            return "emt_clinical"

    return None


def _requires_ma_cna_without_emt(combined_text: str) -> bool:
    """Return True only when MA/CNA is strictly required AND no EMT path is offered."""
    # Use word-boundary match for "emt" so "cement" doesn't trigger a false negative
    emt_signals = [r"\bemt\b", r"\bparamedic\b", r"\bwilderness\b"]
    if any(re.search(sig, combined_text) for sig in emt_signals):
        return False  # EMT can fill the role → keep it

    for pat in MA_CNA_REQUIRED_PATTERNS:
        if re.search(pat, combined_text, re.IGNORECASE):
            return True
    return False


def _cma_required(title: str, description: str) -> bool:
    """
    For a Medical Assistant listing, return True only when a national
    certification (CMA/RMA) is CLEARLY required.

    An explicit "preferred / not required / willing to train" signal wins over a
    stray "required" elsewhere, so ambiguous listings fall into the general
    (preferred/optional) bucket rather than the high-confidence "required" one.
    """
    text = _txt(title, description)

    if any(re.search(pat, text, re.IGNORECASE) for pat in CMA_OPTIONAL_PATTERNS):
        return False

    return any(re.search(pat, text, re.IGNORECASE) for pat in CMA_REQUIRED_PATTERNS)


# ─────────────────────────────────────────────────────────────────────────────
# Job data model
# ─────────────────────────────────────────────────────────────────────────────

class Job:
    def __init__(
        self,
        title: str,
        employer: str,
        location: str,
        url: str,
        posted_date: Optional[datetime] = None,
        pay_range: str = "",
        deadline: str = "",
        employment_type: str = "",
        description: str = "",
        category: str = "",
        source: str = "",
    ):
        self.title           = (title or "").strip()
        self.employer        = (employer or "").strip()
        self.location        = (location or "").strip()
        self.url             = (url or "").strip()
        self.posted_date     = posted_date
        self.pay_range       = (pay_range or "").strip()
        self.deadline        = (deadline or "").strip()
        self.employment_type = (employment_type or "").strip()
        self.description     = description or ""
        self.category        = category
        self.source          = source
        self._dist: Optional[float] = None

    @property
    def distance_miles(self) -> Optional[float]:
        if self._dist is None:
            self._dist = _geocode_distance(self.location)
        return self._dist

    @property
    def job_id(self) -> str:
        key = f"{self.title}|{self.employer}|{self.location}|{self.url}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def within_range(self) -> bool:
        d = self.distance_miles
        return d is not None and d <= MAX_DISTANCE_MILES

    def posted_within_days(self, days: int) -> bool:
        if not self.posted_date:
            return True  # unknown date → include optimistically
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        pd_ = self.posted_date
        if pd_.tzinfo is None:
            pd_ = pd_.replace(tzinfo=timezone.utc)
        return pd_ >= cutoff


# ─────────────────────────────────────────────────────────────────────────────
# Geocoding (in-memory cache + 1 req/sec rate limit for Nominatim ToS)
# ─────────────────────────────────────────────────────────────────────────────

_geolocator  = Nominatim(user_agent="job_digest_agent_v1", timeout=15)
_geo_cache: Dict[str, Optional[Tuple[float, float]]] = {}


def _geocode_distance(location_str: str) -> Optional[float]:
    """Return miles from Palisade, CO. Caches results; respects Nominatim rate limit."""
    if not location_str:
        return None

    key = location_str.strip().lower()
    if key in _geo_cache:
        coords = _geo_cache[key]
    else:
        coords = None
        for probe in [location_str, f"{location_str}, USA"]:
            try:
                time.sleep(1.1)  # Nominatim: ≤ 1 request/second
                result = _geolocator.geocode(probe)
                if result:
                    coords = (result.latitude, result.longitude)
                    break
            except Exception as exc:
                log.debug(f"Geocode error for '{probe}': {exc}")
        _geo_cache[key] = coords

    if coords is None:
        return None
    return geodesic(PALISADE_COORDS, coords).miles


# ─────────────────────────────────────────────────────────────────────────────
# Job board search via python-jobspy
# ─────────────────────────────────────────────────────────────────────────────

_JOBSPY_QUERIES = [
    (
        'EMT OR "ER Tech" OR "ED Tech" OR "emergency technician" '
        'OR "ski patrol" OR "urgent care tech" OR "trauma tech" '
        'OR "emergency department tech" OR "wilderness first responder"',
        "emt_clinical",
    ),
    (
        'Paramedic OR "EMT-P" OR "flight paramedic" '
        'OR "community paramedic" OR "critical care paramedic"',
        "paramedic",
    ),
    (
        '"Medical Assistant" OR "Certified Medical Assistant" '
        'OR "Clinical Medical Assistant" OR "Registered Medical Assistant" OR CMA',
        "ma",
    ),
    ('"Physician Assistant" OR "PA-C" OR "physician associate"', "pa"),
]


def _parse_pay(row) -> str:
    """Build a human-readable pay range string from a jobspy DataFrame row."""
    try:
        lo = row.get("min_amount")
        hi = row.get("max_amount")
        if pd.isna(lo) and pd.isna(hi):
            return ""
        sym      = str(row.get("currency") or "$")
        interval = str(row.get("interval") or "").lower().strip()
        suffix   = f"/{interval}" if interval and interval not in ("none", "nan", "") else ""
        if not pd.isna(lo) and not pd.isna(hi):
            return f"{sym}{int(lo):,}–{sym}{int(hi):,}{suffix}"
        if not pd.isna(lo):
            return f"From {sym}{int(lo):,}{suffix}"
        return f"Up to {sym}{int(hi):,}{suffix}"
    except Exception:
        return ""


def search_job_boards() -> List[Job]:
    """Search Indeed, LinkedIn, ZipRecruiter, and Glassdoor via python-jobspy."""
    if not JOBSPY_AVAILABLE:
        log.warning("python-jobspy not installed — skipping job board search")
        return []

    results: List[Job] = []

    for query, _hint in _JOBSPY_QUERIES:
        log.info(f"[jobspy] Searching: {query[:72]}…")
        try:
            df = jobspy_scrape(
                site_name     = ["indeed", "linkedin", "zip_recruiter", "glassdoor"],
                search_term   = query,
                location      = SEARCH_CENTER,
                distance      = 60,              # slightly wider; we re-filter by distance
                results_wanted= 50,
                hours_old     = DAYS_TO_SEARCH * 24,
                country_indeed= "USA",
            )

            if df is None or df.empty:
                log.info("[jobspy] No results for this query")
                continue

            for _, row in df.iterrows():
                try:
                    # Parse posted date
                    posted = None
                    raw = row.get("date_posted")
                    if raw is not None and str(raw) not in ("NaT", "None", "nan", ""):
                        if isinstance(raw, datetime):
                            posted = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
                        else:
                            posted = datetime.fromisoformat(str(raw)).replace(tzinfo=timezone.utc)

                    # Skip confirmed remote listings
                    if row.get("is_remote"):
                        continue

                    # Prefer direct employer URL
                    url = str(row.get("job_url_direct") or row.get("job_url") or "").strip()
                    if not url or url == "nan":
                        continue

                    job = Job(
                        title           = str(row.get("title")    or ""),
                        employer        = str(row.get("company")  or ""),
                        location        = str(row.get("location") or ""),
                        url             = url,
                        posted_date     = posted,
                        pay_range       = _parse_pay(row),
                        employment_type = str(row.get("job_type") or ""),
                        description     = str(row.get("description") or ""),
                        source          = str(row.get("site")     or "job board"),
                    )
                    results.append(job)
                except Exception as exc:
                    log.debug(f"Row parse error: {exc}")

        except Exception as exc:
            log.warning(f"[jobspy] Search failed: {exc}")

        time.sleep(5)  # polite delay between queries

    log.info(f"[jobspy] Collected {len(results)} raw listings")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Employer career page scrapers
# ─────────────────────────────────────────────────────────────────────────────

_REQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}


def _get(url: str, **kwargs) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=_REQ_HEADERS, timeout=25, **kwargs)
        r.raise_for_status()
        return r
    except Exception as exc:
        log.warning(f"GET {url[:80]} → {exc}")
        return None


# ── Intermountain Health ──────────────────────────────────────────────────────

def _scrape_intermountain() -> List[Job]:
    """Scrape Intermountain Health careers (Oracle Taleo ATS)."""
    jobs: List[Job] = []
    base = "https://careers.intermountainhealth.org"
    searches = [
        "EMT", "ER Tech", "emergency technician",
        "Paramedic", "Medical Assistant",
        "Physician Assistant", "ski patrol", "urgent care",
    ]

    for kw in searches:
        r = _get(
            f"{base}/search-jobs/results",
            params={
                "keywords" : kw,
                "location" : "Grand Junction, CO",
                "latitude" : "39.0639",
                "longitude": "-108.5506",
                "radius"   : "80",   # km ≈ 50 mi
            },
        )
        if r and r.status_code == 200:
            try:
                data = r.json()
                for item in data.get("jobs", []):
                    city  = item.get("city", "")
                    state = item.get("state", "CO")
                    loc   = f"{city}, {state}" if city else "Colorado"
                    href  = item.get("applyUrl") or item.get("url") or ""
                    if href and not href.startswith("http"):
                        href = base + href
                    if not href:
                        href = base + "/careers"
                    jobs.append(Job(
                        title           = item.get("title", ""),
                        employer        = "Intermountain Health",
                        location        = loc,
                        url             = href,
                        employment_type = item.get("jobType", ""),
                        description     = item.get("description", ""),
                        source          = "Intermountain Health",
                    ))
            except Exception as exc:
                log.debug(f"Intermountain JSON parse error: {exc}")
        time.sleep(1.5)

    # HTML fallback if API returned nothing
    if not jobs:
        r = _get(f"{base}/search-jobs", params={"keywords": "EMT OR 'Physician Assistant'", "location": "Grand Junction CO"})
        if r:
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select("li[data-job-id], .job-tile, article.job, .search-result-item"):
                a = el.select_one("h2 a, h3 a, .job-title a, a[href*='/job/']")
                if not a:
                    continue
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = base + href
                loc_el = el.select_one(".location, .job-location")
                jobs.append(Job(
                    title    = a.get_text(strip=True),
                    employer = "Intermountain Health",
                    location = loc_el.get_text(strip=True) if loc_el else "Colorado",
                    url      = href,
                    source   = "Intermountain Health",
                ))

    log.info(f"[Intermountain Health] {len(jobs)} raw listings")
    return jobs


# ── CommonSpirit Health / St. Mary's Medical Center Grand Junction ────────────

def _scrape_commonspirit() -> List[Job]:
    """Scrape CommonSpirit Health (Workday ATS). Includes St. Mary's Grand Junction."""
    jobs: List[Job] = []
    searches = [
        "EMT", "ER Tech", "emergency technician",
        "Paramedic", "Medical Assistant",
        "Physician Assistant", "urgent care tech",
    ]

    for kw in searches:
        r = _get(
            "https://jobs.commonspirit.org/search/",
            params={"q": kw, "location": "Grand Junction, CO", "radius": "50"},
        )
        if r:
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select("[data-job-id], .job-tile, .results__item, .job-result"):
                a = el.select_one("h2 a, h3 a, a.job-link, a[href*='/job/']")
                if not a:
                    continue
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = "https://jobs.commonspirit.org" + href
                loc_el = el.select_one(".location, .job-location, [data-location]")
                jobs.append(Job(
                    title    = a.get_text(strip=True),
                    employer = "CommonSpirit Health",
                    location = loc_el.get_text(strip=True) if loc_el else "Colorado",
                    url      = href,
                    source   = "CommonSpirit Health",
                ))
        time.sleep(1.5)

    log.info(f"[CommonSpirit Health] {len(jobs)} raw listings")
    return jobs


# ── Valley View Hospital (Glenwood Springs) ───────────────────────────────────

def _scrape_valley_view() -> List[Job]:
    """Scrape Valley View Hospital career listings."""
    jobs: List[Job] = []
    r = _get("https://www.vvh.org/careers/")
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    # Try job-listing-style elements first
    for el in soup.select(".career-listing, .job-posting, .position-item, [class*='job']"):
        a = el.select_one("a[href]") or el.find_parent("a")
        title_el = el.select_one("h2, h3, h4, .title, strong")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not (5 < len(title) < 120):
            continue
        skip_words = ["careers", "employment", "apply now", "search", "department", "about"]
        if any(w in title.lower() for w in skip_words):
            continue
        href = ""
        if a:
            href = a.get("href", "")
            if not href.startswith("http"):
                href = "https://www.vvh.org" + href
        if not href:
            href = "https://www.vvh.org/careers/"
        jobs.append(Job(
            title    = title,
            employer = "Valley View Hospital",
            location = "Glenwood Springs, CO",
            url      = href,
            source   = "Valley View Hospital",
        ))

    log.info(f"[Valley View Hospital] {len(jobs)} raw listings")
    return jobs


def scrape_employers() -> List[Job]:
    """Run all employer scrapers and return combined results."""
    all_jobs: List[Job] = []
    for fn in (_scrape_intermountain, _scrape_commonspirit, _scrape_valley_view):
        try:
            all_jobs.extend(fn())
        except Exception as exc:
            log.warning(f"Scraper {fn.__name__} raised: {exc}")
    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as exc:
            log.warning(f"Could not read state file: {exc}")
    return {"seen_job_ids": [], "last_run": None}


def save_state(state: Dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        log.info(f"State saved ({len(state.get('seen_job_ids', []))} seen IDs)")
    except Exception as exc:
        log.warning(f"Could not save state: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Email formatting
# ─────────────────────────────────────────────────────────────────────────────

def _job_card(job: Job, new: bool) -> str:
    badge = (
        '<span style="background:#16a34a;color:#fff;font-size:11px;font-weight:700;'
        'padding:2px 9px;border-radius:999px;margin-right:7px;vertical-align:middle;">NEW</span>'
        if new else ""
    )
    dist = f"{job.distance_miles:.0f} mi from Palisade" if job.distance_miles is not None else "distance unknown"

    meta_items = []
    if job.pay_range:
        meta_items.append(f"💰 {job.pay_range}")
    if job.deadline:
        meta_items.append(f"📅 Deadline: {job.deadline}")
    if job.employment_type and job.employment_type.lower() not in ("none", "nan", ""):
        meta_items.append(f"⏱ {job.employment_type.title()}")
    if job.posted_date:
        meta_items.append(f"Posted: {job.posted_date.strftime('%b %-d, %Y')}")

    meta_html = "  ".join(
        f'<span style="color:#374151;">{item}</span>'
        for item in meta_items
    )

    return f"""
<div style="border:1px solid #e5e7eb;border-radius:8px;padding:15px 17px;margin-bottom:10px;background:#ffffff;">
  <div style="margin-bottom:5px;">
    {badge}<a href="{job.url}" style="font-size:15px;font-weight:600;color:#1d4ed8;text-decoration:none;">{job.title}</a>
  </div>
  <div style="color:#111827;font-size:13px;margin-bottom:7px;">
    <strong>{job.employer}</strong>
    &nbsp;·&nbsp; {job.location}
    &nbsp;·&nbsp; <span style="color:#6b7280;">{dist}</span>
  </div>
  <div style="font-size:13px;line-height:2.0;">{meta_html}</div>
  <div style="margin-top:9px;">
    <a href="{job.url}" style="font-size:13px;color:#1d4ed8;text-decoration:none;">View &amp; Apply →</a>
  </div>
</div>"""


def _section_html(heading: str, jobs: List[Job], seen_ids: Set[str]) -> str:
    if not jobs:
        body = '<p style="color:#6b7280;font-style:italic;font-size:13px;margin:0;">No listings at this time.</p>'
    else:
        body = "".join(_job_card(j, j.job_id not in seen_ids) for j in jobs)
    return f"""
<div style="margin-bottom:26px;">
  <h3 style="margin:0 0 12px;font-size:15px;font-weight:700;color:#111827;">{heading}</h3>
  {body}
</div>"""


def build_email_html(new_jobs: List[Job], active_jobs: List[Job], seen_ids: Set[str]) -> str:
    today     = datetime.now(timezone.utc).strftime("%B %-d, %Y")
    emt_new        = [j for j in new_jobs    if j.category == "emt_clinical"]
    para_new       = [j for j in new_jobs    if j.category == "paramedic"]
    ma_req_new     = [j for j in new_jobs    if j.category == "ma_cma_required"]
    ma_pref_new    = [j for j in new_jobs    if j.category == "ma"]
    pa_new         = [j for j in new_jobs    if j.category == "pa"]
    emt_active     = [j for j in active_jobs if j.category == "emt_clinical"]
    para_active    = [j for j in active_jobs if j.category == "paramedic"]
    ma_req_active  = [j for j in active_jobs if j.category == "ma_cma_required"]
    ma_pref_active = [j for j in active_jobs if j.category == "ma"]
    pa_active      = [j for j in active_jobs if j.category == "pa"]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Job Digest</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:660px;margin:0 auto;padding:24px 12px;">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1e3a5f 0%,#1d4ed8 100%);border-radius:12px;padding:28px 24px;margin-bottom:20px;">
    <h1 style="margin:0 0 6px;color:#fff;font-size:20px;font-weight:700;">🩺 Job Digest</h1>
    <p style="margin:0;color:#bfdbfe;font-size:13px;">
      EMT/Clinical, Paramedic, Medical Assistant &amp; PA Roles &nbsp;·&nbsp; Within 50 miles of Palisade, CO &nbsp;·&nbsp; {today}
    </p>
  </div>

  <!-- Summary bar -->
  <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:12px 16px;margin-bottom:24px;font-size:13px;color:#1e40af;">
    <strong>{len(new_jobs)} new listing(s)</strong> since last digest
    &nbsp;|&nbsp;
    <strong>{len(active_jobs)}</strong> additional active in the last 30 days
  </div>

  <!-- ═══ SECTION 1: New since last digest ═══ -->
  <h2 style="font-size:17px;font-weight:700;margin:0 0 4px;color:#111827;">🆕 New Since Last Digest</h2>
  <p style="font-size:12px;color:#6b7280;margin:0 0 14px;">Listings added or updated since the previous run</p>

  {_section_html("🚑 EMT / Clinical Roles", emt_new, seen_ids)}
  {_section_html("🚑 Paramedic Roles", para_new, seen_ids)}
  {_section_html("🩹 Medical Assistant Roles — CMA Required", ma_req_new, seen_ids)}
  {_section_html("🩹 Medical Assistant Roles — CMA Preferred / Not Required", ma_pref_new, seen_ids)}
  {_section_html("🩺 Physician Assistant Roles", pa_new, seen_ids)}

  <!-- ═══ SECTION 2: Active in the last 30 days ═══ -->
  <h2 style="font-size:17px;font-weight:700;margin:28px 0 4px;color:#111827;">📋 Active in the Last 30 Days</h2>
  <p style="font-size:12px;color:#6b7280;margin:0 0 14px;">Open listings from the past 30 days (previously reported)</p>

  {_section_html("🚑 EMT / Clinical Roles", emt_active, seen_ids)}
  {_section_html("🚑 Paramedic Roles", para_active, seen_ids)}
  {_section_html("🩹 Medical Assistant Roles — CMA Required", ma_req_active, seen_ids)}
  {_section_html("🩹 Medical Assistant Roles — CMA Preferred / Not Required", ma_pref_active, seen_ids)}
  {_section_html("🩺 Physician Assistant Roles", pa_active, seen_ids)}

  <!-- Footer -->
  <div style="border-top:1px solid #e5e7eb;margin-top:32px;padding-top:14px;font-size:11px;color:#9ca3af;text-align:center;">
    <p style="margin:0;">Searches within 50 miles of Palisade, CO · In-person roles only</p>
    <p style="margin:4px 0 0;">Runs automatically every 2 days via GitHub Actions</p>
  </div>

</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Email sending
# ─────────────────────────────────────────────────────────────────────────────

def send_email(html_body: str) -> bool:
    """Send the digest via Gmail SMTP using the app password from the environment."""
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not password:
        log.error("GMAIL_APP_PASSWORD environment variable is not set")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = EMAIL_SUBJECT
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(EMAIL_RECIPIENTS)

    msg.attach(MIMEText(
        "Job Digest — please view this email in an HTML-capable mail client.",
        "plain",
    ))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_SENDER, password)
            smtp.sendmail(EMAIL_SENDER, EMAIL_RECIPIENTS, msg.as_string())
        log.info(f"Email sent → {EMAIL_RECIPIENTS}")
        return True
    except Exception as exc:
        log.error(f"Email send failed: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("═══ Job Digest Agent — starting ═══")

    state    = load_state()
    seen_ids: Set[str] = set(state.get("seen_job_ids", []))
    log.info(f"Loaded {len(seen_ids)} previously seen job IDs")

    raw: List[Job] = []
    raw.extend(search_job_boards())
    raw.extend(scrape_employers())
    log.info(f"Total raw listings: {len(raw)}")

    classified: List[Job] = []
    for j in raw:
        cat = classify(j.title, j.description, j.employment_type)
        if cat:
            j.category = cat
            classified.append(j)
    log.info(f"After classification: {len(classified)}")

    in_range = [j for j in classified if j.within_range()]
    log.info(f"Within {MAX_DISTANCE_MILES} miles: {len(in_range)}")

    recent = [j for j in in_range if j.posted_within_days(DAYS_TO_SEARCH)]
    log.info(f"Posted within {DAYS_TO_SEARCH} days: {len(recent)}")

    seen_this_run: Set[str] = set()
    unique: List[Job] = []
    for j in recent:
        if j.job_id not in seen_this_run:
            seen_this_run.add(j.job_id)
            unique.append(j)
    log.info(f"Unique listings: {len(unique)}")

    if not unique:
        log.info("No listings to report — skipping email")
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    def sort_key(j: Job):
        return j.posted_date or datetime.min.replace(tzinfo=timezone.utc)

    new_jobs    = sorted([j for j in unique if j.job_id not in seen_ids], key=sort_key, reverse=True)
    active_jobs = sorted([j for j in unique if j.job_id     in seen_ids], key=sort_key, reverse=True)
    log.info(f"New: {len(new_jobs)}  |  Previously active: {len(active_jobs)}")

    html_body = build_email_html(new_jobs, active_jobs, seen_ids)

    if send_email(html_body):
        state["seen_job_ids"] = list(seen_ids | {j.job_id for j in unique})
        state["last_run"]     = datetime.now(timezone.utc).isoformat()
        save_state(state)
    else:
        log.error("Email send failed — state not updated")
        sys.exit(1)

    log.info("═══ Job Digest Agent — done ═══")


if __name__ == "__main__":
    main()
