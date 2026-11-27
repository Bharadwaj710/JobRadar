# 03. End-to-End Ingestion & Candidate Matching Pipeline
**Evaluation Target:** Spektra Systems: AI-Native Software Engineer - Trainee
**Repository:** [JobRadar](file:///D:/JB/JobRadar)
**Branch:** `feat/j4-career-tracks`

---

## 1. Global Ingestion Pipeline

JobRadar ingests hiring opportunities from diverse, noisy sources into a structured, deduplicated global job inventory:

```
[ATS Career Boards / Telegram / Email / Pages]
                     |
                     v
             ShortLinkExpander
        (Expands t.me, bit.ly, etc.)
                     |
                     v
                Deduplicator
        (Canonical URL & Content Hash)
                     |
                     v
                  Fetcher
         (Robots.txt check, backoff)
                     |
                     v
                 Extractor
  (Rules First -> LLM Fallback via LiteLLM)
                     |
                     v
           Shared Job Inventory
           (SQLite Table: 'job')
```

### Ingestion Phases
1. **Source Adapters:** Supports Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Telegram channels, watched HTML pages, and forwarded emails.
2. **Link Expansion & Normalization:** Expands shortened redirects, strips marketing query parameters (UTM tracking), and normalizes URLs to prevent duplicate scraping.
3. **Deduplication:** Computes deterministic SHA-256 hashes of canonical URLs and job descriptions. Duplicate postings are merged rather than duplicated.
4. **Structured Extraction:**
   - **Rule Engine First:** Uses free, deterministic regex and structural extractors to parse role titles, degrees, locations, and skills.
   - **LLM Fallback:** Calls `gemini-flash-lite-latest` or `groq` only when unstructured prose lacks explicit delimiters, strictly respecting the configured token budget.

---

## 2. Candidate Onboarding & Assessment Pipeline

When a candidate uploads a resume, JobRadar transforms raw document text into an actionable career profile:

```
[Candidate PDF / DOCX Resume]
              |
              v
       extract_resume_content
   (pypdf / docx text & links)
              |
              v
     parse_resume_to_draft
 (Skills, Education, Experience, Projects)
              |
              v
    POST /onboarding/upload
  (Signed Session Cookie Issued)
              |
              v
  Interactive Profile Review
  (/onboarding/review/{profile_id})
              |
              v
POST /onboarding/confirm/{profile_id}
 (Immutable Profile Version 1 Created)
              |
              v
 CandidateMatchingService
  (4-Track J4 Evaluations Generated)
              |
              v
Candidate Dashboard & Match Radar
  (/candidate/dashboard)
```

---

## 3. Real-Time Opportunity Discovery Mechanism

A central challenge in automated job matching is that real-world ATS postings frequently omit explicit graduation years. Standard filtering systems classify these roles as disqualified, resulting in a "zero-eligible" problem for fresher candidates.

JobRadar solves this with an **Opportunity Discovery Architecture**:

### Four-Tier Eligibility Taxonomy
1. **`ELIGIBLE`:** Confirmed hard-constraint match (e.g. posting explicitly names 2026 graduates and candidate graduated in 2026).
2. **`LIKELY_ELIGIBLE`:** Strong positive indicators without disqualifying clauses (e.g. entry-level intern with matching skills).
3. **`NEEDS_VERIFICATION`:** High technical match relevance, but critical metadata (e.g. graduation year) is omitted in the posting text.
4. **`NOT_ELIGIBLE`:** Disqualified by explicit hard constraints (e.g. requires 5+ years experience, or explicitly targets 2020-2022 graduates).

### Amber Discovery Banner
On `/candidate/dashboard`, jobs with `NEEDS_VERIFICATION` are rendered in an eye-catching amber banner:
- Candidates are alerted to high-relevance opportunities that they may still qualify for.
- Candidates can review the exact evidence and make an informed decision to apply.
- No graduation year is ever hallucinated or fabricated by the system.

---

## 4. Application Tracking & Kanban Lifecycle

When a candidate finds a compelling opportunity, they manage their pipeline directly from the application interface:

```
[Discovered Job]
       |
       +---> [Save Job]          (status: 'saved')
       |           |
       |           v
       +---> [I Applied]         (status: 'applied')
                   |
                   v
             [Interviewing]      (status: 'interviewing')
                   |
                   +---> [Offered]   (status: 'offered')
                   |
                   +---> [Rejected]  (status: 'rejected')
```

### Lifecycle Guarantees
- **Candidate-Scoped State:** State changes write to `candidate_application` and append an audit row to `candidate_application_history`.
- **Shared Inventory Immutability:** Shared `job.status` remains `new`. Multiple candidates can track the same job independently without state collisions.
- **Conversion Analytics:** The `/candidate/applications` view renders conversion metrics per ATS source platform (e.g. interview conversion rates from Greenhouse vs Lever).
