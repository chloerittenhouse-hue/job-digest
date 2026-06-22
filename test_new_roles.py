#!/usr/bin/env python3
"""
Test: Paramedic & Medical Assistant role search additions
==========================================================

Confirms the two newly added roles are picked up by job_digest:

  1. The job-board search queries now include Paramedic and Medical Assistant
     terms.
  2. The classifier routes Paramedic-titled listings to "paramedic", and splits
     Medical-Assistant-titled listings into "ma_cma_required" (national CMA
     certification clearly required) vs "ma" (preferred / optional / not
     required). Medical Assistant listings -- which the OLD code excluded --
     now survive classification.
  3. Precision guards still hold: look-alike titles and online/remote postings
     are rejected.

Then it attempts a small LIVE job-board pull for the two new roles. The live
pull needs outbound internet + python-jobspy; if either is unavailable the test
still passes on the offline checks and the live section is reported as skipped.

Run:  python test_new_roles.py
"""

import sys

import job_digest as jd


def test_queries_present():
    print("\n-- Part 1: Search queries --------------------------------------")
    hints = {hint for _q, hint in jd._JOBSPY_QUERIES}
    all_text = " ".join(q for q, _h in jd._JOBSPY_QUERIES).lower()
    checks = [
        ("paramedic query registered", "paramedic" in hints),
        ("medical-assistant query registered", "ma" in hints),
        ("'paramedic' search term present", "paramedic" in all_text),
        ("'medical assistant' search term present", "medical assistant" in all_text),
    ]
    ok = True
    for label, passed in checks:
        print(f"   [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    return ok


# (title, description, expected_category)
SAMPLES = [
    # Paramedic -> "paramedic"
    ("Paramedic", "Full-time paramedic for 911 response.", "paramedic"),
    ("Flight Paramedic", "Critical care transport via rotor-wing.", "paramedic"),
    ("Community Paramedic - Grand Junction", "Mobile integrated healthcare.", "paramedic"),
    ("EMT-P / Paramedic", "Ground ambulance, ALS.", "paramedic"),
    ("Critical Care Paramedic (CCP)", "Interfacility CCT team.", "paramedic"),

    # Medical Assistant, national CMA certification REQUIRED -> "ma_cma_required"
    ("Certified Medical Assistant (CMA)", "National CMA certification required.", "ma_cma_required"),
    ("Medical Assistant", "Must be a nationally certified medical assistant.", "ma_cma_required"),
    ("Medical Assistant - Cardiology", "CMA required; rooming, vitals, EHR.", "ma_cma_required"),

    # Medical Assistant, certification PREFERRED / OPTIONAL -> "ma"
    ("Medical Assistant", "CMA preferred but not required; willing to train.", "ma"),
    ("Medical Assistant - Family Practice", "Certification preferred. Outpatient clinic.", "ma"),
    ("Registered Medical Assistant", "Front and back office duties.", "ma"),
    ("Clinical Medical Assistant", "Certification not required for this role.", "ma"),

    # Existing categories still work
    ("ER Tech", "Emergency department technician, EMT preferred.", "emt_clinical"),
    ("Physician Assistant - Orthopedics", "PA-C, surgical first assist.", "pa"),

    # Precision guards -- should NOT match the new roles
    ("Pharmacy Technician", "Retail pharmacy; no clinical cert.", None),
    ("HVAC Technician", "Building maintenance.", None),
    ("Medical Assistant", "Fully remote telehealth coordinator.", None),
    ("Online Medical Assistant", "Apply online for this position.", None),
    ("Virtual Paramedic - Telehealth Triage", "Remote triage.", None),
    ("Personal Trainer", "Gym fitness coach.", None),
]


def test_classification():
    print("\n-- Part 2: Classification of sample listings -------------------")
    print(f"   {'TITLE':<42}{'EXPECTED':<18}{'GOT':<18}RESULT")
    print(f"   {'-'*42}{'-'*18}{'-'*18}------")
    ok = True
    for title, desc, expected in SAMPLES:
        got = jd.classify(title, desc)
        passed = (got == expected)
        ok = ok and passed
        print(f"   {title[:40]:<42}{str(expected):<18}{str(got):<18}{'PASS' if passed else 'FAIL'}")
    return ok


def test_live_sample(limit=5):
    print("\n-- Part 3: Live sample pull (Paramedic & Medical Assistant) ----")
    if not jd.JOBSPY_AVAILABLE:
        print("   [SKIP] python-jobspy not installed -- live pull unavailable.")
        return None
    new_role_queries = [(q, h) for q, h in jd._JOBSPY_QUERIES if h in ("paramedic", "ma")]
    found_total = 0
    try:
        for query, hint in new_role_queries:
            print(f"\n   > {hint.upper()} query: {query[:60]}")
            df = jd.jobspy_scrape(
                site_name=["indeed"], search_term=query, location=jd.SEARCH_CENTER,
                distance=60, results_wanted=limit, hours_old=jd.DAYS_TO_SEARCH * 24,
                country_indeed="USA",
            )
            if df is None or df.empty:
                print("     (no rows returned)")
                continue
            for _, row in df.head(limit).iterrows():
                title = str(row.get("title") or "")
                cat = jd.classify(title, str(row.get("description") or ""))
                found_total += 1
                print(f"     - {title[:55]:<57} @ {str(row.get('company') or '')[:25]:<27} -> {cat}")
    except Exception as exc:
        print(f"   [SKIP] Live pull could not complete (likely no network): {exc}")
        return None
    print(f"\n   Live rows pulled: {found_total}")
    return found_total > 0


def main():
    print("=" * 70)
    print(" Job Digest -- Paramedic & Medical Assistant role test")
    print("=" * 70)
    q_ok = test_queries_present()
    c_ok = test_classification()
    live = test_live_sample()
    print("\n-- Summary -----------------------------------------------------")
    print(f"   Search queries include new roles : {'PASS' if q_ok else 'FAIL'}")
    print(f"   Classification routing correct   : {'PASS' if c_ok else 'FAIL'}")
    print(f"   Live sample pull                 : "
          f"{'PASS' if live else ('SKIPPED' if live is None else 'NO ROWS')}")
    offline_ok = q_ok and c_ok
    print("\n" + ("OFFLINE TESTS PASSED" if offline_ok else "OFFLINE TESTS FAILED"))
    print("   (Live pull is informational; it needs internet access to run.)")
    sys.exit(0 if offline_ok else 1)


if __name__ == "__main__":
    main()
