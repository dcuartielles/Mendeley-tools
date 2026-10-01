#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# mendeley_group_export_with-bib.py — attachments and one group BibTeX export.
# Copyright (C) 2026  David Cuartielles
# Modified 2026-10-01: all attachments, one bibliography with folder metadata,
# relative attachment links, metadata, HTTP 451 report, retries and BibTeX cache.
# File-size mismatches are retried and suspect bytes kept with a warning prefix.
# Based on https://github.com/dcuartielles/Mendeley-tools
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. This program is distributed WITHOUT ANY WARRANTY; see
# the GNU General Public License <https://www.gnu.org/licenses/> for details.
"""
Export a Mendeley group's attachments and one bibliography into one directory.

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
    FirstAuthor2025_Title.<original extension>
    _WARNING_UNVERIFIED_FirstAuthor2025_Title.<extension> (failed integrity check)
    __Group Name.bib
    _No PDF written - Unavailable For Legal Reasons
    references.csv and references.json (always written)
    _Mendeley_BibTeX_cache_<group hash>.jsonl (resumable retrieval)

The single bibliography contains all references and folder memberships, using
JabRef group-tree comments for Zotero's built-in BibTeX importer and an embedded
JSON manifest for the exact Mendeley hierarchy. Mendeley's built-in BibTeX import
does not reconstruct folders. All attachment types are downloaded; successful
downloads use relative links in the file field. HTTP 451 failures are listed
by citation key and intended filename in the UTF-8 text report above.
Files that still fail file-size validation after retries are retained with a
warning prefix, UNVERIFIED attachment label, and recorded length diagnostics.
Attachment checksums are not calculated or compared.
Existing files are never overwritten: a repeated export gets numbered names.
Temporary network failures are retried. Completed BibTeX entries are cached
after each reference; rerun with the same --dest to resume BibTeX retrieval.
Attachment downloads are not resumed across runs and receive new numbered names.
"""

import argparse
import base64
import csv
import email.utils
import hashlib
import html
import http.client
import http.server
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import ssl
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
USER_AGENT = "Mendeley-tools/3.3 (+local group and BibTeX export)"
TIMEOUT = 120
REQUEST_ATTEMPTS = 6
LEGAL_REPORT = "_No PDF written - Unavailable For Legal Reasons"


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
class DownloadIntegrityError(ValueError):
    """A received file fails validation; keep diagnostics for retries and export metadata."""
    def __init__(self, reason, details):
        super().__init__(reason)
        self.details = details
        self.attempts = []
        self.temporary_path = None

    def __str__(self):
        d = self.details
        message = (f"{super().__str__()} "
                   f"(file ID {d['file_id']}; expected size {d['expected_size']}, "
                   f"actual size {d['actual_size']} bytes; response type {d['response_content_type'] or 'unspecified'})")
        if len(self.attempts) > 1 and len({a['actual_size'] for a in self.attempts}) == 1:
            message += f"; {len(self.attempts)} failed size checks returned the same byte count"
        if d.get('saved_unverified_filename'):
            message += f"; saved unverified as {d['saved_unverified_filename']}"
        return message


def _transient_error(error):
    if isinstance(error, DownloadIntegrityError):
        return True
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429, 500, 502, 503, 504)
    if isinstance(error, urllib.error.URLError):
        return not isinstance(error.reason, ssl.SSLCertVerificationError)
    return isinstance(error, (TimeoutError, ConnectionError,
                              http.client.IncompleteRead, http.client.RemoteDisconnected))


def _retry_delay(error, attempt):
    delay = min(2 ** attempt, 60)
    if isinstance(error, urllib.error.HTTPError) and error.headers:
        value = error.headers.get("Retry-After")
        if value:
            try:
                seconds = float(value)
            except ValueError:
                try:
                    seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    seconds = 0
            if math.isfinite(seconds) and seconds > delay:
                delay = seconds
    return delay


def _wait_before_retry(seconds):
    # Long server-directed waits remain interruptible and show progress.
    while seconds > 0:
        chunk = min(seconds, 30)
        time.sleep(chunk)
        seconds -= chunk
        if seconds > 0:
            print(f"    waiting another {seconds:.0f}s before retry...", flush=True)


