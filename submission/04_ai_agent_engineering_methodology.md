# 04. AI-Native Agent Engineering Methodology
**Evaluation Target:** Spektra Systems: AI-Native Software Engineer - Trainee
**Repository:** [JobRadar](file:///D:/JB/JobRadar)
**Branch:** `feat/j4-career-tracks`

---

## 1. Principles of AI-Native Software Engineering

JobRadar represents a showcase of **AI-native engineering discipline**. The extensions developed in this repository were not generated through ad-hoc chatbot queries, but through structured, autonomous agent execution governed by strict architectural principles:

```
+-----------------------------------------------------------------------------+
|                      HUMAN DEVELOPER / SPECIFICATION                        |
|   (Clear bounded goals, explicit constraints, safety limits, acceptance criteria) |
+-----------------------------------------------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
|                     ANTIGRAVITY AUTONOMOUS AGENT                            |
|                                                                             |
|  Phase 1: Read-Only Inspection & Baseline Verification                      |
|  Phase 2: Test-Driven Design (Isolated Sandbox DB Fixtures)                 |
|  Phase 3: Targeted Implementation & Code Refinement                         |
|  Phase 4: Full Quality Gate Execution (Ruff, Mypy, Pytest, Playwright)       |
|  Phase 5: Factual Reporting & Human-in-the-Loop Review Stop                 |
+-----------------------------------------------------------------------------+
```

---

## 2. Core Methodological Pillars

### Pillar 1: Constrained Task Scope and Phased Execution
- Complex epics were decomposed into focused, verifiable sub-phases (e.g. Stage 3C Phase 1 Migration, Phase 1.1 UI Verification, Phase 1.2 Form Controls, Phase 2 Discovery, Phase 3 Final Validation).
- Each task enforced strict boundaries: no mass formatting, no branch switching, no unrequested features, and no production data mutation.

### Pillar 2: Absolute Production Database Invariance
- Agents were instructed with strict negative constraints: **Never mutate or open `data/jobradar.db` for writing during test execution.**
- Every test and demo script operated against isolated temporary databases (`make_engine(tmp_path / "test.db")` or `data/demo/jobradar_demo.db`).
- SHA-256 checksums were calculated before and after every execution phase to objectively prove zero regression.

### Pillar 3: Grounding and Anti-Hallucination Controls
- **Job Requirements:** Extraction algorithms enforce strict quote matching. If an LLM returns a requirement, it must quote the exact sentence from the source job description; otherwise, the finding is discarded.
- **Candidate Profiles:** Resume tailoring logic operates on facts present in `profile_data`. The system never invents skills, tools, metrics, or achievements.
- **Eligibility Semantics:** In the Opportunity Discovery phase, the agent explicitly refused to convert unknown graduation years into confirmed eligibility, preserving truth through the `NEEDS_VERIFICATION` status.

### Pillar 4: Deterministic Free Rules Before LLM Calls
- LLM calls are expensive and subject to rate limits.
- JobRadar implements a tiered architecture: free deterministic regex, string parsing, and rule extractors run first. LLMs (Gemini Flash Lite or Groq) are invoked strictly as fallback parsers for unstructured prose.
- Match score calculations and eligibility checks are 100% deterministic code algorithms, ensuring reproducible evaluations.

### Pillar 5: Continuous Automated Verification Over Verbal Claims
- An agent is never permitted to declare a feature "complete" based solely on code edits.
- Completion requires passing the automated quality gates:
  - Full pytest test suite (994 passed).
  - Playwright browser E2E suites (6 passed).
  - Strict type checking (Mypy clean across 102 files).
  - Linter and style enforcement (Ruff clean).
  - Visual proof verification (capturing actual rendered screenshots).

---

## 3. Human-in-the-Loop Governance

The engineering protocol mandates that the autonomous agent stops after producing each milestone report:
1. The developer inspects the generated report, test logs, and visual screenshots.
2. The developer resolves open design choices (e.g. deciding between archival vs deletion of legacy artifacts).
3. The developer issues explicit approval before git staging, branching, or public release packaging occurs.
