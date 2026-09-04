# CleanFrame Wiki

This directory mirrors the project documentation for publishing to the
[GitHub Wiki](https://github.com/inboxpraveen/Cleanframe/wiki).

GitHub Wiki is a **separate** git repository (`Cleanframe.wiki.git`). Editing
files here does **not** update the live wiki until they are synced.

## Automatic sync (recommended)

A GitHub Action (`.github/workflows/sync-wiki.yml`) copies every `wiki/*.md`
page (except this README) to the live wiki whenever those files change on
`main` / `master`.

Prerequisites:

1. **Settings → General → Features → Wikis** is enabled.
2. The wiki already exists (create any page once in the Wiki UI if needed).
3. This workflow file is on `main`.
4. A `WIKI_TOKEN` secret exists — see below. It is **required**, not optional:
   GitHub Actions' built-in `GITHUB_TOKEN` can clone a wiki but cannot push to
   one, so without it the job fails with "Password authentication is not
   supported for Git operations".

Then any push to `main` that touches `wiki/**` publishes automatically.
You can also run **Actions → Sync Wiki → Run workflow** manually.

### Required: the `WIKI_TOKEN` secret

A wiki lives in its own git repository, and the token GitHub Actions injects has
no write access to it. There is also **no “Wikis” checkbox** on fine-grained
PATs — that is expected.

Use a **classic** personal access token:

1. [Classic tokens](https://github.com/settings/tokens) → **Generate new token (classic)**
2. Enable the **`repo`** scope (full control of private repositories — includes wiki push)
3. Repo **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `WIKI_TOKEN`
   - Value: the classic token

The workflow checks for it first and stops with an explanatory message if it is
missing, rather than failing on an opaque authentication error.

## Manual sync (optional)

```bash
# One-time: clone the wiki repo beside the main repo
git clone https://github.com/inboxpraveen/Cleanframe.wiki.git

# Copy pages (from Cleanframe repo root; skip this README)
cp wiki/*.md ../Cleanframe.wiki/
rm -f ../Cleanframe.wiki/README.md   # keep wiki Home.md as the landing page

cd ../Cleanframe.wiki
git add -A
git commit -m "Sync wiki documentation"
git push
```

In-repo canonical docs (same content, relative links): [`../docs/`](../docs/).
