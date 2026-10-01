#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# mendeley_group_export_with-bib.py — group PDFs and folder/group BibTeX export.
# Copyright (C) 2026  David Cuartielles
# Modified 2026-10-01: flat AuthorYear_Title PDF names, folder and group BibTeX
# bibliographies with relative attachment links, and automatic metadata export.
# Based on https://github.com/dcuartielles/Mendeley-tools
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. This program is distributed WITHOUT ANY WARRANTY; see
# the GNU General Public License <https://www.gnu.org/licenses/> for details.
"""
Export a Mendeley group's PDFs and bibliographies into one destination directory.

Uses the Mendeley REST API (api.mendeley.com) with OAuth2 authorization-code
flow. Standard library only -- no pip installs required.

Nothing in Mendeley's local data folder is touched; this only downloads
from the server into DEST_DIR.

Usage (PowerShell):
    $env:MENDELEY_CLIENT_ID     = "<your client id>"
    $env:MENDELEY_CLIENT_SECRET = "<your client secret>"
    uv run --env-file .env mendeley_group_export_with-bib.py --group "My Group Name" --dest "C:/path/to/out"

If --group is omitted (or not found) the script prints all your groups and exits
so you can copy the exact name.

Outputs (all directly in --dest):
    FirstAuthor2025_Title.pdf
    _Folder.bib
    _Parent｜Child.bib
    __Group Name.bib
    references.csv and references.json (always written)

Windows forbids ASCII | in filenames. The fullwidth vertical line U+FF5C (｜)
is used on Windows; other systems use ASCII |. Each folder bibliography contains
its direct members, not its descendants. The group bibliography contains every
reference, including unfiled references and references without PDFs. PDF links
are relative to the bibliography. Multiple PDFs use JabRef's attachment syntax.
Existing files are never overwritten: a repeated export gets numbered names.
"""

import argparse
import base64
import csv
import html
import http.client
import http.server
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

API = "https://api.mendeley.com"
AUTHORIZE_URL = f"{API}/oauth/authorize"
TOKEN_URL = f"{API}/oauth/token"
REDIRECT_URI = "http://localhost:8080/callback"
REDIRECT_PORT = 8080
USER_AGENT = "Mendeley-tools/2.0 (+local group and BibTeX export)"
TIMEOUT = 60
FOLDER_SEPARATOR = "｜" if os.name == "nt" else "|"


# ---------------------------------------------------------------- OAuth ------
_auth_code = {}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        if not secrets.compare_digest(qs.get("state", [""])[0], _auth_code["state"]):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid authorization state. Return to the terminal.")
            return
        _auth_code["code"] = qs.get("code", [None])[0]
        _auth_code["error"] = qs.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = ("Authorization received. You can close this tab and return to the terminal."
               if _auth_code.get("code") else
               f"Authorization failed: {_auth_code.get('error')}")
        self.wfile.write(f"<html><body><h3>{html.escape(msg)}</h3></body></html>".encode())

    def log_message(self, *args):
        pass  # silence


def get_access_token(client_id, client_secret):
    _auth_code.clear()
    _auth_code["state"] = secrets.token_urlsafe(32)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "all",
        "state": _auth_code["state"],
    }
    url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    server.timeout = 1

    print("Opening your browser to authorize with Mendeley...")
    print(f"If it doesn't open, paste this URL manually:\n  {url}\n")
    webbrowser.open(url)
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            server.handle_request()
            if _auth_code.get("code") or _auth_code.get("error"):
                break
    finally:
        server.server_close()

    if _auth_code.get("error"):
        sys.exit(f"OAuth error: {_auth_code['error']}")
    code = _auth_code.get("code")
    if not code:
        sys.exit("Timed out waiting for authorization (no code received).")

    # Exchange code for token (HTTP Basic auth with client id/secret)
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            tok = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"Token exchange failed: HTTP {e.code} {e.reason}\n"
                 f"Server said: {detail}\n"
                 f"(Common causes: wrong client secret, or redirect URI not "
                 f"registered as exactly {REDIRECT_URI})")
    return tok["access_token"]


# ------------------------------------------------------------- API helpers ---
def _open(token, url, accept, follow=True):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    req.add_header("User-Agent", USER_AGENT)
    if follow:
        return urllib.request.urlopen(req, timeout=TIMEOUT)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e  # signed download URL is in Location
        raise


def paged(token, path, accept, params):
    """Yield items across all pages, following the Link: rel=next header."""
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    while url:
        with _open(token, url, accept, follow=True) as resp:
            data = json.load(resp)
            link = resp.headers.get("Link", "")
        if not isinstance(data, list):
            raise ValueError("Expected a JSON collection from the Mendeley API.")
        for item in data:
            yield item
        m = re.search(r'<([^>]+)>;\s*rel=[\"\']?next(?:[\"\']|\s|$)', link)
        url = urllib.parse.urljoin(url, m.group(1)) if m else None


