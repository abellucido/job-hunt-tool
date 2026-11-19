# base/: your own documents (never committed)

Everything in this folder except this README is ignored by git. Put here:

| File | What it is |
|---|---|
| `cv_master.tex` | Your master CV in LaTeX. The single source of truth for facts; never edited by the tools |
| `master.tex` | Your master cover letter template in LaTeX, with `[PLACEHOLDERS]` to fill |
| `facts.md` | Optional: true facts that aren't in the CV (extra skills, preferences, standing conventions) |

The `/apply` skill (see `skills/apply/SKILL.md`) copies these into `output/<date>_<Company>_<Role>/`
and tailors the copies.
