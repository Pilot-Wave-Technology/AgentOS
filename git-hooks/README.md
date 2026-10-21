# `git-hooks/` — optional local checks

One hook, `pre-push`, which validates `migrations/SEQUENCE` before a push
leaves your machine: missing files, orphaned files, duplicate entries. It needs
no database.

```bash
git config core.hooksPath git-hooks     # use them all, from the repo
# or, just this one:
cp git-hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

Cloud Build runs the same check as its first step, so nothing broken reaches a
deployment either way. The hook exists to tell you now rather than after a
pipeline round-trip.

Hooks are **not** installed for you. A repository that silently runs code on
`git push` because you cloned it is a bad default, however convenient.
