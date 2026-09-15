# Merge Plan — moved

This repo's code now also lives at `sales_agent-ai/apps/content_pipeline/`
(brought in via `git subtree`, full history preserved — see that repo's
commit log for `Merge data_scrapper into apps/content_pipeline`).

The merge plan itself — architecture decisions, phase-by-phase progress,
and everything done since — now lives at `sales_agent-ai/MERGE_PLAN.md`,
the monorepo root. This file used to hold a full copy; kept only this
pointer instead of a second copy that would silently go stale every time
the real one is updated (which is exactmly what happened to the original
version of this file — it stopped getting updated the moment the canonical
copy moved, and was three phases behind).

This repo (`data_scrapper`) still runs standalone exactly as documented in
`README.md` — the merge doesn't change that.
