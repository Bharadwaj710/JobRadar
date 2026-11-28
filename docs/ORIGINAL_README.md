# Original JobRadar README (Upstream Archive)

> **Note on Upstream Provenance:**
> This file preserves the original upstream README from Sameer (@ThePlator) at base commit `fdd739b` ([https://github.com/ThePlator/JobRadar.git](https://github.com/ThePlator/JobRadar.git)).
> It documents the original single-user CLI agent architecture prior to the Spektra Systems assignment extensions.
> For the current multi-candidate, four-track J4 career intelligence platform, see the root [README.md](../README.md).

---

# JobRadar

**One inbox for every new job from company career pages, job boards and the Telegram groups you follow: deduplicated, ranked, and ready to apply.**

**Website:** [jobradar.theplator.dev](https://jobradar.theplator.dev/), see how it works and try the post reader in your browser.

JobRadar is an open-source, self-hosted agent. It reads company career sites directly (Greenhouse, Lever, Ashby, SmartRecruiters, Workday and Oracle boards, plus freehire.me's search across about 90 hiring systems), watches hiring-drive pages and your Telegram job channels, and takes jobs you forward by email. It reads each job page, pulls out each job's details (free rules first, an LLM only when needed), scores how well each job fits your profile, emails you only about strong matches you're eligible for, builds a tailored one-page LaTeX resume, and lays everything out on a local dashboard (and in Notion, if you want it). You review and click Submit; JobRadar never applies for you.

> **Status:** pre-1.0, and I use it every day for my own job search. Working today: company boards, hiring-drive pages, Telegram and email-forward sources, dedupe, ad and scam filtering, free extraction with a Gemini fallback, the Match Score, email alerts and digests, tailored resumes, follow-up reminders, replies from companies, and a local dashboard. Notion and Telegram are optional. Not done yet: a one-command Docker setup and [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) matching. See the [roadmap](#roadmap).

---

## Why

Job hunting means checking dozens of company career pages every day, and if you are a fresher in India, also 10 to 50 Telegram and WhatsApp groups that post hundreds of links a day. Most of those links are duplicates, irrelevant, or scams. Good postings get missed or found days late, deadlines pass, and every application needs a resume tweak and the same form answers typed again.

JobRadar fixes that:

- **Straight from the source.** The company boards you pick are read through their public job APIs every 20 to 30 minutes, so a new posting reaches you minutes after it goes up, not when someone reposts it. `jobradar boards add <careers link>` adds one. Hiring-drive pages are checked every few hours (robots.txt obeyed).
- **And from the groups.** Telegram channels are watched in real time, with backfill on first run, and you can forward any job by email.
- **One job, one row.** A job posted in 10 groups, or one role posted for three cities, shows up once, with a "Seen In" count.
- **Ranked by fit.** Each job is checked against your profile: are you eligible, is it a role you want, do your skills cover it, does the location work. Code turns the answers into a 0-100 Match Score with a reason: *Good fit / 82 / eligible / Python, SQL / missing Docker / Remote*.
- **Free first.** Most details come from the job page's structured data and the post's own "Company: ... Role: ..." lines. An LLM is only called when those fall short: 138 of the first 181 Telegram jobs needed no AI call at all.
- **Scams flagged.** Registration fees, personal-email-only HR and unrealistic pay raise a Scam Risk flag. A likely scam is never emailed.
- **Tailored resumes that only say true things.** Strong matches get a one-page PDF built only from facts in your profile. A checker undoes any rewrite that adds a number, tool or claim your profile doesn't have.
- **Alerts you can trust.** An email only when a job is a strong match, you're eligible, it's fresh and it isn't a likely scam. One email per job, a daily cap, a morning and evening digest, and closing-deadline reminders.
- **After you apply.** Tap **I applied** in an email or on the dashboard. JobRadar drafts a follow-up after 10 quiet days and spots interview invites, tests and rejections in your inbox (it suggests the status change; it never makes it on its own).

---

## Documentation

- [Commands](COMMANDS.md): every command, logs, settings and fixes
- [The dashboard on your phone](PHONE.md): Tailscale in about ten minutes
- [Product Requirements (PRD)](PRD.md): goals, user stories, requirements, metrics, risks
- [High-Level Design (HLD)](HLD.md): architecture, flows, integrations, deployment, security
- [Low-Level Design (LLD)](LLD.md): schema, models, module specs, prompts, Notion mapping, plugins

## License

[MIT](../LICENSE) (Copyright (c) 2026 ThePlator)
