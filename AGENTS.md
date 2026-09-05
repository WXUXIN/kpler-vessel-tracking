## Agent skills

### Issue tracker

Issues and specs live as GitHub issues on `WXUXIN/kpler-vessel-tracking`, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context layout: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Implementation log

After every `/implement` run, append an entry at the top of `docs/implementation-log.md`
and commit it with the work. Sections: **Done**, **Verified**, **Review caught**, **Take
note**. Take note is the point of the file - deliberate deviations, gaps left open,
anything a review raised that you pushed back on, and anything a later ticket inherits.
Write it during the run rather than reconstructing it afterwards, and never leave the
section empty without saying there is nothing in it.
