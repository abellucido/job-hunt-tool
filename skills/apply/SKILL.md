---
name: apply
description: Tailor a LaTeX CV and cover letter to a job posting and compile them to PDF, given as pasted text, a link, or a jobscout job file. Use when the user runs /apply, pastes a job description or posting link, or asks to tailor their CV or cover letter for a job.
---

# Job application tailoring (CV → cover letter)

This is a generic version of the skill the Job Hunt web page runs. Copy this folder to
`~/.claude/skills/apply/`, then adapt the details (your strengths, your template's placeholders)
to your own documents.

Workspace: the repository folder (the one containing `base/`, `jobscout/` and `webapp/`).
- `base/cv_master.tex`: master CV. Never modify it.
- `base/master.tex`: master cover letter with `[PLACEHOLDERS]`. Never modify it.
- `base/facts.md` (optional): extra true facts that aren't in the CV.
- `output/<YYYY-MM-DD>_<Company>_<Role>/`: one folder per application, holding `job_description.txt`,
  `analysis.md`, `cv.tex`, `cover_letter.tex` and the compiled PDFs (aux files in `build/`).

`cv_master.tex` is the single source of truth for facts.

By default do both documents. If the user says "CV only" or "letter only", do just that one.

## Ground rules (both documents)

- **Never invent or inflate.** Do not change dates, job titles, employers, metrics, publications,
  certifications, or language levels. Do not add skills, tools, or experience that aren't in the
  masters or `facts.md`. Rephrasing a true bullet in the posting's terminology is fine; if unsure,
  keep the original wording.
- Keep the preamble, packages, colours and layout macros unchanged. Escape LaTeX specials
  (`& % $ # _ { } ~ ^`) in new text.
- Do not make either document longer than its master.

## Steps

1. **Get the job description.**
   - Use the text from the arguments. If none was given, ask for a description or a link, and stop.
   - If it's a path to a `.txt` file (e.g. from `jobscout/runs/<run>/jobs/`), read it: it holds the
     title, company, location, URL and full posting text.
   - If it's a URL, fetch it and extract the title, company, location, responsibilities and
     requirements. If the page has no actual posting (login wall, JavaScript-only page), say so and
     ask for the text.

2. **Analyse the role** and write `analysis.md`. Its first line must be
   `# <Company>: <Role> (<Location>)`; the Tracker page reads company, role and location from it.
   Then:
   - Team or recipient, if stated.
   - The 5–8 key requirements, each mapped to matching evidence from the CV, or marked **gap**.
   - The tailoring plan: which experiences and skills to lead with.
   If the role is clearly a poor fit (most core requirements are gaps), say so in `analysis.md`
   and stop without tailoring. Otherwise continue straight to step 3.

3. **Tailor the CV** (copy `cv_master.tex` → `cv.tex`):
   - **Profile:** rewrite for this role in 2–3 sentences, leading with the most relevant strengths
     and using the posting's terms where true.
   - **Skills:** reorder rows and items within rows by relevance.
   - **Experience:** keep reverse-chronological order; within each role, put the most relevant
     bullets first and tune their wording towards the posting (truthfully).
   - **Projects and certifications:** reorder by relevance; drop a few clearly irrelevant projects
     if that keeps the CV focused. Education and publications stay unchanged.

4. **Tailor the cover letter** (copy `master.tex` → `cover_letter.tex`). Make it consistent with
   the tailored CV: highlight the same 2–3 experiences the CV now leads with. Fill every
   placeholder (company, recipient, job title, why this role, relevant experience) and remove any
   "customise this" comments.

5. **Check both files:**
   - No leftover placeholders: `grep -n '\[[A-Z]' cover_letter.tex` finds nothing in the body.
   - Braces are balanced and every `\begin{...}` has a matching `\end{...}`.
   - `diff base/cv_master.tex <out>/cv.tex` shows no changed date, number, title or employer.

6. **Save** everything to the output folder, including the posting as `job_description.txt`.

7. **Compile both PDFs** (run twice each so references settle; aux files go to `build/`):
   ```bash
   cd "<out>" && mkdir -p build
   for i in 1 2; do pdflatex -interaction=nonstopmode -output-directory=build cv.tex; done
   for i in 1 2; do xelatex  -interaction=nonstopmode -output-directory=build cover_letter.tex; done
   cp build/cv.pdf build/cover_letter.pdf .
   ```
   Use the engines your templates need. The cover letter should be 1 page and the CV no longer
   than the master. On failure, read the `! ...` error in the log, fix the `.tex`, and recompile.

8. **Final report:** the paths to `cv.pdf` and `cover_letter.pdf` (mention the `output/<folder>/`
   path; the web page uses it to find the files), page counts, what changed in each document,
   and any gaps the user might address themselves.

Revisions: edit the files in the output folder (never the masters), then recompile.