def retry_request(action, label):
    """Retry complete GET operations, including reads, without retrying permanent errors."""
    integrity_history = []
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        try:
            return action()
        except (OSError, http.client.HTTPException, DownloadIntegrityError) as error:
            if isinstance(error, DownloadIntegrityError):
                integrity_history.append(error.details)
                error.attempts = list(integrity_history)
            if not _transient_error(error) or attempt == REQUEST_ATTEMPTS:
                raise
            delay = _retry_delay(error, attempt)
            # Avoid logging full URLs: attachment URLs can contain signed credentials.
            if isinstance(error, DownloadIntegrityError):
                reason = str(error)
            else:
                reason = f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__
            print(f"  ! {label}: {reason}; retry {attempt + 1}/{REQUEST_ATTEMPTS} "
                  f"in {delay:.0f}s", flush=True)
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            _wait_before_retry(delay)


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
        def read_page():
            with _open(token, url, accept, follow=True) as resp:
                return json.load(resp), resp.headers.get("Link", "")
        data, link = retry_request(read_page, f"API page {path}")
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


def attachment_stem(doc):
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
        parts.append(str(folder.get("name") or fid))
        parent = folder.get("parent_id")
        if parent == "root":
            parent = None
        if parent and parent not in by_id:
            raise ValueError(f"Missing parent folder {parent} for {fid}.")
        folder = by_id.get(parent)
    return list(reversed(parts))


def ordered_folders(folders):
    """Validate the hierarchy and return parents before their descendants."""
    by_id = {f["id"]: f for f in folders}
    if len(by_id) != len(folders):
        raise ValueError("Duplicate folder IDs returned by the API. Please retry.")
    children = {}
    for folder in folders:
        folder_path(folder, by_id)  # catches cycles and missing parents
        parent = folder.get("parent_id")
        children.setdefault(None if parent == "root" or not parent else parent, []).append(folder)
    result = []
    stack = list(reversed(sorted(children.get(None, []),
                                 key=lambda f: (str(f.get("name") or "").casefold(), f["id"]))))
    while stack:
        folder = stack.pop()
        result.append(folder)
        stack.extend(reversed(sorted(children.get(folder["id"], []),
                                     key=lambda f: (str(f.get("name") or "").casefold(), f["id"]))))
    return result


def bib_value(value):
    """Escape added human-readable fields, leaving API bibliographic fields intact."""
    replacements = {"\\": r"{\textbackslash}", "{": r"\{", "}": r"\}",
                    "#": r"\#", "$": r"\$", "%": r"\%", "&": r"\&",
                    "_": r"\_", "~": r"{\textasciitilde}", "^": r"{\textasciicircum}"}
    return "".join(replacements.get(c, c) for c in str(value))


def _jabref_escape(value):
    return value.replace("\\", "\\\\").replace(";", r"\;")


def zotero_groups(folders, folder_members, keys_by_doc):
    """Encode the legacy JabRef tree supported by Zotero's built-in importer.

    The importer splits twice, accepts only single-digit levels, stops at '}',
    and merges equal sibling names. Adapt its display tree where necessary;
    the separate embedded manifest always retains the exact original tree.
    """
    by_id = {f["id"]: f for f in folders}
    paths, used_by_parent, warnings = {}, {}, set()
    records = [_jabref_escape("0 AllEntriesGroup:;")]
    for folder in folders:
        raw_path = folder_path(folder, by_id)
        parent = folder.get("parent_id")
        parent_path = paths.get(parent, [])
        level = min(len(raw_path), 9)
        if len(raw_path) > 9:
            parent_path = parent_path[:8]
            name = " | ".join(raw_path[8:])
            warnings.add("Zotero's BibTeX importer supports nine folder levels; deeper paths "
                         "are displayed together at level nine.")
        else:
            name = raw_path[-1]
        safe_name = name.replace("{", "❴").replace("}", "❵").replace("\r", " ").replace("\n", " ")
        if safe_name != name:
            warnings.add("Braces/newlines in Zotero collection names were adjusted for its BibTeX importer.")
        if safe_name in ("__proto__", "constructor", "prototype", "hasOwnProperty"):
            safe_name = "_" + safe_name
            warnings.add("Reserved JavaScript property names were adjusted in the Zotero display tree.")
        used = used_by_parent.setdefault(tuple(parent_path), set())
        candidate, number = safe_name, 2
        while candidate in used:
            candidate = f"{safe_name} ({number})"
            number += 1
        if candidate != safe_name:
            warnings.add("Duplicate sibling folder names were numbered in the Zotero display tree.")
        used.add(candidate)
        paths[folder["id"]] = parent_path + [candidate]
        fields = [f"{level} ExplicitGroup:{candidate}", "0"]
        fields.extend(keys_by_doc[did] for did in folder_members[folder["id"]])
        record = ";".join(_jabref_escape(field) for field in fields) + ";"
        records.append(_jabref_escape(record))
    for warning in sorted(warnings):
        print("  ! " + warning + " Exact names/hierarchy remain in the embedded Mendeley manifest.")
    return ("@comment{jabref-meta: groupsversion:3;}\n"
            "@comment{jabref-meta: groupstree:\n" + ";\n".join(records) + ";\n}\n")


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


