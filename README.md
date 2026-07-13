# Mendeley tools

Small utilities for working with a Mendeley account. Standard-library Python
only — no `pip install` required.

## `mendeley_group_export.py`

Copies every file attached to a specific Mendeley **group** into a folder on
disk, naming each file by its document title (instead of Mendeley's internal
hash names). Talks to the Mendeley REST API (`api.mendeley.com`) over OAuth2;
it does **not** read or modify Mendeley's local database.

### One-time setup: register an API app

1. Go to <https://dev.mendeley.com/myapps.html> and sign in.
2. Create a new app. Set the **Redirect URL** to exactly:
   `http://localhost:8080/callback`
3. Copy the **Client ID** and generate a **Secret**.

### Usage (PowerShell)

```powershell
$env:MENDELEY_CLIENT_ID     = "<client id>"
$env:MENDELEY_CLIENT_SECRET = "<secret>"

# 1. Dry run — authorize in the browser and list your groups (downloads nothing)
python mendeley_group_export.py --list-groups

# 2. Copy one group's files out, named by document title
python mendeley_group_export.py --group "Exact Group Name" --dest "C:\path\to\out"

# 3. ...and also write references.csv + references.json for EVERY reference
#    in the group (including those with no attached PDF)
python mendeley_group_export.py --group "Exact Group Name" --dest "C:\path\to\out" --include-metadata
```

### `--include-metadata`

Writes two files into the destination folder:

- **`references.csv`** — one row per reference (title, authors, year, type,
  source, doi, isbn, issn, pmid, `has_pdf`, `pdf_filename`, id), sorted by
  year then title. UTF-8 with BOM so Excel opens accented characters cleanly.
- **`references.json`** — the full raw document records from the API.

This captures reference-only entries (no PDF attached) that the file export
alone would miss.

If `--group` is omitted or doesn't match, the script prints all your groups and
exits so you can copy the exact name.

### Notes

- The client secret is used only locally by the script. It is git-ignored via
  `.env` / `client_secret*` patterns — never commit it.
- Download-only: nothing in Mendeley's own data folders is touched.
- Auth uses the OAuth2 authorization-code flow with a one-shot local callback
  server on `http://localhost:8080/callback`.
