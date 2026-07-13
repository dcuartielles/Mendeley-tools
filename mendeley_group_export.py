#!/usr/bin/env python3
"""
Copy all files attached to a specific Mendeley group into a folder.

Uses the Mendeley REST API (api.mendeley.com) with OAuth2 authorization-code
flow. Standard library only -- no pip installs required.

Nothing in Mendeley's local data folder is touched; this only downloads
from the server into DEST_DIR.

Usage (PowerShell):
    $env:MENDELEY_CLIENT_ID     = "<your client id>"
    $env:MENDELEY_CLIENT_SECRET = "<your client secret>"
    python mendeley_group_export.py --group "My Group Name" --dest "C:/path/to/out"

If --group is omitted (or not found) the script prints all your groups and exits
so you can copy the exact name.
"""

import argparse
import base64
import http.server
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

API = "https://api.mendeley.com"
AUTHORIZE_URL = f"{API}/oauth/authorize"
TOKEN_URL = f"{API}/oauth/token"
REDIRECT_URI = "http://localhost:8080/callback"
REDIRECT_PORT = 8080
USER_AGENT = "Mendeley-tools/1.0 (+local export script)"


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
        _auth_code["code"] = qs.get("code", [None])[0]
        _auth_code["error"] = qs.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = ("Authorization received. You can close this tab and return to the terminal."
               if _auth_code.get("code") else
               f"Authorization failed: {_auth_code.get('error')}")
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *args):
        pass  # silence


def get_access_token(client_id, client_secret):
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "all",
    }
    url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    t = threading.Thread(target=server.handle_request)  # serve exactly one request
    t.start()

    print("Opening your browser to authorize with Mendeley...")
    print(f"If it doesn't open, paste this URL manually:\n  {url}\n")
    webbrowser.open(url)
    t.join(timeout=300)
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
        with urllib.request.urlopen(req) as resp:
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
        return urllib.request.urlopen(req)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(req)
    except urllib.error.HTTPError as e:
        return e  # 303 lands here; Location header is on e.headers


def paged(token, path, accept, params):
    """Yield items across all pages, following the Link: rel=next header."""
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    while url:
        resp = _open(token, url, accept, follow=True)
        data = json.load(resp)
        for item in data:
            yield item
        link = resp.headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None


# -------------------------------------------------------------------- main ---
def sanitize(name, maxlen=150):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name[:maxlen] or "untitled")


def main():
    ap = argparse.ArgumentParser()
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
        if not args.group:
            print("\nRe-run with --group \"<name>\" --dest \"<folder>\".")
            return

    match = [g for g in groups if g["name"].lower() == args.group.lower()]
    if not match:
        print(f'No group named "{args.group}". Available:')
        for g in groups:
            print(f"  - {g['name']}")
        sys.exit(1)
    group = match[0]
    gid = group["id"]
    print(f'Group: {group["name"]}  (id={gid})')

    # doc_id -> title, for naming
    titles = {}
    for d in paged(token, "/documents", "application/vnd.mendeley-document.1+json",
                   {"group_id": gid, "limit": 500}):
        titles[d["id"]] = d.get("title", d["id"])
    print(f"Documents in group: {len(titles)}")

    files = list(paged(token, "/files", "application/vnd.mendeley-file.1+json",
                       {"group_id": gid, "limit": 500}))
    print(f"Files attached: {len(files)}\n")

    dest = args.dest or os.path.join(
        os.path.expanduser("~"), "Desktop", "Mendeley-export", sanitize(group["name"]))
    os.makedirs(dest, exist_ok=True)
    print(f"Downloading into: {dest}\n")

    used = set()
    ok = 0
    for f in files:
        fid = f["id"]
        title = titles.get(f.get("document_id"), fid)
        orig = f.get("file_name", "")
        ext = os.path.splitext(orig)[1] or ".pdf"
        base = sanitize(title)
        fname = base + ext
        i = 2
        while fname.lower() in used:
            fname = f"{base} ({i}){ext}"
            i += 1
        used.add(fname.lower())
        out = os.path.join(dest, fname)

        # GET /files/{id} -> 303 with signed Location
        resp = _open(token, f"{API}/files/{fid}",
                     "application/vnd.mendeley-file.1+json", follow=False)
        loc = resp.headers.get("Location")
        if not loc:
            print(f"  ! no download URL for {fname} (HTTP {getattr(resp,'code','?')})")
            continue
        try:
            dlreq = urllib.request.Request(loc)
            dlreq.add_header("User-Agent", USER_AGENT)
            with urllib.request.urlopen(dlreq) as r, open(out, "wb") as w:
                w.write(r.read())
            ok += 1
            print(f"  + {fname}")
        except Exception as e:
            print(f"  ! failed {fname}: {e}")

    print(f"\nDone. {ok}/{len(files)} files copied to {dest}")


if __name__ == "__main__":
    main()
