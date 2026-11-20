You are checking how well a candidate fits a job posting, the way a careful recruiter would before
deciding whether the candidate should apply. Do not use any tools: everything you need is below.

Use only the CV and the extra facts below as evidence. Do not assume skills, tools or experience that
are not there. Rephrased or clearly equivalent experience counts (e.g. "mixed-effects models" for
"hierarchical models"); a different field's version of a skill counts as partial.

Steps:
1. Pick the 5–8 requirements that decide who gets hired for this job: the core duties and the
   must-have qualifications (degree, years and kind of experience, key tools and methods, domain,
   languages). Mark each as "must" or "nice" (nice-to-have / preferred).
2. For each requirement, give a status:
   - "match": the CV clearly shows it.
   - "partial": related or transferable experience, or the skill without the required depth or domain.
   - "gap": not in the CV.
   and one short line of evidence from the CV (or what is missing).
3. Give a compatibility score from 0 to 100: how likely this CV is to get an interview for this job.
   Must-haves weigh far more than nice-to-haves. A missing must-have keeps the score at 60 or below;
   several missing must-haves keep it below 40. A required language the CV lacks, or far more
   seniority than the CV shows, keeps it below 30.
4. Write a summary of one or two sentences: the main reason for the score, and the biggest gap.

Reply with only this JSON, no other text:
{"score": 0-100, "summary": "...", "requirements": [{"requirement": "...", "importance": "must|nice", "status": "match|partial|gap", "evidence": "..."}]}

=== JOB POSTING ===
{posting}

=== CV (LaTeX source) ===
{cv}

=== EXTRA FACTS (true, not in the CV) ===
{facts}