def render_bib_entry(entry, key, document_id, attachments, folder_ids, folder_paths):
    kind, _, fields = entry
    # Remove API attachment paths: only successful local downloads may be linked.
    fields = [f for f in fields if not re.match(
        r"^(file|mendeley_id|mendeley_folder_ids|mendeley_folder_paths|mendeley_unverified_attachments)\s*=", f, re.I)]
    fields.append("mendeley_id = {" + bib_value(document_id) + "}")
    if folder_ids:
        fields.append("mendeley_folder_ids = {" + bib_value("; ".join(folder_ids)) + "}")
        fields.append("mendeley_folder_paths = {" + bib_value("; ".join(folder_paths)) + "}")
    # MIME types make non-PDF attachments meaningful to Zotero as well.
    path = ";".join(f"{'UNVERIFIED' if a.get('verified') is False else ''}:{a['filename']}:{a['mime_type']}"
                    for a in attachments)
    if path:
        fields.append("file = {" + path + "}")
    unverified = [a['filename'] for a in attachments if a.get('verified') is False]
    if unverified:
        fields.append("mendeley_unverified_attachments = {" + bib_value('; '.join(unverified)) + "}")
    return "@" + kind + "{" + key + ",\n  " + ",\n  ".join(fields) + "\n}\n"


def fetch_bib_entry(token, document_id):
    did = urllib.parse.quote(document_id, safe="")
    def read_entry():
        with _open(token, f"{API}/documents/{did}?view=bib",
                   "application/x-bibtex") as response:
            return response.read().decode("utf-8-sig")
    text = retry_request(read_entry, f"BibTeX document {document_id}")
    return parse_bib_entry(text)


class BibCache:
    """Append each retrieved reference to a group-specific, resumable JSONL cache."""
    def __init__(self, dest, group_id):
        suffix = hashlib.sha256(str(group_id).encode("utf-8")).hexdigest()[:16]
        self.path = os.path.join(dest, f"_Mendeley_BibTeX_cache_{suffix}.jsonl")
        self.header = {"cache_version": 1, "group_id": group_id, "api": API}
        self.entries = {}
        if os.path.exists(self.path):
            bad = 0
            with open(self.path, "rb") as source:
                try:
                    header = json.loads(source.readline())
                except (ValueError, UnicodeError) as error:
                    raise ValueError(f"Invalid BibTeX cache header in {self.path}; "
                                     "rename the cache file and retry.") from error
                if header != self.header:
                    raise ValueError(f"BibTeX cache belongs to a different group/API or version: {self.path}")
                for line in source:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict) or not all(
                                isinstance(row.get(k), str) for k in ("id", "fingerprint", "bibtex")):
                            raise ValueError("Invalid cache record")
                        entry = parse_bib_entry(row["bibtex"])
                        self.entries[row["id"]] = (row["fingerprint"], entry)
                    except (ValueError, UnicodeError):
                        bad += 1
            if bad:
                print(f"  ! Ignored {bad} incomplete/invalid BibTeX cache records; they will be fetched again.")
        else:
            with open(self.path, "x", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(self.header) + "\n")
        self.output = None

    @staticmethod
    def fingerprint(doc):
        payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, doc):
        cached = self.entries.get(doc["id"])
        return cached[1] if cached and cached[0] == self.fingerprint(doc) else None

    def save(self, doc, entry):
        kind, key, fields = entry
        text = "@" + kind + "{" + key + ",\n" + ",\n".join(fields) + "\n}"
        row = {"id": doc["id"], "fingerprint": self.fingerprint(doc), "bibtex": text}
        # A leading newline isolates a torn final record from a previous interruption.
        self.output.write("\n" + json.dumps(row, ensure_ascii=False) + "\n")
        self.output.flush()
        os.fsync(self.output.fileno())
        self.entries[doc["id"]] = (row["fingerprint"], entry)

    def __enter__(self):
        self.output = open(self.path, "a", encoding="utf-8", newline="\n")
        return self

    def __exit__(self, *args):
        self.output.close()


