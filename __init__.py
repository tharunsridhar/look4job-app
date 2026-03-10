from __future__ import annotations

import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Tuple

import pandas as pd

from bayt import BaytScraper
from bdjobs import BDJobs
from glassdoor import Glassdoor
from indeed import Indeed
from linkedin import LinkedIn
from naukri import Naukri
from model import Country, JobResponse, Location, SalarySource, ScraperInput, Site
from util import (
    convert_to_annual,
    create_logger,
    desired_order,
    extract_salary,
    get_enum_from_value,
    map_str_to_site,
    set_logger_level,
)
from ziprecruiter import ZipRecruiter

BASE_DIR = Path(__file__).resolve().parent


def _load_local_google_class():
    google_dir = BASE_DIR / 'google'
    spec = importlib.util.spec_from_file_location(
        'look4job_google',
        google_dir / '__init__.py',
        submodule_search_locations=[str(google_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError('Could not load local google scraper package')

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Google


Google = _load_local_google_class()


def scrape_jobs(
    site_name: str | list[str] | Site | list[Site] | None = None,
    search_term: str | None = None,
    google_search_term: str | None = None,
    location: str | None = None,
    distance: int | None = 50,
    is_remote: bool = False,
    job_type: str | None = None,
    easy_apply: bool | None = None,
    results_wanted: int = 15,
    country_indeed: str = 'usa',
    proxies: list[str] | str | None = None,
    ca_cert: str | None = None,
    description_format: str = 'markdown',
    linkedin_fetch_description: bool | None = False,
    linkedin_company_ids: list[int] | None = None,
    offset: int | None = 0,
    hours_old: int = None,
    enforce_annual_salary: bool = False,
    verbose: int = 2,
    user_agent: str = None,
    **kwargs,
) -> pd.DataFrame:
    """Scrape job data from configured job boards concurrently."""
    scraper_mapping = {
        Site.LINKEDIN: LinkedIn,
        Site.INDEED: Indeed,
        Site.ZIP_RECRUITER: ZipRecruiter,
        Site.GLASSDOOR: Glassdoor,
        Site.GOOGLE: Google,
        Site.BAYT: BaytScraper,
        Site.NAUKRI: Naukri,
        Site.BDJOBS: BDJobs,
    }
    set_logger_level(verbose)
    parsed_job_type = get_enum_from_value(job_type) if job_type else None

    def get_site_type() -> list[Site]:
        site_types = list(Site)
        if isinstance(site_name, str):
            site_types = [map_str_to_site(site_name)]
        elif isinstance(site_name, Site):
            site_types = [site_name]
        elif isinstance(site_name, list):
            site_types = [
                map_str_to_site(site) if isinstance(site, str) else site
                for site in site_name
            ]
        return site_types

    country_enum = Country.from_string(country_indeed)
    scraper_input = ScraperInput(
        site_type=get_site_type(),
        country=country_enum,
        search_term=search_term,
        google_search_term=google_search_term,
        location=location,
        distance=distance,
        is_remote=is_remote,
        job_type=parsed_job_type,
        easy_apply=easy_apply,
        description_format=description_format,
        linkedin_fetch_description=linkedin_fetch_description,
        results_wanted=results_wanted,
        linkedin_company_ids=linkedin_company_ids,
        offset=offset,
        hours_old=hours_old,
    )

    def scrape_site(site: Site) -> Tuple[str, JobResponse]:
        scraper_class = scraper_mapping[site]
        scraper = scraper_class(proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        scraped_data: JobResponse = scraper.scrape(scraper_input)
        cap_name = site.value.capitalize()
        display_name = 'ZipRecruiter' if cap_name == 'Zip_recruiter' else cap_name
        display_name = 'LinkedIn' if cap_name == 'Linkedin' else display_name
        create_logger(display_name).info('finished scraping')
        return site.value, scraped_data

    site_to_jobs_dict: dict[str, JobResponse] = {}

    with ThreadPoolExecutor() as executor:
        future_to_site = {
            executor.submit(scrape_site, site): site for site in scraper_input.site_type
        }
        for future in as_completed(future_to_site):
            site_value, scraped_data = future.result()
            site_to_jobs_dict[site_value] = scraped_data

    jobs_dfs: list[pd.DataFrame] = []
    for site, job_response in site_to_jobs_dict.items():
        for job in job_response.jobs:
            job_data = job.dict()
            job_data['site'] = site
            job_data['company'] = job_data['company_name']
            job_data['job_type'] = (
                ', '.join(item.value[0] for item in job_data['job_type'])
                if job_data['job_type']
                else None
            )
            job_data['emails'] = ', '.join(job_data['emails']) if job_data['emails'] else None
            if job_data['location']:
                job_data['location'] = Location(**job_data['location']).display_location()

            compensation_obj = job_data.get('compensation')
            if compensation_obj and isinstance(compensation_obj, dict):
                job_data['interval'] = (
                    compensation_obj.get('interval').value
                    if compensation_obj.get('interval')
                    else None
                )
                job_data['min_amount'] = compensation_obj.get('min_amount')
                job_data['max_amount'] = compensation_obj.get('max_amount')
                job_data['currency'] = compensation_obj.get('currency', 'USD')
                job_data['salary_source'] = SalarySource.DIRECT_DATA.value
                if enforce_annual_salary and (
                    job_data['interval']
                    and job_data['interval'] != 'yearly'
                    and job_data['min_amount']
                    and job_data['max_amount']
                ):
                    convert_to_annual(job_data)
            elif country_enum == Country.USA:
                (
                    job_data['interval'],
                    job_data['min_amount'],
                    job_data['max_amount'],
                    job_data['currency'],
                ) = extract_salary(
                    job_data['description'],
                    enforce_annual_salary=enforce_annual_salary,
                )
                job_data['salary_source'] = SalarySource.DESCRIPTION.value

            job_data['salary_source'] = (
                job_data['salary_source']
                if 'min_amount' in job_data and job_data['min_amount']
                else None
            )
            job_data['skills'] = ', '.join(job_data['skills']) if job_data['skills'] else None
            job_data['experience_range'] = job_data.get('experience_range')
            job_data['company_rating'] = job_data.get('company_rating')
            job_data['company_reviews_count'] = job_data.get('company_reviews_count')
            job_data['vacancy_count'] = job_data.get('vacancy_count')
            job_data['work_from_home_type'] = job_data.get('work_from_home_type')

            jobs_dfs.append(pd.DataFrame([job_data]))

    if not jobs_dfs:
        return pd.DataFrame()

    filtered_dfs = [df.dropna(axis=1, how='all') for df in jobs_dfs]
    jobs_df = pd.concat(filtered_dfs, ignore_index=True)
    for column in desired_order:
        if column not in jobs_df.columns:
            jobs_df[column] = None

    jobs_df = jobs_df[desired_order]
    return jobs_df.sort_values(by=['site', 'date_posted'], ascending=[True, False]).reset_index(drop=True)


__all__ = ['scrape_jobs', 'BDJobs']
