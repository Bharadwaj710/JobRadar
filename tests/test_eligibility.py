from datetime import date
from pathlib import Path
from typing import Any

from jobradar.pipeline.eligibility import (
    CandidateEligibility,
    EligibilityState,
    candidate_eligibility,
    evaluate_eligibility,
)
from jobradar.pipeline.schemas import JobPosting
from jobradar.profile import load_profile


def candidate(**changes: Any) -> CandidateEligibility:
    values: dict[str, Any] = {
        "graduation_year": 2027,
        "degree": "B.Tech",
        "branch": "Computer Science and Engineering",
        "current_undergraduate": True,
        "professional_experience_years": 0,
        "internship_experience": True,
        "academic_project_experience": True,
        "location": "Visakhapatnam, Andhra Pradesh, India",
    }
    values.update(changes)
    return CandidateEligibility.model_validate(values)


def posting(**changes: Any) -> JobPosting:
    values: dict[str, Any] = {
        "role": "Software Engineer Intern",
        "employment_type": "internship",
        "batch_years": [2027],
        "degrees": ["B.Tech", "BE"],
        "locations": ["Bengaluru, India"],
        "work_mode": "onsite",
    }
    values.update(changes)
    return JobPosting.model_validate(values)


def evidence(sentence: str, section: str = "Minimum requirements") -> dict[str, str]:
    return {"sentence": sentence, "section": section, "source": "page"}


def experience(
    sentence: str,
    minimum: float | None,
    *,
    need: str = "required",
    kind: str = "professional",
    maximum: float | None = None,
) -> dict[str, Any]:
    return {
        "need": need,
        "experience_type": kind,
        "minimum_years": minimum,
        "maximum_years": maximum,
        "evidence": evidence(sentence),
    }


