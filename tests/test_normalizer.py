"""Normalizer tests on saved real records (+ a few synthetic edge cases). No internet needed."""
import json
from pathlib import Path

import pytest

from app.processing.normalizer import (
    clean_drug_name, clean_sponsor, months_between, normalize_all, phase_label,
)

STUDIES = json.loads((Path(__file__).parent / "fixtures" / "studies_sample.json").read_text())


@pytest.fixture(scope="module")
def result():
    return normalize_all(STUDIES, needed={"start_year", "phase", "enrollment"})


def by_id(trials, nct_id):
    return next(t for t in trials if t.nct_id == nct_id)


# --- helpers -------------------------------------------------------------

@pytest.mark.parametrize("phases, label", [
    (["PHASE2"], "PHASE2"),
    (["PHASE2", "PHASE1"], "PHASE1/PHASE2"),   # order fixed, counted once
    ([], "NA"),
    (["NA"], "NA"),
    (["EARLY_PHASE1"], "EARLY_PHASE1"),
])
def test_phase_label(phases, label):
    assert phase_label(phases) == label


@pytest.mark.parametrize("raw, clean", [
    ("Pembrolizumab 200 mg IV", "Pembrolizumab"),
    ("Pembrolizumab (MK-3475)", "Pembrolizumab"),
    ("Cisplatin 75 mg/m2 on day 1", "Cisplatin"),
    ("5-Fluorouracil", "5-Fluorouracil"),
    ("IL-2", "IL-2"),
])
def test_clean_drug_name(raw, clean):
    assert clean_drug_name(raw) == clean


@pytest.mark.parametrize("raw, clean", [
    ("Merck Sharp & Dohme LLC", "Merck Sharp & Dohme"),
    ("Merck Sharp & Dohme Corp.", "Merck Sharp & Dohme"),
    ("Pfizer Inc.", "Pfizer"),
    ("Eli Lilly and Company", "Eli Lilly"),
    ("Hoffmann-La Roche", "Hoffmann-La Roche"),
    ("National Cancer Institute (NCI)", "National Cancer Institute (NCI)"),
])
def test_clean_sponsor(raw, clean):
    assert clean_sponsor(raw) == clean


def test_months_between():
    assert months_between("2015-03", "2017-09") == 30.0
    assert months_between("2015-03-01", "2015-02-01") is None   # end before start
    assert months_between("2015", None) is None


# --- full records ----------------------------------------------------------

def test_report_counts_skips_and_missing(result):
    trials, report = result
    assert report.total == 7 and report.kept == 5
    assert report.skipped["duplicate NCT ID"] == 1
    assert sum(report.skipped.values()) == 2          # duplicate + broken record
    assert report.missing["enrollment"] == 4          # only one record has a valid number
    assert report.missing["start_year"] == 1


def test_combined_entry_does_not_create_a_fake_drug(result):
    trial = by_id(result[0], "NCT05185739")
    assert trial.drug_keys == {"pembrolizumab", "lenvatinib"}


def test_combo_only_name_is_split(result):
    trial = by_id(result[0], "NCT02799095")
    assert trial.drug_keys == {"alks 4230", "pembrolizumab"}
    assert trial.phase == "PHASE1/PHASE2"


def test_non_drug_interventions_left_out(result):
    trial = by_id(result[0], "NCT02880228")
    assert trial.drug_keys == {"dexamethasone", "lenalidomide", "pembrolizumab"}


def test_synthetic_record(result):
    t = by_id(result[0], "NCT09999901")
    assert t.drug_keys == {"pembrolizumab"}               # dose/parens stripped, placebo excluded
    assert t.interventions[0].name == "Pembrolizumab 200 mg IV (MK-3475)"  # raw kept for citations
    assert t.lead_sponsor_display == "Merck Sharp & Dohme"
    assert t.lead_sponsor == "Merck Sharp & Dohme LLC"
    assert t.start_year == 2015 and t.duration_months == 30.0
    assert t.enrollment == 305
    assert t.countries == {"United States", "Germany"}
    assert len(t.sites) == 2                               # duplicates and country-less sites dropped


def test_messy_record_does_not_crash(result):
    t = by_id(result[0], "NCT09999902")
    assert t.phase == "NA" and t.start_year is None and t.enrollment is None