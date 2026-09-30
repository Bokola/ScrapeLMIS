"""Shared fuzzy product/category matching for program-based (e.g. HIV/ART)
extractions, used by both country pipelines (main.py for Mozambique,
malawi_main.py for Malawi).

A master product/category list (e.g. data/LMIS_HIV_category.xlsx) can be
used both to (a) restrict an extract to only listed products and (b)
populate a Category (and, for Mozambique, Product Short) for them - see
filters.product_category_file in each country's config.

This exists because a country's raw HIV product names and the master
list's names use genuinely different conventions (Portuguese vs English
spelling, "+" vs "/" separators, different dosage formatting) - confirmed
via real Mozambique examples, e.g. SIMAM's
"Abacavir+Lamivudina+Dolutegravir; 90 Comp; 60mg+30mg+5mg; Comp" vs the
master list's "Abacavir/Lamivudine/Dolutegravir (ABC/3TC/DTG)" - so exact
matching (as used for Mozambique's 6 family-planning products) would match
zero HIV rows.

THIS IS APPROXIMATE, not exact-match-verified - matches active-ingredient
name STEMS shared between the two strings, scored by Jaccard similarity
(overlap / union), and requires a clear margin over the next-best
candidate before accepting a match. Verified against a real 47-item unique
product list from a Mozambique HIV extract (44/47 matched correctly after
tuning; the remaining 3 were genuine gaps in the master list, not matching
errors) - but this has NOT been validated against Malawi's own HIV product
naming at all yet. Spot-check real output against this before trusting it
for anything critical, especially for a new country/dataset.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .logger import get_logger

log = get_logger(__name__)

_PRODUCT_MATCH_STOPWORDS = {
    "mg", "ml", "comp", "tablets", "tablet", "kit", "long", "acting",
    "vaginal", "ring", "anel", "dvr", "de", "hiv",
    # Portuguese/English packaging, dosage-form, and other non-ingredient
    # words - confirmed necessary via real Mozambique product names, where
    # these were being counted as if they were active ingredients and
    # diluting scores below threshold for otherwise-correct matches.
    "comprimidos", "embalagem", "susp", "disp", "baby", "sulfato", "gran",
    "orais", "caps", "cps", "frascos", "sol", "papel",
    # More of the same, confirmed necessary via real Malawi product names -
    # same failure mode (e.g. "Nevirapine ... bottle of 100ml oral
    # suspension" scored 0.25 instead of a clean 1.0 before these were
    # added; "Tenofovir Disoproxil Fumarate/Lamivudine..." fell just short
    # of the margin threshold because "Disoproxil"/"Fumarate" - the salt/
    # prodrug qualifier for Tenofovir, not a separate ingredient - were
    # counted as 2 extra unmatched tokens).
    "bottle", "oral", "suspension", "dispersible", "pack", "disoproxil",
    "fumarate",
}

# Common ARV abbreviations, mapped directly to the same canonical stem as
# their full ingredient name. CONFIRMED NECESSARY: several master-list
# entries are phrased using ONLY these abbreviations (e.g. "TDF/3TC/DTG
# 300/300/50 mg – 30 tablets"), and since they're all 3 letters, the
# regular tokenizer's 4-letter minimum drops every one of them - leaving
# such an entry with ZERO extractable stems, so it could never be matched
# no matter what.
_ARV_ABBREVIATION_TO_STEM = {
    "tdf": "tenofovir",
    "3tc": "lamivud",
    "dtg": "dolutegravir",
    "efv": "efavirenz",
    "abc": "abacavir",
    "azt": "zidovud",
    "nvp": "nevirap",
    "lpv": "lopinavir",
    "ftc": "emtricitab",
    "cab": "cabotegravir",
    "atv": "atazanavir",
    "drv": "darunavir",
    "rtv": "ritonavir",
}


def _tokenize_product_name(name: str) -> set[str]:
    """Extract candidate active-ingredient tokens from a product name:
    alphabetic runs of 3+ letters. A recognized ARV abbreviation (see
    _ARV_ABBREVIATION_TO_STEM) is mapped directly to its ingredient's
    stem; an unrecognized short all-caps run is dropped as noise (assumed
    to duplicate a full name already present elsewhere, or be otherwise
    not a drug name); common non-drug words (dosage units, dosage forms,
    packaging terms) are dropped via _PRODUCT_MATCH_STOPWORDS.
    """
    raw_tokens = re.findall(r"[A-Za-zÀ-ÿ]{3,}", name)
    tokens = set()
    for t in raw_tokens:
        tl = t.lower()
        if tl in _ARV_ABBREVIATION_TO_STEM:
            tokens.add(_ARV_ABBREVIATION_TO_STEM[tl])
            continue
        if len(t) < 4:
            continue
        if tl in _PRODUCT_MATCH_STOPWORDS:
            continue
        if t.isupper() and len(t) <= 4:
            continue
        tokens.add(tl)
    # "3TC" (lamivudine/lamivudina's standard abbreviation) has a numeric
    # prefix, so the alphabetic-only regex above never captures it as a
    # whole token (only the trailing "TC", 2 letters, below threshold) -
    # checked separately. Confirmed necessary: without this, "3TC" is
    # silently invisible to the tokenizer no matter what.
    if re.search(r"3tc", name, re.IGNORECASE):
        tokens.add("lamivud")
    return tokens


def _stem_product_token(token: str) -> str:
    """Collapse the Portuguese/English INN spelling difference for many HIV
    drug names (e.g. "lamivudina" / "lamivudine" -> "lamivud")."""
    for suffix in ("ina", "ine"):
        if token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _product_stem_set(name: str) -> set[str]:
    return {_stem_product_token(t) for t in _tokenize_product_name(name)}


def load_product_category_list(path: str | Path) -> list[tuple[str, str]]:
    """Load a master product/category list (columns: Product, Category) -
    see data/LMIS_HIV_category.xlsx for the confirmed real example this was
    built against."""
    df = pd.read_excel(path)
    return list(zip(df["Product"].astype(str), df["Category"].astype(str)))


def match_product_category(
    raw_name: str,
    master_list: list[tuple[str, str]],
    min_jaccard: float = 0.5,
    min_margin: float = 0.15,
) -> tuple[str, str] | None:
    """Find the best fuzzy match for raw_name in master_list (see module
    docstring above for the approach and its real, verified accuracy so
    far). Returns (matched_product_name, category), or None if there's no
    confident match - the best score is below min_jaccard, or the
    top-scoring candidates disagree on Category (a genuinely ambiguous
    case, safer left unmatched than guessed).

    Candidates within min_margin of the best score are treated as tied.
    Confirmed necessary via real master-list data: several entries differ
    only by pack size (e.g. "TDF/3TC/DTG 300/300/50 mg" at 30/90/180
    tablets) and score identically for a given real product - the exact
    tablet count can't be determined by ingredient-name matching alone,
    but this doesn't matter for classification purposes as long as every
    tied candidate agrees on Category, in which case that category (and
    the first tied candidate's name) is returned. If the tied candidates
    disagree on Category, that's a genuine ambiguity and this returns None.
    """
    raw_stems = _product_stem_set(raw_name)
    if not raw_stems:
        return None

    scored = []
    for master_name, category in master_list:
        master_stems = _product_stem_set(master_name)
        if not master_stems:
            continue
        overlap = raw_stems & master_stems
        if not overlap:
            continue
        union = raw_stems | master_stems
        scored.append((len(overlap) / len(union), master_name, category))

    if not scored:
        return None
    scored.sort(reverse=True)
    best_score = scored[0][0]
    if best_score < min_jaccard:
        return None

    tied = [s for s in scored if (best_score - s[0]) < min_margin]
    categories = {category for _, _, category in tied}
    if len(categories) > 1:
        return None
    _, best_name, best_category = tied[0]
    return best_name, best_category


def filter_to_known_products(
    df: pd.DataFrame, master_list: list[tuple[str, str]], product_column: str = "Nome do produto"
) -> pd.DataFrame:
    """Keep only rows whose product_column fuzzy-matches something in
    master_list (see match_product_category) - used as a whitelist for
    program-based (e.g. HIV/TARV) extractions, where the site's own
    product filter is deliberately left at its default (unfiltered) state
    (Mozambique) or simply doesn't exist as a site-side option at all
    (Malawi), and this is the only product-level restriction applied.
    Matches are cached per unique product name, since a real extract has
    vastly fewer distinct product names than rows.
    """
    unique_names = df[product_column].unique()
    match_cache = {name: match_product_category(name, master_list) for name in unique_names}
    kept_names = {name for name, m in match_cache.items() if m is not None}
    dropped_names = {name for name, m in match_cache.items() if m is None}

    before = len(df)
    filtered = df[df[product_column].isin(kept_names)].reset_index(drop=True)
    log.info(
        "Filtered to master product list: %d row(s) -> %d row(s) kept "
        "(%d of %d distinct product name(s) matched)",
        before, len(filtered), len(kept_names), len(unique_names),
    )
    if dropped_names:
        log.info(
            "Dropped %d distinct product name(s) with no confident match: %s",
            len(dropped_names), sorted(dropped_names),
        )
    return filtered
