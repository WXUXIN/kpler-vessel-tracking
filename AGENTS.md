## Agent skills

### Issue tracker

Issues and specs live as GitHub issues on `WXUXIN/kpler-vessel-tracking`, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context layout: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Implementation log

After every `/implement` run, append an entry at the top of `docs/implementation-log.md`
and commit it with the work. Sections: **Done**, **Files changed**, **Verified**,
**Review caught**, **Take note**.

Files changed is a table of every path in the commit with its line counts and what the
change was for, so a review knows where to look without reading the diff first; get the
counts from `git show --numstat` rather than estimating, and name anything that slipped
in unintentionally as such.

Take note is the point of the file - deliberate deviations, gaps left open, anything a
review raised that you pushed back on, and anything a later ticket inherits. Write the
entry during the run rather than reconstructing it afterwards, and never leave a section
empty without saying there is nothing in it.

### Before committing

Stage first, then run `git add -A && git diff --cached --check`. Editors here add
trailing whitespace on save and `git add -A` has twice swept a stray one-character
change into an unrelated commit. Staging first matters: `git diff --check` alone cannot
see untracked files, so a whole new file passes the gate without being looked at.
