from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from __init__ import scrape_jobs

from matcher import (
    AVAILABLE_SITES,
    DEFAULT_SITES,
    convert_pdf_to_txt,
    deduplicate_jobs,
    extract_resume_skills,
    filter_jobs_for_experience,
    filter_jobs_for_search_term,
    infer_search_term,
    is_intern_role,
    parse_float,
    parse_int,
    rank_jobs,
)

app = FastAPI(title="look4job")

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
UPLOAD_DIR = RUNTIME_DIR / "uploaded_pdf"
CONVERTED_DIR = RUNTIME_DIR / "converted_txt"
SITE_OPTIONS = ["indeed", "linkedin", "zip_recruiter", "glassdoor", "google"]
JOB_TYPE_OPTIONS = ["regular", "intern", "both"]

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def normalize_selected_sites(selected_sites: list[str] | None) -> list[str]:
    if not selected_sites:
        return DEFAULT_SITES
    normalized = [site.strip().lower() for site in selected_sites if site.strip()]
    valid = [site for site in normalized if site in AVAILABLE_SITES]
    return valid or DEFAULT_SITES


def save_uploaded_pdf(file: UploadFile) -> Path:
    filename = Path(file.filename or "resume.pdf").name
    safe_name = filename.replace(" ", "_")
    upload_path = UPLOAD_DIR / safe_name
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with upload_path.open("wb") as f:
        f.write(file.file.read())
    return upload_path


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "defaults": {
                "search_term": "software engineer python fastapi",
                "location": "Remote",
                "selected_sites": DEFAULT_SITES,
                "site_options": SITE_OPTIONS,
                "results_wanted": 30,
                "hours_old": 168,
                "expected_experience": 2,
                "job_type": "regular",
                "job_type_options": JOB_TYPE_OPTIONS,
            },
            "errors": [],
        },
    )


@app.post("/match", response_class=HTMLResponse)
def match_jobs(
    request: Request,
    resume_file: Annotated[UploadFile | None, File()] = None,
    search_term: str = Form(""),
    location: str = Form("Remote"),
    sites: Annotated[list[str] | None, Form()] = None,
    results_wanted: str = Form("30"),
    hours_old: str = Form("168"),
    expected_experience: str = Form("2"),
    job_type: str = Form("regular"),
) -> HTMLResponse:
    errors: list[str] = []

    if resume_file is None or not resume_file.filename:
        errors.append("Please upload a resume PDF file.")
    elif not resume_file.filename.lower().endswith(".pdf"):
        errors.append("Resume must be a .pdf file.")

    selected_sites = normalize_selected_sites(sites)
    selected_job_type = job_type.strip().lower() if job_type else "regular"
    if selected_job_type not in JOB_TYPE_OPTIONS:
        selected_job_type = "regular"

    wanted = parse_int(results_wanted, fallback=30)
    max_age = parse_int(hours_old, fallback=168)
    expected_exp_years = parse_float(expected_experience, fallback=2.0, minimum=0.0)

    defaults = {
        "search_term": search_term,
        "location": location,
        "selected_sites": selected_sites,
        "site_options": SITE_OPTIONS,
        "results_wanted": wanted,
        "hours_old": max_age,
        "expected_experience": expected_exp_years,
        "job_type": selected_job_type,
        "job_type_options": JOB_TYPE_OPTIONS,
    }

    if errors:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "defaults": defaults,
                "errors": errors,
            },
            status_code=400,
        )

    try:
        uploaded_pdf_path = save_uploaded_pdf(resume_file)
        converted_txt_path, resume_text = convert_pdf_to_txt(
            uploaded_pdf_path, CONVERTED_DIR
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "defaults": defaults,
                "errors": [f"Could not process uploaded PDF: {exc}"],
            },
            status_code=400,
        )

    if not resume_text.strip():
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "defaults": defaults,
                "errors": ["Converted TXT is empty. Check your PDF content."],
            },
            status_code=400,
        )

    skills = extract_resume_skills(resume_text)
    suggested = infer_search_term(resume_text, skills)
    final_search = search_term.strip() or suggested
    final_location = location.strip() or "Remote"

    try:
        jobs = scrape_jobs(
            site_name=selected_sites,
            search_term=final_search,
            location=final_location,
            results_wanted=wanted,
            hours_old=max_age,
            country_indeed="USA",
            description_format="plain",
            verbose=0,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "defaults": defaults,
                "errors": [f"Job scraping failed: {exc}"],
            },
            status_code=500,
        )

    filtered_jobs = deduplicate_jobs(
        filter_jobs_for_experience(
            filter_jobs_for_search_term(jobs, final_search),
            expected_exp_years,
        )
    )
    ranked = (
        rank_jobs(filtered_jobs, skills, expected_exp_years)
        if not filtered_jobs.empty
        else filtered_jobs
    )

    columns = [
        "site",
        "title",
        "company",
        "location",
        "date_posted",
        "match_score",
        "experience_fit",
        "matched_skills",
        "job_url",
    ]
    rows = []
    intern_rows = []
    if not ranked.empty:
        available = [c for c in columns if c in ranked.columns]
        visible = ranked[available].fillna("")
        intern_mask = visible["title"].astype(str).apply(is_intern_role)
        regular_visible = visible[~intern_mask]
        intern_visible = visible[intern_mask]

        if selected_job_type == "intern":
            intern_rows = intern_visible.head(25).to_dict(orient="records")
        elif selected_job_type == "both":
            rows = regular_visible.head(25).to_dict(orient="records")
            intern_rows = intern_visible.head(25).to_dict(orient="records")
        else:
            rows = regular_visible.head(25).to_dict(orient="records")

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "errors": errors,
            "summary": {
                "search_term": final_search,
                "location": final_location,
                "sites": ", ".join(selected_sites),
                "skills": ", ".join(skills) if skills else "None",
                "expected_experience": f"{expected_exp_years:g} years",
                "job_type": selected_job_type.title(),
                "jobs_found": len(filtered_jobs),
                "regular_jobs_found": len(rows),
                "intern_jobs_found": len(intern_rows),
            },
            "rows": rows,
            "intern_rows": intern_rows,
        },
    )




