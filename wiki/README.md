# CleanFrame Wiki

This directory mirrors the project documentation for publishing to the
[GitHub Wiki](https://github.com/inboxpraveen/Cleanframe/wiki).

## Publish / update the wiki

```bash
# One-time: clone the wiki repo beside the main repo
git clone https://github.com/inboxpraveen/Cleanframe.wiki.git

# Copy pages (from Cleanframe repo root)
cp wiki/*.md ../Cleanframe.wiki/

cd ../Cleanframe.wiki
git add -A
git commit -m "Sync wiki documentation"
git push
```

Or enable the wiki in GitHub repo settings and paste pages via the web UI.

In-repo canonical docs (same content, relative links): [`../docs/`](../docs/).