def test_explicit_2027_internship_is_eligible() -> None:
    job = posting(
        requirements={
            "student_requirements": [
                {
                    "kind": "current_student",
                    "required": True,
                    "evidence": evidence("Applicants must be currently pursuing a degree."),
                }
            ]
        }
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.ELIGIBLE
    assert result.blockers == [] and result.unresolved == []


def test_explicit_2027_exclusion_is_not_eligible() -> None:
    result = evaluate_eligibility(posting(batch_years=[2025, 2026]), candidate())
    assert result.state == EligibilityState.NOT_ELIGIBLE
    assert "candidate graduates in 2027" in result.blockers[0]


def test_unknown_graduation_window_needs_verification() -> None:
    result = evaluate_eligibility(posting(batch_years=[]), candidate())
    assert result.state == EligibilityState.NEEDS_VERIFICATION
    assert any("does not confirm whether 2027" in reason for reason in result.unresolved)


def test_completed_degree_restriction_rejects_current_undergraduate() -> None:
    job = posting(
        requirements={
            "general_qualifications": [
                {
                    "qualification": "Applicants must have completed a bachelor's degree.",
                    "need": "required",
                    "evidence": evidence("Applicants must have completed a bachelor's degree."),
                }
            ]
        }
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.NOT_ELIGIBLE
    check = next(check for check in result.checks if check.dimension == "completed_degree")
    assert check.evidence[0].sentence == "Applicants must have completed a bachelor's degree."


def test_zero_to_one_years_accepts_a_fresher() -> None:
    job = posting(
        requirements={
            "experience": [experience("0-1 years of professional experience.", 0, maximum=1)],
            "experience_status": "known",
        }
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.ELIGIBLE
    check = next(check for check in result.checks if check.dimension == "experience")
    assert check.status == "met"


def test_mandatory_two_years_is_not_fresher_eligible() -> None:
    job = posting(
        requirements={
            "experience": [experience("2+ years professional experience required.", 2)],
            "experience_status": "known",
        }
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.NOT_ELIGIBLE
    assert any("2+ years" in reason for reason in result.blockers)


def test_preferred_three_years_is_not_a_hard_failure() -> None:
    job = posting(
        requirements={
            "experience": [
                experience(
                    "3+ years professional experience preferred.",
                    3,
                    need="preferred",
                )
            ]
        }
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.LIKELY_ELIGIBLE
    check = next(check for check in result.checks if check.dimension == "experience")
    assert check.status == "not_met" and not check.critical


def test_internship_experience_is_not_professional_experience() -> None:
    accepted = posting(
        requirements={
            "experience": [
                experience(
                    "Previous internship experience is required.",
                    None,
                    kind="internship",
                )
            ]
        }
    )
    rejected = posting(
        requirements={
            "experience": [experience("1+ years professional experience required.", 1)],
            "experience_status": "known",
        }
    )
    assert evaluate_eligibility(accepted, candidate()).state == EligibilityState.ELIGIBLE
    assert evaluate_eligibility(rejected, candidate()).state == EligibilityState.NOT_ELIGIBLE


def test_conflicting_experience_needs_verification() -> None:
    job = posting(
        requirements={
            "experience": [
                experience("0-1 years professional experience.", 0, maximum=1),
                experience("3+ years professional experience required.", 3),
            ],
            "experience_status": "conflicting",
        }
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.NEEDS_VERIFICATION
    assert "conflicting mandatory experience" in result.unresolved[0]


def test_remote_role_open_to_india_is_eligible() -> None:
    result = evaluate_eligibility(
        posting(work_mode="remote", locations=["Remote - India"]), candidate()
    )
    assert result.state == EligibilityState.ELIGIBLE
    location = next(check for check in result.checks if check.dimension == "location")
    assert location.status == "met"


def test_remote_candidate_with_india_location_preference_is_eligible_for_indian_roles() -> None:
    cand = candidate(location="Remote", location_preferences=["Anywhere in India", "Remote"])
    result = evaluate_eligibility(posting(work_mode="hybrid", locations=["Bengaluru, India"]), cand)
    assert result.state == EligibilityState.ELIGIBLE
    location = next(check for check in result.checks if check.dimension == "location")
    assert location.status == "met"
    assert "in India" in location.reason


def test_remote_role_restricted_outside_india_is_not_eligible() -> None:
    result = evaluate_eligibility(
        posting(work_mode="remote", locations=["Remote - United States only"]), candidate()
    )
    assert result.state == EligibilityState.NOT_ELIGIBLE
    assert "restricted outside India" in result.blockers[0]


def test_immediate_full_time_joining_needs_verification() -> None:
    job = posting(
        employment_type="full_time",
        requirements={
            "general_qualifications": [
                {
                    "qualification": "Candidates must be available to join immediately.",
                    "need": "required",
                    "evidence": evidence("Candidates must be available to join immediately."),
                }
            ]
        },
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.NEEDS_VERIFICATION
    assert any("immediate availability" in reason for reason in result.unresolved)


def test_technical_availability_language_is_not_a_joining_requirement() -> None:
    sentence = "Experience building highly available distributed systems is required."
    job = posting(
        requirements={
            "general_qualifications": [
                {
                    "qualification": sentence,
                    "need": "required",
                    "evidence": evidence(sentence),
                }
            ]
        }
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.ELIGIBLE
    assert all(check.dimension != "joining_date" for check in result.checks)


def test_internship_duration_needs_candidate_confirmation() -> None:
    sentence = "Applicants must commit to a 6 month internship."
    job = posting(
        requirements={
            "general_qualifications": [
                {
                    "qualification": sentence,
                    "need": "required",
                    "evidence": evidence(sentence),
                }
            ]
        }
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.NEEDS_VERIFICATION
    assert any("6-month internship" in reason for reason in result.unresolved)


def test_required_work_authorization_remains_unknown() -> None:
    sentence = "Applicants must be authorized to work in India without sponsorship."
    job = posting(
        requirements={
            "general_qualifications": [
                {
                    "qualification": sentence,
                    "need": "required",
                    "evidence": evidence(sentence),
                }
            ]
        }
    )
    result = evaluate_eligibility(job, candidate())
    assert result.state == EligibilityState.NEEDS_VERIFICATION
    assert any("authorization is unknown" in reason for reason in result.unresolved)


def test_profile_derivation_keeps_internships_separate() -> None:
    profile = load_profile(Path("config/profile.yaml"))
    facts = candidate_eligibility(profile, date(2026, 11, 17))
    assert facts.graduation_year == 2027
    assert facts.current_undergraduate is True
    assert facts.professional_experience_years == 0
    assert facts.internship_experience is True


def test_stripe_regression_stays_needs_verification_without_invented_requirements() -> None:
    stripe = JobPosting.model_validate(
        {
            "company": "Stripe",
            "role": "Software Engineer, Intern",
            "employment_type": "internship",
            "locations": ["Bengaluru, India"],
            "work_mode": "onsite",
            "skills_required": [],
            "requirements": {
                "employer_technologies": [
                    {
                        "skills": ["Ruby", "Java", "JavaScript", "Go", "Scala"],
                        "relation": "all",
                        "evidence": evidence(
                            "We work mostly in Ruby, Java, Javascript, Go, and Scala.",
                            "Minimum requirements",
                        ),
                    }
                ],
                "general_qualifications": [
                    {
                        "qualification": "Ability to learn unfamiliar systems.",
                        "need": "required",
                        "evidence": evidence(
                            "Ability to learn unfamiliar systems.", "Minimum requirements"
                        ),
                    }
                ],
            },
        }
    )
    result = evaluate_eligibility(stripe, candidate())
    assert result.state == EligibilityState.NEEDS_VERIFICATION
    assert stripe.skills_required == []
    assert any(check.dimension == "graduation_year" for check in result.checks)
