from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pandas as pd

AVAILABLE_SITES = {"indeed", "linkedin", "zip_recruiter", "glassdoor", "google"}
DEFAULT_SITES = ["indeed", "linkedin", "zip_recruiter"]

SKILL_KEYWORDS = {
    "python",
    "java",
    "javascript",
    "typescript",
    "sql",
    "aws",
    "azure",
    "gcp",
    "docker",
    "kubernetes",
    "react",
    "node",
    "django",
    "flask",
    "fastapi",
    "pandas",
    "numpy",
    "machine learning",
    "data analysis",
    "tensorflow",
    "pytorch",
    "spark",
    "hadoop",
    "git",
    "linux",
    "rest api",
    "microservices",
}

TITLE_HINTS = [
    "software engineer",
    "data analyst",
    "data scientist",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "devops engineer",
    "machine learning engineer",
    "python developer",
]

ROLE_KEYWORDS = {
    "engineer",
    "developer",
    "scientist",
    "analyst",
    "architect",
    "manager",
    "designer",
}

NEGATIVE_TITLE_TERMS = {
    "sales",
    "account executive",
    "business development",
    "recruiter",
    "marketing",
    "customer success",
    "loan officer",
    "insurance agent",
}

QUERY_TITLE_RULES = {
    "software engineer": [
        "software",
        "developer",
        "backend",
        "frontend",
        "full stack",
        "fullstack",
        "application",
        "platform",
    ],
    "data scientist": ["data", "scientist", "machine learning", "ml", "ai"],
    "data analyst": ["data", "analyst", "analytics", "bi"],
    "machine learning engineer": ["machine learning", "ml", "ai", "engineer"],
}

MAX_EXPERIENCE_GAP_YEARS = 3.0
HARD_REJECT_GAP_YEARS = 5.0
SENIORITY_TITLE_RULES = {
    "intern": {"max_years": 1.0, "score_delta": -1, "label": "Entry-level"},
    "junior": {"max_years": 2.0, "score_delta": 1, "label": "Entry-level"},
    "entry level": {"max_years": 2.0, "score_delta": 1, "label": "Entry-level"},
    "trainee": {"max_years": 2.0, "score_delta": 1, "label": "Entry-level"},
    "associate": {"max_years": 3.0, "score_delta": 0, "label": "Early-career"},
    "mid level": {"max_years": 5.0, "score_delta": -1, "label": "Mid-level"},
    "senior": {"min_years": 4.0, "score_delta": -4, "label": "Senior-title"},
    "lead": {"min_years": 6.0, "score_delta": -5, "label": "Lead-title"},
    "principal": {"min_years": 8.0, "score_delta": -6, "label": "Principal-title"},
    "staff": {"min_years": 8.0, "score_delta": -6, "label": "Staff-title"},
    "architect": {"min_years": 8.0, "score_delta": -6, "label": "Architect-title"},
    "manager": {"min_years": 6.0, "score_delta": -5, "label": "Manager-title"},
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def normalize_title_seniority(title: str) -> str:
    normalized_title = normalize(title)
    replacements = {
        r"\bsr\.?\b": "senior",
        r"\bjr\.?\b": "junior",
        r"\bmgr\.?\b": "manager",
    }
    for pattern, replacement in replacements.items():
        normalized_title = re.sub(pattern, replacement, normalized_title)
    return normalized_title




def is_intern_role(title: str) -> bool:
    normalized_title = normalize_title_seniority(title)
    return "intern" in normalized_title


def convert_pdf_to_txt(pdf_path: Path, output_dir: Path) -> tuple[Path, str]:
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Resume must be a .pdf file")

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Install pypdf for PDF support: pip install pypdf") from exc

    reader = PdfReader(str(pdf_path))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(pages).strip()

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{pdf_path.stem}.txt"
    output_path.write_text(text, encoding="utf-8")

    return output_path, text


def extract_resume_skills(resume_text: str) -> list[str]:
    text = normalize(resume_text)
    skills = [skill for skill in SKILL_KEYWORDS if skill in text]
    return sorted(skills)


def infer_search_term(resume_text: str, skills: list[str]) -> str:
    text = normalize(resume_text)
    for title in TITLE_HINTS:
        if title in text:
            return title
    if skills:
        return f"software engineer {' '.join(skills[:3])}"
    return "software engineer"


def parse_int(value: str, fallback: int, minimum: int = 1) -> int:
    if not value or not value.strip().isdigit():
        return fallback
    return max(int(value.strip()), minimum)


def parse_float(value: str, fallback: float, minimum: float = 0.0) -> float:
    if not value:
        return fallback
    try:
        parsed = float(value.strip())
    except ValueError:
        return fallback
    return max(parsed, minimum)


def deduplicate_jobs(jobs_df: pd.DataFrame) -> pd.DataFrame:
    if jobs_df.empty:
        return jobs_df

    ranked = jobs_df.copy()
    for col in ["title", "company", "location", "job_url"]:
        if col not in ranked.columns:
            ranked[col] = ""
        ranked[col] = ranked[col].fillna("").astype(str)

    ranked["_dedupe_key"] = (
        ranked["title"].str.lower().str.strip()
        + "|"
        + ranked["company"].str.lower().str.strip()
        + "|"
        + ranked["location"].str.lower().str.strip()
    )

    ranked = ranked.drop_duplicates(subset=["_dedupe_key"], keep="first")
    return ranked.drop(columns=["_dedupe_key"]).reset_index(drop=True)


def is_title_relevant(title: str, search_term: str) -> bool:
    normalized_title = normalize(title)
    normalized_query = normalize(search_term)

    if not normalized_query:
        return True

    for blocked_term in NEGATIVE_TITLE_TERMS:
        if blocked_term in normalized_title and blocked_term not in normalized_query:
            return False

    for query_pattern, required_fragments in QUERY_TITLE_RULES.items():
        if query_pattern in normalized_query:
            return any(fragment in normalized_title for fragment in required_fragments)

    query_role_terms = [role for role in ROLE_KEYWORDS if role in normalized_query]
    if query_role_terms and not any(role in normalized_title for role in query_role_terms):
        return False

    return True


def filter_jobs_for_search_term(jobs_df: pd.DataFrame, search_term: str) -> pd.DataFrame:
    if jobs_df.empty or "title" not in jobs_df.columns:
        return jobs_df

    filtered = jobs_df[
        jobs_df["title"].fillna("").astype(str).apply(
            lambda title: is_title_relevant(title, search_term)
        )
    ]
    return filtered.reset_index(drop=True)


def extract_required_experience_years(text: str) -> tuple[float | None, float | None]:
    normalized = normalize(text)

    range_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:\+?\s*)?(?:years|year|yrs|yr)",
        normalized,
    )
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))

    single_match = re.search(
        r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years|year|yrs|yr)", normalized
    )
    if single_match:
        years = float(single_match.group(1))
        return years, None

    return None, None