# -------------------------------------------------------------------- main ---
def sanitize(name, maxlen=150):
    # Braces/semicolons are also removed to keep BibTeX attachment paths simple.
    name = re.sub(r'[<>:"/\\|?*{};\x00-\x1f]', "_", str(name)).strip(" .")
    name = name[:maxlen]
    # Keep a complete filename below filesystem component limits, including UTF-8.
    while len(name.encode("utf-8")) > 180:
        name = name[:-1]
    name = name.rstrip(" .") or "untitled"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    return name


def claim_filename(stem, extension, used):
    """Reserve a case-insensitively unique filename; used includes existing files."""
    candidate = stem + extension
    index = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({index}){extension}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def pdf_stem(doc):
    authors = doc.get("authors") or []
    first = authors[0] if authors else {}
    author = first.get("last_name") or first.get("first_name") or "UnknownAuthor"
    year = doc.get("year") or "n.d."
    return sanitize(f"{author}{year}_{doc.get('title') or 'Untitled'}")


def folder_path(folder, by_id):
    """Resolve a folder's full ancestry without relying on API list order."""
    parts, seen = [], set()
    while folder:
        fid = folder["id"]
        if fid in seen:
            raise ValueError(f"Cycle in folder hierarchy at {fid}.")
        seen.add(fid)
        parts.append(sanitize(folder.get("name") or fid))
        parent = folder.get("parent_id")
        if parent and parent not in by_id:
            raise ValueError(f"Missing parent folder {parent} for {fid}.")
        folder = by_id.get(parent)
    return list(reversed(parts))


def folder_bib_stem(folder, by_id):
    # Sanitize components before joining, so ASCII | can be used on POSIX.
    path = FOLDER_SEPARATOR.join(folder_path(folder, by_id))
    while len(path.encode("utf-8")) > 180:
        path = path[:-1]
    return "_" + path.rstrip(" .")


def _split_bib_fields(body):
    """Split on commas outside nested braces or quoted BibTeX strings."""
    fields, start, depth, quoted, escaped = [], 0, 0, False, False
    for i, ch in enumerate(body):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        elif ch == '"' and depth == 0:
            quoted = not quoted
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced braces in API BibTeX entry.")
        elif ch == "," and depth == 0 and not quoted:
            fields.append(body[start:i].strip())
            start = i + 1
    if depth or quoted or escaped:
        raise ValueError("Unterminated value in API BibTeX entry.")
    fields.append(body[start:].strip())
    return fields


def parse_bib_entry(text):
    text = text.strip().lstrip("\ufeff")
    match = re.match(r"@([A-Za-z]+)\s*([{(])", text)
    if not match or match.group(1).lower() in ("comment", "preamble", "string"):
        raise ValueError("The API did not return a single BibTeX reference.")
    closing = "}" if match.group(2) == "{" else ")"
    if not text.endswith(closing):
        raise ValueError("Incomplete API BibTeX entry.")
    fields = _split_bib_fields(text[match.end():-1])
    key = fields.pop(0)
    if not key or "=" in key:
        raise ValueError("Missing citation key in API BibTeX entry.")
    for field in fields:
        if field and not re.match(r"^[A-Za-z][A-Za-z0-9_-]*\s*=", field):
            raise ValueError("Invalid field in API BibTeX entry.")
    return match.group(1), key, [f for f in fields if f]


def render_bib_entry(entry, key, document_id, pdf_names):
    kind, _, fields = entry
    # Remove API attachment paths: only successful local downloads may be linked.
    fields = [f for f in fields if not re.match(
        r"^(file|mendeley_id)\s*=", f, re.I)]
    fields.append("mendeley_id = {" + document_id + "}")
    if len(pdf_names) == 1:
        path = pdf_names[0]
    else:
        path = ";".join(f":{name}:PDF" for name in pdf_names)
    if path:
        fields.append("file = {" + path + "}")
    return "@" + kind + "{" + key + ",\n  " + ",\n  ".join(fields) + "\n}\n"


def fetch_bib_entry(token, document_id):
    did = urllib.parse.quote(document_id, safe="")
    with _open(token, f"{API}/documents/{did}?view=bib",
               "application/x-bibtex") as response:
        text = response.read().decode("utf-8-sig")
    return parse_bib_entry(text)


def is_pdf(attachment):
    mime = (attachment.get("mime_type") or "").split(";", 1)[0].lower()
    return mime == "application/pdf" or (
            attachment.get("file_name") or "").lower().endswith(".pdf")