def is_pdf(attachment):
    mime = (attachment.get("mime_type") or "").split(";", 1)[0].lower()
    return mime == "application/pdf" or (
        attachment.get("file_name") or "").lower().endswith(".pdf")


def attachment_mime(attachment):
    mime = (attachment.get("mime_type") or "").split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_type(attachment.get("file_name") or "")[0]
    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime) or mime == "application/octet-stream":
        mime = guessed or "application/octet-stream"
    return mime


def attachment_extension(attachment):
    original = attachment.get("file_name") or ""
    # Preserve compound archive suffixes without retaining the original basename.
    lower = original.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".nii.gz"):
        if lower.endswith(suffix):
            return suffix
    extension = os.path.splitext(original)[1].lower()
    if re.fullmatch(r"\.[a-z0-9_-]{1,20}", extension):
        return extension
    return mimetypes.guess_extension(attachment_mime(attachment), strict=False) or ".bin"


def download_attachment(token, attachment, dest, filename):
    latest = None
    last_integrity = None

    def attempt():
        nonlocal latest, last_integrity
        try:
            return _download_attachment_once(token, attachment, dest, filename, retain_failed=True)
        except DownloadIntegrityError as error:
            if latest is not None:
                os.unlink(latest)
            latest = error.temporary_path
            error.temporary_path = None
            last_integrity = error
            raise

    try:
        return retry_request(attempt, f"Attachment {filename}")
    except (OSError, http.client.HTTPException, DownloadIntegrityError) as error:
        if latest is not None:
            stem, extension = os.path.splitext("_WARNING_UNVERIFIED_" + filename)
            used = {name.casefold() for name in os.listdir(dest)}
            warning_name = claim_filename(stem, extension, used)
            _copy_new_file(latest, os.path.join(dest, warning_name))
            last_integrity.details['saved_unverified_filename'] = warning_name
            if error is not last_integrity:
                last_integrity.details['last_retry_error'] = str(error)
                error.download_integrity = last_integrity
            print(f"  ! Saved unverified download: {warning_name}", flush=True)
        raise
    finally:
        if latest is not None:
            os.unlink(latest)


def _copy_new_file(source_path, target):
    """Copy exclusively, cleaning up only the output created by this operation."""
    created = False
    try:
        with open(source_path, "rb") as source, open(target, "xb") as output:
            created = True
            shutil.copyfileobj(source, output, length=1024 * 1024)
    except BaseException:
        if created:
            os.unlink(target)
        raise


def _download_attachment_once(token, attachment, dest, filename, retain_failed=False):
    """Stream arbitrary attachment bytes; leave no partial file after a failure."""
    fid = urllib.parse.quote(attachment["id"], safe="")
    with _open(token, f"{API}/files/{fid}",
               "application/vnd.mendeley-file.1+json", follow=False) as response:
        location = response.headers.get("Location")
        if not location:
            raise ValueError("API response has no signed download URL.")
    location = urllib.parse.urljoin(API + "/", location)
    request = urllib.request.Request(location, headers={"User-Agent": USER_AGENT,
                                                       "Accept-Encoding": "identity"})
    tmp_name = None
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            response_type = response.headers.get("Content-Type", "")
            response_encoding = response.headers.get("Content-Encoding", "")
            with tempfile.NamedTemporaryFile(dir=dest, delete=False) as output:
                tmp_name = output.name
                shutil.copyfileobj(response, output, length=1024 * 1024)
        expected_size = attachment.get("size")
        actual_size = os.path.getsize(tmp_name)
        details = {"file_id": attachment["id"], "expected_size": expected_size,
                   "actual_size": actual_size, "response_content_type": response_type,
                   "response_content_encoding": response_encoding}
        reason = None
        if expected_size is not None and actual_size != int(expected_size):
            reason = "Downloaded size differs from the API file size."
        if reason:
            error = DownloadIntegrityError(reason, details)
            if retain_failed:
                error.temporary_path = tmp_name
                tmp_name = None  # ownership passes to download_attachment
            raise error
        _copy_new_file(tmp_name, os.path.join(dest, filename))
    finally:
        if tmp_name is not None:
            os.unlink(tmp_name)


