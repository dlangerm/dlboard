# Contributing

Thanks for considering a contribution to dlboard.

## Setup

Everything runs through [`uv`](https://docs.astral.sh/uv/) (Python >=3.12, deps pinned in `uv.lock`):

```bash
uv run --env-file .env dlboard serve local     # start the server
uv run pytest                       # run the test suite
uv run pytest -m browser            # run only the browser/e2e tests
uv run pytest -m "not browser and not postgres and not s3"  # everything that needs neither a browser nor Docker
uv run ruff check && uv run ruff format   # lint / format
uv run pyrefly check                      # type check (strict mode)
uv run prek run --all-files         # run pre-commit hooks manually
```

Run `uv run prek install` once after cloning. Besides the commit checks it links a `reference-transaction` git hook
(`tools/tag_guard.py`) the first time a commit runs them, which refuses to create a `vX.Y.Z` tag that disagrees with
`dlboard/_version.py` at the tagged commit -- a pushed release tag is painful to move, so it is caught before one
exists. It only judges tags you make (`git tag`, `git update-ref`); tags that arrive via `git fetch` or `pull` are never refused. Delete tags rather than moving them; `git tag -d` is always allowed.

See [CLAUDE.md](CLAUDE.md) for the fuller set of architecture notes and code-style conventions this
repo follows -- file layout, the plugin system, test placement, and the rest.

## Before you open a PR

- Run the commands above locally first. CI runs the same lint, type-check, and test suites (see
  `.github/workflows/ci.yml`) and will fail on anything they would have caught.
- Keep PRs small and focused on one change. A PR that bundles several unrelated things is harder
  to review and more likely to get bounced back for a split.
- If a change is naturally a sequence of dependent steps, submit it as a stack of small PRs rather
  than one large one -- [`gh stack`](https://github.com/gregoryclarkwitten/gh-stack) is a good way
  to manage that locally.
- Add tests for new behavior. Favor an end-to-end test that asserts behavior over one that pins
  implementation details, and don't duplicate coverage another test already provides.

## When main is red

People run what `main` builds (`latest`/`beta` are its head's image, and releases are cut from it), so a failed or
cancelled run on `main` is an incident. CI files "CI failed on main at `<sha>`" when its run fails on a push, and
`image-freshness.yml` files "The latest image is behind main" when the head has no image two hours after it landed.

1. **Is it main?** The run's event must be `push` on `main`. A `pull_request` run of an already merged PR is not: it
   ran after the merge with a read-only cache token (`Cache mode: read` in its log). Ignore it.
2. **Rerun the failed jobs once.** If they pass, link the run and close the issue; if not, it is real.
3. **Fix forward or revert.** Don't leave the head red while investigating.
4. **Confirm `latest` is the head's image:** `docker buildx imagetools inspect` of `ghcr.io/dlangerm/dlboard:latest`
   and of `:sha-$(git rev-parse --short=7 origin/main)` print the same `Digest`. If not, `gh run rerun` the head's
   `push` run; only the head publishes, so an old run can't move `latest` backwards.

## AI-assisted contributions

dlboard's own history includes a fair amount of AI-assisted code (see the README's "AI Usage"
section) -- so a PR being AI-assisted is not by itself a reason it won't be considered. It does need
to meet the same bar as a PR you wrote by hand: you understand what it does and why, you can defend
it in review, and it follows this repo's conventions rather than generic defaults. A PR too large
for a human reviewer to reasonably follow will be asked to split into a stack, same as any other
contribution.

## Reporting a bug or requesting a feature

Open an issue using the templates under `.github/ISSUE_TEMPLATE/`. For a security issue, see
[SECURITY.md](SECURITY.md) instead -- please don't open a public issue for those.