def download_pdf(token, attachment, dest, filename):
    """Stream to a temporary file; leave no partial PDF if downloading fails."""
    fid = urllib.parse.quote(attachment["id"], safe="")
    with _open(token, f"{API}/files/{fid}",
               "application/vnd.mendeley-file.1+json", follow=False) as response:
        location = response.headers.get("Location")
        if not location:
            raise ValueError("API response has no signed download URL.")
    location = urllib.parse.urljoin(API + "/", location)
    request = urllib.request.Request(location, headers={"User-Agent": USER_AGENT})
    tmp_name = None
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            with tempfile.NamedTemporaryFile(dir=dest, delete=False) as output:
                tmp_name = output.name
                shutil.copyfileobj(response, output, length=1024 * 1024)
        expected_size = attachment.get("size")
        if expected_size is not None and os.path.getsize(tmp_name) != int(expected_size):
            raise ValueError("Downloaded size differs from the API file size.")
        with open(tmp_name, "rb") as source:
            if b"%PDF-" not in source.read(1024):
                raise ValueError("Downloaded file has no PDF header.")
        target = os.path.join(dest, filename)
        # Exclusive creation also protects files created since names were reserved.
        created = False
        try:
            with open(tmp_name, "rb") as source, open(target, "xb") as output:
                created = True
                shutil.copyfileobj(source, output, length=1024 * 1024)
        except BaseException:
            if created:
                os.unlink(target)
            raise
    finally:
        if tmp_name is not None:
            os.unlink(tmp_name)


def write_bib(dest, filename, ids, entries):
    with open(os.path.join(dest, filename), "x", encoding="utf-8", newline="\n") as output:
        output.write("% Mendeley group export. PDF paths are relative to this file.\n\n")
        for document_id in ids:
            output.write(entries[document_id] + "\n")


def _fmt_authors(doc):
    out = []
    for a in doc.get("authors", []) or []:
        last = a.get("last_name", "")
        first = a.get("first_name", "")
        out.append(f"{last}, {first}".strip(", ").strip())
    return "; ".join(out)


META_COLUMNS = ["title", "authors", "year", "type", "source",
                "doi", "isbn", "issn", "pmid", "has_pdf", "pdf_filename", "id"]


def _doc_row(doc, pdf_by_doc):
    ids = doc.get("identifiers", {}) or {}
    pdfs = pdf_by_doc.get(doc["id"], [])
    return {
        "title": doc.get("title", ""),
        "authors": _fmt_authors(doc),
        "year": doc.get("year", ""),
        "type": doc.get("type", ""),
        "source": doc.get("source", ""),
        "doi": ids.get("doi", ""),
        "isbn": ids.get("isbn", ""),
        "issn": ids.get("issn", ""),
        "pmid": ids.get("pmid", ""),
        "has_pdf": "yes" if pdfs else "no",
        "pdf_filename": "; ".join(pdfs),
        "id": doc["id"],
    }