def write_bib(dest, filename, ids, entries, groups, manifest):
    with open(os.path.join(dest, filename), "x", encoding="utf-8", newline="\n") as output:
        output.write("% Mendeley group export. Attachment paths are relative to this file.\n"
                     "% Zotero collections: JabRef groupsversion 3 comments below.\n"
                     "% Exact Mendeley folders/memberships: base64-encoded UTF-8 JSON manifest.\n\n")
        for document_id in ids:
            output.write(entries[document_id] + "\n")
        output.write(groups + "\n")
        encoded = base64.b64encode(json.dumps(manifest, ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8")).decode("ascii")
        output.write("@comment{mendeley-export-json-base64:" + encoded + "}\n")


def _fmt_authors(doc):
    out = []
    for a in doc.get("authors", []) or []:
        last = a.get("last_name", "")
        first = a.get("first_name", "")
        out.append(f"{last}, {first}".strip(", ").strip())
    return "; ".join(out)


META_COLUMNS = ["title", "authors", "year", "type", "source",
                "doi", "isbn", "issn", "pmid", "has_pdf", "pdf_filename", "id",
                "bibtex_id", "has_files", "filenames", "folder_paths", "unverified_filenames"]


def _doc_row(doc, attachments_by_doc, keys_by_doc, paths_by_doc):
    ids = doc.get("identifiers", {}) or {}
    attachments = attachments_by_doc.get(doc["id"], [])
    pdfs = [a["filename"] for a in attachments if a["mime_type"] == "application/pdf"
            or a["filename"].lower().endswith(".pdf")]
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
        "bibtex_id": keys_by_doc[doc["id"]],
        "has_files": "yes" if attachments else "no",
        "filenames": "; ".join(a["filename"] for a in attachments),
        "folder_paths": "; ".join(paths_by_doc[doc["id"]]),
        "unverified_filenames": "; ".join(a['filename'] for a in attachments if a.get('verified') is False),
    }


def write_metadata(dest, docs, attachments_by_doc, keys_by_doc, paths_by_doc, used):
    """Write references.csv and references.json for every document in the group."""
    rows = [_doc_row(d, attachments_by_doc, keys_by_doc, paths_by_doc) for d in docs]
    rows.sort(key=lambda r: (str(r["year"] or ""), (r["title"] or "").lower()))

    csv_path = os.path.join(dest, claim_filename("references", ".csv", used))
    with open(csv_path, "x", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=META_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    json_path = os.path.join(dest, claim_filename("references", ".json", used))
    with open(json_path, "x", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False, indent=2)

    with_files = sum(1 for r in rows if r["has_files"] == "yes")
    print(f"\nMetadata: {len(rows)} references written "
          f"({with_files} with downloaded attachments, {len(rows) - with_files} reference-only)")
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
    files.sort(key=lambda f: (f.get("document_id") or "", f["id"]))
    for attachment in files:
        if attachment.get("document_id") not in docs_by_id:
            raise ValueError("An attachment belongs to a reference absent from the group. "
                             "The group may have changed during export; please retry.")
    if len({f["id"] for f in files}) != len(files):
        raise ValueError("Duplicate attachment IDs returned by the API. Please retry.")
    print(f"Files attached: {len(files)} ({sum(is_pdf(f) for f in files)} PDFs)\n")

    folders = list(paged(token, "/folders", "application/vnd.mendeley-folder.1+json",
                         {"group_id": gid, "limit": 500}))
    folders = ordered_folders(folders)
    folders_by_id = {f["id"]: f for f in folders}
    folder_members = {}
    folders_by_doc = {d["id"]: [] for d in docs}
    paths_by_doc = {d["id"]: [] for d in docs}
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
        path = " | ".join(folder_path(folder, folders_by_id))
        for document_id in folder_members[folder["id"]]:
            folders_by_doc[document_id].append(folder["id"])
            paths_by_doc[document_id].append(path)
    print(f"Folders in group: {len(folders)}")

    dest = destination or os.path.join(
        os.path.expanduser("~"), "Desktop", "Mendeley-export", sanitize(group["name"]))
    os.makedirs(dest, exist_ok=True)

    # Resolve final, unique citation keys before downloading: the failure report
    # must use exactly the keys that end up in the bibliography.
    print("\nRetrieving BibTeX entries...")
    api_entries, keys_by_doc, keys = {}, {}, set()
    with BibCache(dest, gid) as cache:
        reused = 0
        print(f"BibTeX cache: {cache.path}")
        for index, doc in enumerate(docs, 1):
            entry = cache.get(doc)
            if entry is not None:
                reused += 1
            else:
                try:
                    entry = fetch_bib_entry(token, doc["id"])
                except (OSError, ValueError, http.client.HTTPException) as error:
                    raise OSError(f"BibTeX retrieval failed at {index}/{len(docs)} "
                                  f"(document {doc['id']}, title: {doc.get('title') or 'Untitled'}): {error}. "
                                  f"Completed entries are cached in {cache.path}. "
                                  "Run again with the same --dest to resume BibTeX retrieval.") from error
                cache.save(doc, entry)
            preferred = doc.get("citation_key") or entry[1]
            base = re.sub(r"[^A-Za-z0-9_:.-]", "_", str(preferred)) or "reference"
            key, number = base, 2
            while key in keys:
                key = f"{base}_{number}"
                number += 1
            keys.add(key)
            api_entries[doc["id"]] = entry
            keys_by_doc[doc["id"]] = key
            if index == 1 or index % 25 == 0 or index == len(docs):
                print(f"  {index}/{len(docs)} references ({reused} from cache)", flush=True)

    print(f"Downloading into: {dest}\n")

    used = {name.casefold() for name in os.listdir(dest)}
    attachments_by_doc, failures = {}, []
    ok = 0
    unverified_count = 0
    report_name = claim_filename(LEGAL_REPORT, "", used)
    with open(os.path.join(dest, report_name), "x", encoding="utf-8", newline="") as report:
        writer = csv.writer(report, delimiter="\t", lineterminator="\n")
        writer.writerow(["BibTeX ID", "Filename"])
        report.flush()
        for f in files:
            document_id = f["document_id"]
            fname = claim_filename(attachment_stem(docs_by_id[document_id]),
                                   attachment_extension(f), used)
            try:
                download_attachment(token, f, dest, fname)
                ok += 1
                attachments_by_doc.setdefault(document_id, []).append({
                    "file_id": f["id"], "filename": fname, "mime_type": attachment_mime(f), "verified": True})
                print(f"  + {fname}")
            except (OSError, ValueError, http.client.HTTPException) as e:
                status = e.code if isinstance(e, urllib.error.HTTPError) else None
                failures.append({"document_id": document_id, "bibtex_id": keys_by_doc[document_id],
                                 "file_id": f["id"], "filename": fname,
                                 "http_status": status, "error": str(e)})
                integrity = e if isinstance(e, DownloadIntegrityError) else getattr(e, 'download_integrity', None)
                if integrity is not None:
                    failures[-1]["integrity"] = integrity.details
                    failures[-1]["integrity_attempts"] = integrity.attempts
                    warning_name = integrity.details.get('saved_unverified_filename')
                    if warning_name:
                        unverified_count += 1
                        used.add(warning_name.casefold())
                        attachments_by_doc.setdefault(document_id, []).append({
                            "file_id": f['id'], "filename": warning_name,
                            "mime_type": attachment_mime(f), "verified": False})
                if status == 451:
                    writer.writerow([keys_by_doc[document_id], fname])
                    report.flush()
                print(f"  ! failed {fname}: {e}")

    print(f"\nAttachments downloaded: {ok}/{len(files)}")
    if unverified_count:
        print(f"Unverified attachments retained for inspection: {unverified_count}")
    print(f"  - {report_name} ({sum(f['http_status'] == 451 for f in failures)} HTTP 451 failures)")
    write_metadata(dest, docs, attachments_by_doc, keys_by_doc, paths_by_doc, used)

    entries = {d["id"]: render_bib_entry(
        api_entries[d["id"]], keys_by_doc[d["id"]], d["id"],
        attachments_by_doc.get(d["id"], []), folders_by_doc[d["id"]], paths_by_doc[d["id"]])
        for d in docs}
    manifest = {
        "schema": "mendeley-group-export", "version": 1,
        "group": group, "folders": folders, "folder_members": folder_members,
        "documents": [{"id": d["id"], "bibtex_id": keys_by_doc[d["id"]],
                       "folder_ids": folders_by_doc[d["id"]],
                       "attachments": attachments_by_doc.get(d["id"], [])} for d in docs],
        "download_failures": failures,
    }

    group_bib = claim_filename("__" + sanitize(group["name"]), ".bib", used)
    write_bib(dest, group_bib, [d["id"] for d in docs], entries,
              zotero_groups(folders, folder_members, keys_by_doc), manifest)
    print(f"  + {group_bib} ({len(docs)} references)")
    print(f"\nDone. One bibliography with {len(folders)} folders written to {dest}")
    if ok != len(files):
        print("Some attachments failed to download or verify. Files that failed integrity checks "
              "are retained with a warning prefix and linked as UNVERIFIED; missing files have no links.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, http.client.HTTPException) as error:
        sys.exit(f"Export failed: {error}")
    except KeyboardInterrupt:
        sys.exit("Export interrupted.")
