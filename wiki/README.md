# CleanFrame Wiki

This directory mirrors the project documentation for publishing to the
[GitHub Wiki](https://github.com/inboxpraveen/Cleanframe/wiki).

GitHub Wiki is a **separate** git repository (`Cleanframe.wiki.git`). Editing
files here does **not** update the live wiki until they are synced.

## Automatic sync (recommended)

A GitHub Action (`.github/workflows/sync-wiki.yml`) copies every `wiki/*.md`
page (except this README) to the live wiki whenever those files change on
`main` / `master`.

**No personal token is required** in the normal case. The workflow uses
GitHub Actions’ built-in `GITHUB_TOKEN` with `contents: write` permission to
push to this repo’s wiki.

Prerequisites:

1. **Settings → General → Features → Wikis** is enabled.
2. The wiki already exists (create any page once in the Wiki UI if needed).
3. This workflow file is on `main`.

Then any push to `main` that touches `wiki/**` publishes automatically.
You can also run **Actions → Sync Wiki → Run workflow** manually.

### Optional: `WIKI_TOKEN` secret

Only needed if the built-in token is blocked in your org. There is **no
“Wikis” checkbox** on fine-grained PATs — that is expected.

Use a **classic** personal access token instead:

1. [Classic tokens](https://github.com/settings/tokens) → **Generate new token (classic)**
2. Enable the **`repo`** scope (full control of private repositories — includes wiki push)
3. Repo **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `WIKI_TOKEN`
   - Value: the classic token

If `WIKI_TOKEN` is set, the workflow uses it; otherwise it uses `GITHUB_TOKEN`.

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