def write_metadata(dest, docs, pdf_by_doc, used):
    """Write references.csv and references.json for every document in the group."""
    rows = [_doc_row(d, pdf_by_doc) for d in docs]
    rows.sort(key=lambda r: (str(r["year"] or ""), (r["title"] or "").lower()))

    csv_path = os.path.join(dest, claim_filename("references", ".csv", used))
    with open(csv_path, "x", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=META_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    json_path = os.path.join(dest, claim_filename("references", ".json", used))
    with open(json_path, "x", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False, indent=2)

    with_pdf = sum(1 for r in rows if r["has_pdf"] == "yes")
    print(f"\nMetadata: {len(rows)} references written "
          f"({with_pdf} with a PDF, {len(rows) - with_pdf} reference-only)")
    print(f"  - {csv_path}")
    print(f"  - {json_path}  (full raw records)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", help="Exact group name (case-insensitive)")
    ap.add_argument("--dest", help="Destination folder")
    ap.add_argument("--list-groups", action="store_true", help="Just list groups and exit")
    args = ap.parse_args()

    cid = os.environ.get("MENDELEY_CLIENT_ID")
    secret = os.environ.get("MENDELEY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set MENDELEY_CLIENT_ID and MENDELEY_CLIENT_SECRET env vars first.")

    token = get_access_token(cid, secret)
    print("Authorized.\n")

    groups = list(paged(token, "/groups", "application/vnd.mendeley-group.1+json",
                        {"limit": 500}))
    if args.list_groups or not args.group:
        print("Your groups:")
        for g in groups:
            print(f"  - {g['name']}   (id={g['id']})")
        if not args.list_groups:
            print("\nRe-run with --group \"<name>\" --dest \"<folder>\".")
        return 0

    match = [g for g in groups if g["name"].lower() == args.group.lower()]
    if not match:
        print(f'No group named "{args.group}". Available:')
        for g in groups:
            print(f"  - {g['name']}")
        sys.exit(1)
    if len(match) > 1:
        sys.exit("Several groups have this name. Rename one in Mendeley to disambiguate.")
    return export_group(token, match[0], args.dest)


def export_group(token, group, destination=None):
    gid = group["id"]
    print(f'Group: {group["name"]}  (id={gid})')

    # Full document records (view=all gives authors, year, identifiers, etc.)
    docs = list(paged(token, "/documents", "application/vnd.mendeley-document.1+json",
                      {"group_id": gid, "limit": 500, "view": "all"}))
    docs.sort(key=lambda d: (str(d.get("year") or ""),
                             (d.get("title") or "").casefold(), d["id"]))
    docs_by_id = {d["id"]: d for d in docs}
    if len(docs_by_id) != len(docs):
        raise ValueError("Duplicate document IDs returned by the API. Please retry.")
    print(f"Documents in group: {len(docs)}")

    files = list(paged(token, "/files", "application/vnd.mendeley-file.1+json",
                       {"group_id": gid, "limit": 500}))
    pdfs = sorted((f for f in files if is_pdf(f)),
                  key=lambda f: (f.get("document_id") or "", f["id"]))
    print(f"Files attached: {len(files)} ({len(pdfs)} PDFs)\n")

    folders = list(paged(token, "/folders", "application/vnd.mendeley-folder.1+json",
                         {"group_id": gid, "limit": 500}))
    folders_by_id = {f["id"]: f for f in folders}
    folders.sort(key=lambda f: (folder_path(f, folders_by_id), f["id"]))
    folder_members = {}
    for folder in folders:
        fid = urllib.parse.quote(folder["id"], safe="")
        members = list(paged(token, f"/folders/{fid}/documents",
                             "application/vnd.mendeley-document.1+json", {"limit": 500}))
        ids = {d["id"] for d in members}
        missing = ids.difference(docs_by_id)
        if missing:
            raise ValueError("Folder membership changed during export, or the API "
                             "returned references absent from the group. Please retry.")
        folder_members[folder["id"]] = [d["id"] for d in docs if d["id"] in ids]
    print(f"Folders in group: {len(folders)}")

    dest = destination or os.path.join(
        os.path.expanduser("~"), "Desktop", "Mendeley-export", sanitize(group["name"]))
    os.makedirs(dest, exist_ok=True)
    print(f"Downloading into: {dest}\n")

    used = {name.casefold() for name in os.listdir(dest)}
    pdf_by_doc = {}
    ok = 0
    for f in pdfs:
        document_id = f.get("document_id")
        if document_id not in docs_by_id:
            raise ValueError("An attachment belongs to a reference absent from the group. "
                             "The group may have changed during export; please retry.")
        fname = claim_filename(pdf_stem(docs_by_id[document_id]), ".pdf", used)
        try:
            download_pdf(token, f, dest, fname)
            ok += 1
            pdf_by_doc.setdefault(document_id, []).append(fname)
            print(f"  + {fname}")
        except (OSError, ValueError, http.client.HTTPException) as e:
            print(f"  ! failed {fname}: {e}")

    print(f"\nPDFs downloaded: {ok}/{len(pdfs)}")
    write_metadata(dest, docs, pdf_by_doc, used)

    print("\nRetrieving BibTeX entries...")
    entries, keys = {}, set()
    for index, doc in enumerate(docs, 1):
        entry = fetch_bib_entry(token, doc["id"])
        preferred = doc.get("citation_key") or entry[1]
        base = re.sub(r"[^A-Za-z0-9_:.-]", "_", str(preferred)) or "reference"
        key, number = base, 2
        while key in keys:
            key = f"{base}_{number}"
            number += 1
        keys.add(key)
        entries[doc["id"]] = render_bib_entry(entry, key, doc["id"],
                                              pdf_by_doc.get(doc["id"], []))
        if index == 1 or index % 25 == 0 or index == len(docs):
            print(f"  {index}/{len(docs)} references")

    group_bib = claim_filename("__" + sanitize(group["name"]), ".bib", used)
    write_bib(dest, group_bib, [d["id"] for d in docs], entries)
    print(f"  + {group_bib} ({len(docs)} references)")
    for folder in folders:
        name = claim_filename(folder_bib_stem(folder, folders_by_id), ".bib", used)
        ids = folder_members[folder["id"]]
        write_bib(dest, name, ids, entries)
        print(f"  + {name} ({len(ids)} references)")

    print(f"\nDone. {len(folders) + 1} bibliographies written to {dest}")
    if ok != len(pdfs):
        print("Some PDFs failed to download. Their references remain in the bibliographies "
              "without links to the missing PDFs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, http.client.HTTPException) as error:
        sys.exit(f"Export failed: {error}")
    except KeyboardInterrupt:
        sys.exit("Export interrupted.")