def is_experience_reasonable(text: str, expected_experience_years: float) -> bool:
    exp_min, _ = extract_required_experience_years(text)
    if exp_min is None:
        return True
    return (exp_min - expected_experience_years) < HARD_REJECT_GAP_YEARS


def filter_jobs_for_experience(
    jobs_df: pd.DataFrame, expected_experience_years: float
) -> pd.DataFrame:
    if jobs_df.empty:
        return jobs_df

    def row_ok(row: pd.Series) -> bool:
        searchable = " ".join(
            [
                str(row.get("title", "") or ""),
                str(row.get("description", "") or ""),
                str(row.get("job_function", "") or ""),
                str(row.get("job_level", "") or ""),
                str(row.get("company_industry", "") or ""),
                str(row.get("experience_range", "") or ""),
            ]
        )
        return is_experience_reasonable(searchable, expected_experience_years)

    filtered = jobs_df[jobs_df.apply(row_ok, axis=1)]
    return filtered.reset_index(drop=True)


def score_job_against_resume(
    job_row: pd.Series,
    resume_skills: Iterable[str],
    expected_experience_years: float,
) -> tuple[int, list[str], str]:
    searchable = " ".join(
        [
            str(job_row.get("title", "") or ""),
            str(job_row.get("description", "") or ""),
            str(job_row.get("job_function", "") or ""),
            str(job_row.get("job_level", "") or ""),
            str(job_row.get("company_industry", "") or ""),
            str(job_row.get("experience_range", "") or ""),
        ]
    ).lower()

    matched = [skill for skill in resume_skills if skill in searchable]
    score = len(matched)

    title = normalize_title_seniority(str(job_row.get("title", "") or ""))

    exp_min, exp_max = extract_required_experience_years(searchable)
    exp_fit = "Unknown"

    if exp_min is not None and exp_max is not None:
        if exp_min <= expected_experience_years <= exp_max:
            score += 3
            exp_fit = f"Good fit ({exp_min:g}-{exp_max:g} yrs)"
        elif expected_experience_years < exp_min:
            gap = exp_min - expected_experience_years
            if gap <= MAX_EXPERIENCE_GAP_YEARS:
                score -= 2
                exp_fit = f"Stretch ({exp_min:g}-{exp_max:g} yrs)"
            else:
                score -= 5
                exp_fit = f"Senior-heavy ({exp_min:g}-{exp_max:g} yrs)"
        else:
            score += 1
            exp_fit = f"Above range ({exp_min:g}-{exp_max:g} yrs)"
    elif exp_min is not None:
        gap = exp_min - expected_experience_years
        if gap <= 0:
            score += 3
            exp_fit = f"Good fit ({exp_min:g}+ yrs)"
        elif gap <= MAX_EXPERIENCE_GAP_YEARS:
            score -= 2
            exp_fit = f"Stretch ({exp_min:g}+ yrs)"
        else:
            score -= 5
            exp_fit = f"Senior-heavy ({exp_min:g}+ yrs)"

    seniority_fit = None
    for keyword, rule in SENIORITY_TITLE_RULES.items():
        if keyword not in title:
            continue

        min_years = rule.get("min_years")
        max_years = rule.get("max_years")

        if min_years is not None and expected_experience_years < min_years:
            score += int(rule["score_delta"])
            if exp_fit == "Unknown":
                seniority_fit = f"{rule['label']} ({min_years:g}+ yrs signal)"
        elif max_years is not None and expected_experience_years <= max_years:
            score += int(rule["score_delta"])
            if exp_fit == "Unknown":
                seniority_fit = f"{rule['label']} ({max_years:g} yrs signal)"

    if seniority_fit is not None:
        exp_fit = seniority_fit

    return score, matched, exp_fit


def rank_jobs(
    jobs_df: pd.DataFrame,
    resume_skills: list[str],
    expected_experience_years: float,
) -> pd.DataFrame:
    if jobs_df.empty:
        return jobs_df

    scores: list[int] = []
    matched_terms: list[str] = []
    experience_fit: list[str] = []

    for _, row in jobs_df.iterrows():
        score, matched, exp_fit = score_job_against_resume(
            row, resume_skills, expected_experience_years
        )
        scores.append(score)
        matched_terms.append(", ".join(matched) if matched else "")
        experience_fit.append(exp_fit)

    ranked = jobs_df.copy()
    ranked["match_score"] = scores
    ranked["matched_skills"] = matched_terms
    ranked["experience_fit"] = experience_fit
    return ranked.sort_values(
        by=["match_score", "date_posted"], ascending=[False, False]
    ).reset_index(drop=True)
