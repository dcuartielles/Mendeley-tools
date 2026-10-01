# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline integration checks: run with python -m unittest -v test_mendeley_export.py."""
import contextlib
import base64
import csv
import hashlib
import email.utils
import http.server
import importlib.util
import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest.mock import Mock, patch

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "exporter", HERE / "mendeley_group_export_with-bib.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

PDF = b"%PDF-1.4\n% offline fixture\n%%EOF\n"
XLSX = b"PK\x03\x04 arbitrary non-PDF fixture"
DOCS = [
    {"id": "d1", "title": 'Trial {A}, with "quotes"', "year": 2024,
     "authors": [{"last_name": "Müller", "first_name": "Anna"}]},
    {"id": "d2", "title": 'Trial {A}, with "quotes"', "year": 2024,
     "authors": [{"last_name": "Müller", "first_name": "Max"}]},
    {"id": "d3", "title": "No PDF", "year": 2025},
    {"id": "d4", "title": "Unfiled", "year": 2025},
]
FILES = [
    {"id": "p1", "document_id": "d1", "file_name": "one.pdf",
     "mime_type": "application/pdf", "size": len(PDF), "filehash": hashlib.sha1(PDF).hexdigest()},
    {"id": "p2", "document_id": "d1", "file_name": "two.PDF",
     "mime_type": "application/octet-stream", "size": len(PDF), "filehash": hashlib.sha1(PDF).hexdigest()},
    {"id": "p3", "document_id": "d2", "mime_type": "application/pdf", "size": len(PDF),
     "filehash": hashlib.sha1(PDF).hexdigest()},
    {"id": "other", "document_id": "d3", "file_name": "notes.xlsx", "size": len(XLSX)},
]
FOLDERS = [
    {"id": "child", "name": "Child", "parent_id": "parent"},
    {"id": "parent", "name": "Parent"},
    {"id": "empty", "name": "Empty"},
    {"id": "duplicate", "name": "Parent"},
]
MEMBERS = {"parent": [{"id": "d1"}], "child": [{"id": "d2"}, {"id": "d3"}],
           "empty": [], "duplicate": [{"id": "d4"}]}


class Handler(http.server.BaseHTTPRequestHandler):
    failed_pdf = None
    damaged_pdf = None
    missing_member = False
    legal_file = None
    legal_stage = None
    requests = []
    transient = {}
    damage_remaining = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.requests.append((parsed.path, params, dict(self.headers)))
        fault = self.transient.get(self.path)
        if fault and fault[1] > 0:
            code, remaining = fault
            self.transient[self.path] = (code, remaining - 1)
            self.send_response(code)
            self.send_header('Retry-After', '7')
            self.end_headers()
            return
        if parsed.path.startswith("/download/"):
            # The token must not be sent to the signed download destination.
            if self.headers.get("Authorization"):
                self.send_error(403)
                return
            if self.legal_stage == "download" and parsed.path.rsplit("/", 1)[-1] == self.legal_file:
                self.send_error(451)
                return
            if parsed.path.rsplit("/", 1)[-1] == self.failed_pdf:
                self.send_error(503)
                return
            self.send_response(200)
            self.end_headers()
            damage = self.damaged_pdf if self.damage_remaining is None or self.damage_remaining > 0 else None
            if parsed.path.endswith('/p3') and damage and self.damage_remaining is not None:
                type(self).damage_remaining -= 1
            if parsed.path.endswith("/p3") and damage == "short":
                payload = PDF[:-5]
            elif parsed.path.endswith("/p3") and damage == "html":
                payload = b"x" * len(PDF)
            elif parsed.path.endswith("/other"):
                payload = XLSX
            else:
                payload = PDF
            self.wfile.write(payload)
            return
        if self.headers.get("Authorization") != "Bearer test-token":
            self.send_error(401)
            return
        if parsed.path.startswith("/files/"):
            if self.legal_stage == "api" and parsed.path.rsplit("/", 1)[-1] == self.legal_file:
                self.send_error(451)
                return
            self.send_response(303)
            self.send_header("Location", exporter.API + "/download/" + parsed.path.rsplit("/", 1)[-1])
            self.end_headers()
            return
        if parsed.path.startswith("/documents/"):
            if params.get("view") != ["bib"] or self.headers.get("Accept") != "application/x-bibtex":
                self.send_error(406)
                return
            did = parsed.path.rsplit("/", 1)[-1]
            text = ('@article{SameKey,\n'
                    ' title = {Nested {braces}, commas and a word file = {bogus}},\n'
                    ' author = "Müller, Anna and Someone, Bob",\n'
                    ' note = "Quoted, comma and {protected "quotes", comma}",\n'
                    ' doi = {10.1234/test\\_id},\n'
                    ' file = {C:/stale/path.pdf},\n'
                    ' mendeley_id = {old-id},\n'
                    ' year = {2024}\n}\n')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))
            return
        if parsed.path == "/groups":
            items = [{"id": "g1", "name": "My Group"}]
        elif parsed.path in ("/documents", "/files", "/folders"):
            if params.get("group_id") != ["g1"]:
                self.send_error(400)
                return
            items = {"/documents": DOCS, "/files": FILES, "/folders": FOLDERS}[parsed.path]
        elif parsed.path.startswith("/folders/"):
            fid = parsed.path.split("/")[2]
            items = MEMBERS[fid]
            if self.missing_member and fid == "parent":
                items = [{"id": "absent"}]
        else:
            self.send_error(404)
            return
        # Force multiple pages even though the client requests limit=500.
        offset = int(params.get("marker", ["0"])[0])
        page = items[offset:offset + 1]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if offset + 1 < len(items):
            query = {key: value[0] for key, value in params.items()}
            query["marker"] = offset + 1
            link = exporter.API + parsed.path + "?" + urllib.parse.urlencode(query)
            self.send_header("Link", '<' + link + '>; rel="next"')
        self.end_headers()
        self.wfile.write(json.dumps(page).encode())


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        Handler.requests = []
        Handler.failed_pdf = None
        Handler.damaged_pdf = None
        Handler.missing_member = False
        Handler.legal_file = None
        Handler.legal_stage = None
        Handler.transient = {}
        Handler.damage_remaining = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = pathlib.Path(self.tmp.name)
        api = f"http://127.0.0.1:{self.server.server_port}"
        self.api_patch = patch.object(exporter, "API", api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.wait_patch = patch.object(exporter, '_wait_before_retry')
        self.wait = self.wait_patch.start()
        self.addCleanup(self.wait_patch.stop)

    def run_export(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return exporter.export_group("test-token", {"id": "g1", "name": "My Group"}, str(self.dest))

    def manifest(self):
        text = (self.dest / "__My Group.bib").read_text("utf-8")
        encoded = re.search(r"@comment\{mendeley-export-json-base64:([^}]+)\}", text)[1]
        return json.loads(base64.b64decode(encoded))

    def test_complete_flat_export_and_links(self):
        self.assertEqual(self.run_export(), 0)
        self.assertTrue(all(p.is_file() for p in self.dest.iterdir()))
        self.assertEqual(len(list(self.dest.glob("*.pdf"))), 3)
        names = {p.name for p in self.dest.glob("*.pdf")}
        self.assertEqual(names, {
            "Müller2024_Trial _A_, with _quotes_.pdf",
            "Müller2024_Trial _A_, with _quotes_ (2).pdf",
            "Müller2024_Trial _A_, with _quotes_ (3).pdf",
        })
        group = (self.dest / "__My Group.bib").read_text("utf-8")
        self.assertEqual(group.count("@article{"), 4)
        self.assertIn("@article{SameKey_4,", group)
        for did in ("d1", "d2", "d3", "d4"):
            self.assertEqual(group.count("mendeley_id = {" + did + "}"), 1)
        self.assertNotIn("C:/stale/path.pdf", group)
        self.assertNotIn("mendeley_id = {old-id}", group)
        self.assertIn("word file = {bogus}", group)  # nested title text preserved
        self.assertIn("10.1234/test\\_id", group)
        self.assertIn(":Müller2024_Trial _A_, with _quotes_.pdf:application/pdf;", group)
        self.assertEqual(len(list(self.dest.glob("*.bib"))), 1)
        xlsx = self.dest / "UnknownAuthor2025_No PDF.xlsx"
        self.assertEqual(xlsx.read_bytes(), XLSX)
        self.assertIn(":UnknownAuthor2025_No PDF.xlsx:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", group)
        self.assertIn("mendeley_folder_paths = {Parent | Child}", group)
        self.assertIn("@comment{jabref-meta: groupsversion:3;}", group)
        manifest = self.manifest()
        self.assertEqual({f['id']: f for f in manifest['folders']}, {f['id']: f for f in FOLDERS})
        self.assertEqual(manifest['folder_members']['child'], ['d2', 'd3'])
        self.assertEqual(manifest['folder_members']['empty'], [])
        self.assertEqual(manifest['download_failures'], [])
        self.assertEqual(sum(len(d['attachments']) for d in manifest['documents']), 4)
        self.assertEqual((self.dest / exporter.LEGAL_REPORT).read_text('utf-8'), 'BibTeX ID\tFilename\n')
        rows = list(csv.DictReader(io.StringIO((self.dest / "references.csv").read_text("utf-8-sig"))))
        self.assertEqual(len(rows), 4)
        self.assertEqual(next(r for r in rows if r['id'] == 'd3')['has_pdf'], 'no')
        self.assertEqual(next(r for r in rows if r['id'] == 'd3')['has_files'], 'yes')
        self.assertEqual(len(json.loads((self.dest / "references.json").read_text("utf-8"))), 4)
        self.assertTrue(any("marker" in params for path, params, _ in Handler.requests))

    def test_existing_files_survive_repeated_export(self):
        self.assertEqual(self.run_export(), 0)
        before = {p.name: p.read_bytes() for p in self.dest.iterdir()}
        self.assertEqual(self.run_export(), 0)
        for name, contents in before.items():
            self.assertEqual((self.dest / name).read_bytes(), contents)
        self.assertTrue((self.dest / "__My Group (2).bib").exists())
        self.assertEqual(len(list(self.dest.glob("*.pdf"))), 6)

    def test_failed_download_not_linked(self):
        Handler.failed_pdf = "p3"
        self.assertEqual(self.run_export(), 1)
        self.assertEqual(len(list(self.dest.glob("*.pdf"))), 2)
        group = (self.dest / "__My Group.bib").read_text("utf-8")
        self.assertNotIn("(3).pdf", group)
        self.assertEqual(group.count("@article{"), 4)
        # Temporary files must be removed on failure.
        self.assertEqual(len(list(self.dest.iterdir())), 8)
        self.assertEqual((self.dest / exporter.LEGAL_REPORT).read_text('utf-8'), 'BibTeX ID\tFilename\n')

    def test_inconsistent_folder_membership_stops_export(self):
        Handler.missing_member = True
        with self.assertRaisesRegex(ValueError, "Folder membership changed"):
            self.run_export()
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_truncated_response_saved_with_warning(self):
        for damage in ("short",):
            with self.subTest(damage=damage):
                Handler.damaged_pdf = damage
                self.dest = pathlib.Path(self.tmp.name) / damage
                self.assertEqual(self.run_export(), 1)
                self.assertEqual(len(list(self.dest.glob("*.pdf"))), 3)
                text = (self.dest / "__My Group.bib").read_text("utf-8")
                failed = self.manifest()['download_failures'][0]
                self.assertNotIn(':' + failed['filename'] + ':', text)
                retained = failed['integrity']['saved_unverified_filename']
                self.assertIn('UNVERIFIED:' + retained + ':', text)
                self.assertTrue((self.dest / retained).is_file())
                self.assertEqual(text.count("@article{"), 4)

    def test_451_report_at_both_request_stages_and_all_file_types(self):
        for stage, file_id in [('api', 'p3'), ('download', 'p3'), ('download', 'other')]:
            with self.subTest(stage=stage, file_id=file_id):
                Handler.legal_stage, Handler.legal_file = stage, file_id
                self.dest = pathlib.Path(self.tmp.name) / (stage + file_id)
                self.assertEqual(self.run_export(), 1)
                report = list(csv.reader(io.StringIO((self.dest / exporter.LEGAL_REPORT).read_text('utf-8')), delimiter='\t'))
                self.assertEqual(report[0], ['BibTeX ID', 'Filename'])
                self.assertEqual(len(report), 2)
                key, filename = report[1]
                self.assertEqual(key, 'SameKey_2' if file_id == 'p3' else 'SameKey_3')
                self.assertFalse((self.dest / filename).exists())
                text = (self.dest / '__My Group.bib').read_text('utf-8')
                self.assertIn('@article{' + key + ',', text)
                self.assertNotIn(':' + filename + ':', text)
                failures = self.manifest()['download_failures']
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]['http_status'], 451)
                self.assertEqual(failures[0]['bibtex_id'], key)

    def test_multiple_451_attachments_for_same_reference(self):
        def blocked(token, attachment, dest, filename):
            raise urllib.error.HTTPError('https://example.test', 451, 'Unavailable For Legal Reasons', {}, None)
        with patch.object(exporter, 'download_attachment', side_effect=blocked):
            self.assertEqual(self.run_export(), 1)
        rows = list(csv.reader(io.StringIO((self.dest / exporter.LEGAL_REPORT).read_text('utf-8')), delimiter='\t'))
        self.assertEqual(len(rows), 5)
        self.assertEqual([r[0] for r in rows[1:]], ['SameKey', 'SameKey', 'SameKey_2', 'SameKey_3'])
        self.assertEqual(len({r[1] for r in rows[1:]}), 4)
        self.assertEqual(len(list(self.dest.glob('*.bib'))), 1)

    def test_all_attachment_types_and_missing_extensions(self):
        fixtures = [({'file_name': 'document.docx'}, '.docx'),
                    ({'file_name': 'data.csv'}, '.csv'),
                    ({'file_name': 'archive.tar.gz'}, '.tar.gz'),
                    ({'mime_type': 'application/pdf'}, '.pdf'),
                    ({'mime_type': 'image/png'}, '.png'),
                    ({}, '.bin')]
        for attachment, extension in fixtures:
            self.assertEqual(exporter.attachment_extension(attachment), extension)
        self.assertEqual(exporter.attachment_mime({'file_name': 'one.PDF', 'mime_type': 'application/octet-stream'}), 'application/pdf')
        arbitrary = [{'id': 'other', 'document_id': 'd3', 'file_name': 'data.custom', 'size': len(XLSX),
                      'filehash': hashlib.sha1(XLSX).hexdigest()}]
        with patch.dict(Handler.do_GET.__globals__, {'FILES': arbitrary}):
            self.assertEqual(self.run_export(), 0)
        self.assertEqual((self.dest / 'UnknownAuthor2025_No PDF.custom').read_bytes(), XLSX)

    def test_folder_root_sentinel_and_missing_parent(self):
        root = {'id': 'f', 'name': 'Folder', 'parent_id': 'root'}
        self.assertEqual(exporter.folder_path(root, {'f': root}), ['Folder'])
        self.assertEqual(exporter.ordered_folders([root]), [root])
        with self.assertRaisesRegex(ValueError, 'Missing parent'):
            exporter.ordered_folders([dict(root, parent_id='missing')])

    def test_special_folder_names_preserved_in_manifest(self):
        tricky = [{'id': 'r', 'name': 'Semicolon; Back\\slash {braces}\ntext', 'parent_id': 'root'},
                  {'id': 's', 'name': '__proto__', 'parent_id': 'r'}]
        with patch.dict(Handler.do_GET.__globals__, {'FOLDERS': tricky, 'MEMBERS': {'r': [{'id': 'd1'}], 's': [{'id': 'd1'}]}}):
            self.assertEqual(self.run_export(), 0)
        self.assertEqual(self.manifest()['folders'], tricky)
        self.assertEqual(self.manifest()['folder_members'], {'r': ['d1'], 's': ['d1']})

    def test_attachment_outside_group_stops_before_output(self):
        with patch.dict(Handler.do_GET.__globals__, {'FILES': [dict(FILES[0], document_id='absent')]}):
            with self.assertRaisesRegex(ValueError, 'attachment belongs'):
                self.run_export()
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_empty_group(self):
        with patch.object(exporter, "paged", return_value=iter(())):
            self.assertEqual(self.run_export(), 0)
        self.assertTrue((self.dest / "__My Group.bib").exists())
        self.assertEqual(list(self.dest.glob("*.pdf")), [])

    def test_cli_and_list_groups(self):
        cli = subprocess.run([sys.executable, str(HERE / "mendeley_group_export_with-bib.py"), "--help"],
                             capture_output=True, text=True, check=True)
        self.assertNotIn("--include-metadata", cli.stdout)
        self.assertIn("--group", cli.stdout)
        self.assertIn("--dest", cli.stdout)
        with patch.dict("os.environ", {"MENDELEY_CLIENT_ID": "test", "MENDELEY_CLIENT_SECRET": "test"}), \
             patch.object(exporter, "get_access_token", return_value="test-token"), \
             patch.object(sys, "argv", ["export", "--list-groups", "--group", "My Group"]), \
             patch.object(exporter, "export_group") as export, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(exporter.main(), 0)
            export.assert_not_called()

    def test_filename_limits_and_hierarchy_cycles(self):
        self.assertEqual(exporter.sanitize("CON"), "_CON")
        self.assertLessEqual(len(exporter.sanitize("文" * 200).encode()), 180)
        self.assertEqual(exporter.attachment_stem({}), "UnknownAuthorn.d._Untitled")
        with self.assertRaisesRegex(ValueError, "Cycle"):
            exporter.folder_path({"id": "f", "name": "bad", "parent_id": "f"},
                                 {"f": {"id": "f", "name": "bad", "parent_id": "f"}})

    def test_timeout_and_reset_retried_with_backoff(self):
        action = Mock(side_effect=[urllib.error.URLError(TimeoutError('WinError 10060')),
                                   ConnectionResetError('connection reset'), 'ok'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(exporter.retry_request(action, 'fixture'), 'ok')
        self.assertEqual(action.call_count, 3)
        self.assertEqual([c.args[0] for c in self.wait.call_args_list], [2, 4])

    def test_rate_limit_and_service_failure_recover(self):
        Handler.transient = {'/documents/d2?view=bib': (429, 1), '/download/p3': (503, 1)}
        self.assertEqual(self.run_export(), 0)
        self.assertEqual([c.args[0] for c in self.wait.call_args_list], [7, 7])
        self.assertEqual(len(list(self.dest.glob('*.pdf'))), 3)
        self.assertEqual(self.manifest()['download_failures'], [])

    def test_api_page_retried_without_losing_pagination(self):
        Handler.transient = {'/documents?group_id=g1&limit=500&view=all': (503, 1)}
        self.assertEqual(self.run_export(), 0)
        self.assertEqual(len(self.manifest()['documents']), 4)
        self.wait.assert_called_once_with(7)

    def test_timeout_during_body_read_retries_whole_reference(self):
        class BrokenResponse(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b'partial', 100)
        with patch.object(exporter, '_open', side_effect=[BrokenResponse(), io.BytesIO(b'@article{Key,title={Recovered}}')]) as opened, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(exporter.fetch_bib_entry('test-token', 'd1'), ('article', 'Key', ['title={Recovered}']))
        self.assertEqual(opened.call_count, 2)
        self.wait.assert_called_once_with(2)

    def test_permanent_http_errors_and_invalid_bib_not_retried(self):
        for code in (400, 401, 403, 404, 451):
            with self.subTest(code=code):
                error = urllib.error.HTTPError('https://example.test', code, 'permanent', {}, None)
                action = Mock(side_effect=error)
                with self.assertRaises(urllib.error.HTTPError):
                    exporter.retry_request(action, 'fixture')
                self.assertEqual(action.call_count, 1)
        with patch.object(exporter, '_open', return_value=io.BytesIO(b'not BibTeX')) as opened:
            with self.assertRaises(ValueError):
                exporter.fetch_bib_entry('test-token', 'd1')
            opened.assert_called_once()
        self.wait.assert_not_called()

    def test_retry_after_date_and_invalid_headers(self):
        error = urllib.error.HTTPError('https://example.test', 429, 'rate limit', {}, None)
        error.headers = {'Retry-After': email.utils.formatdate(time.time() + 150, usegmt=True)}
        self.assertGreater(exporter._retry_delay(error, 1), 148)
        for value in ('invalid', 'inf', '-100'):
            error.headers = {'Retry-After': value}
            self.assertEqual(exporter._retry_delay(error, 1), 2)

    def test_retry_exhaustion_then_resume_uses_completed_cache(self):
        Handler.transient = {'/documents/d3?view=bib': (503, 20)}
        with self.assertRaisesRegex(OSError, r'3/4.*document d3.*cached'):
            self.run_export()
        calls = [path for path, _, _ in Handler.requests if path == '/documents/d3']
        self.assertEqual(len(calls), exporter.REQUEST_ATTEMPTS)
        self.assertEqual(len(list(self.dest.glob('*.bib'))), 0)
        self.assertEqual(len(list(self.dest.glob('_Mendeley_BibTeX_cache_*.jsonl'))), 1)
        Handler.transient = {}
        Handler.requests = []
        self.assertEqual(self.run_export(), 0)
        requests = [path for path, _, _ in Handler.requests if path.startswith('/documents/')]
        self.assertEqual(requests, ['/documents/d3', '/documents/d4'])
        self.assertEqual({d['bibtex_id'] for d in self.manifest()['documents']}, {'SameKey', 'SameKey_2', 'SameKey_3', 'SameKey_4'})

    def test_cache_reused_and_metadata_changes_invalidated(self):
        self.assertEqual(self.run_export(), 0)
        Handler.requests = []
        changed = [dict(d) for d in DOCS]
        changed[0]['title'] = 'Updated title'
        with patch.dict(Handler.do_GET.__globals__, {'DOCS': changed}):
            self.assertEqual(self.run_export(), 0)
        requests = [path for path, _, _ in Handler.requests if path.startswith('/documents/')]
        self.assertEqual(requests, ['/documents/d1'])

    def test_torn_cache_record_recovers_and_preserves_later_records(self):
        self.assertEqual(self.run_export(), 0)
        cache_path = next(self.dest.glob('_Mendeley_BibTeX_cache_*.jsonl'))
        with cache_path.open('ab') as output:
            output.write(b'{"id":"torn","bibtex":"\xe2')
        doc = dict(DOCS[0], title='Changed')
        entry = ('article', 'NewKey', ['title={New}'])
        with contextlib.redirect_stdout(io.StringIO()):
            with exporter.BibCache(str(self.dest), 'g1') as cache:
                cache.save(doc, entry)
            with exporter.BibCache(str(self.dest), 'g1') as cache:
                self.assertEqual(cache.get(doc), entry)
                self.assertIsNotNone(cache.get(DOCS[1]))

    def test_cache_is_group_specific_and_header_is_validated(self):
        with exporter.BibCache(str(self.dest), 'g1') as one:
            one.save(DOCS[0], ('article', 'Key', ['title={Fixture}']))
        with exporter.BibCache(str(self.dest), 'g2') as two:
            self.assertNotEqual(one.path, two.path)
            self.assertIsNone(two.get(DOCS[0]))
        pathlib.Path(one.path).write_text('invalid header', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Invalid BibTeX cache header'):
            exporter.BibCache(str(self.dest), 'g1')

    def test_checksum_mismatch_ignored_when_file_size_matches(self):
        files = [dict(f) for f in FILES]
        files[2]['filehash'] = '0' * 40
        with patch.dict(Handler.do_GET.__globals__, {'FILES': files}), \
             patch.object(exporter.hashlib, 'sha1', side_effect=AssertionError('Attachment checksum must not be calculated')):
            self.assertEqual(self.run_export(), 0)
        self.assertEqual(len(list(self.dest.glob('*.pdf'))), 3)
        self.assertEqual(self.manifest()['download_failures'], [])
        self.assertEqual(sum(path == '/files/p3' for path, _, _ in Handler.requests), 1)
        self.assertEqual(sum(path == '/download/p3' for path, _, _ in Handler.requests), 1)
        self.assertEqual(list(self.dest.glob('_WARNING_UNVERIFIED_*')), [])
        self.wait.assert_not_called()

    def test_size_failure_then_good_download_recovers(self):
        Handler.damaged_pdf = 'short'
        Handler.damage_remaining = 1
        self.assertEqual(self.run_export(), 0)
        self.assertEqual(self.manifest()['download_failures'], [])
        self.assertEqual(sum(path == '/files/p3' for path, _, _ in Handler.requests), 2)
        self.wait.assert_called_once_with(2)

    def test_persistent_size_failure_keeps_diagnostics_and_warning_link(self):
        Handler.damaged_pdf = 'short'
        self.assertEqual(self.run_export(), 1)
        failure = self.manifest()['download_failures'][0]
        self.assertEqual(failure['file_id'], 'p3')
        details = failure['integrity']
        self.assertEqual(details['expected_size'], len(PDF))
        self.assertEqual(details['actual_size'], len(PDF) - 5)
        self.assertNotIn('expected_sha1', details)
        self.assertNotIn('actual_sha1', details)
        self.assertEqual(len(failure['integrity_attempts']), exporter.REQUEST_ATTEMPTS)
        self.assertEqual(len({a['actual_size'] for a in failure['integrity_attempts']}), 1)
        self.assertIn('6 failed size checks returned the same byte count', failure['error'])
        self.assertIn(f"expected size {len(PDF)}", failure['error'])
        self.assertIn(f"actual size {len(PDF)-5}", failure['error'])
        self.assertFalse((self.dest / failure['filename']).exists())
        retained = details['saved_unverified_filename']
        self.assertTrue(retained.startswith('_WARNING_UNVERIFIED_'))
        self.assertEqual((self.dest / retained).read_bytes(), PDF[:-5])
        bib = (self.dest / '__My Group.bib').read_text('utf-8')
        self.assertNotIn(':' + failure['filename'] + ':', bib)
        self.assertIn('UNVERIFIED:' + retained + ':', bib)
        self.assertIn('mendeley_unverified_attachments', bib)
        doc = next(d for d in self.manifest()['documents'] if d['id'] == 'd2')
        self.assertEqual(doc['attachments'][0]['verified'], False)
        self.assertEqual(doc['attachments'][0]['filename'], retained)
        rows = list(csv.DictReader(io.StringIO((self.dest / 'references.csv').read_text('utf-8-sig'))))
        self.assertEqual(next(r for r in rows if r['id'] == 'd2')['unverified_filenames'], retained)
        self.assertEqual(sum(path == '/download/p3' for path, _, _ in Handler.requests), exporter.REQUEST_ATTEMPTS)

    def test_size_only_check_supports_empty_and_large_binary_files(self):
        payloads = [b'', b'abc', b'a' * 1000000, bytes(range(256)) * (8192 + 7)]
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                redirect = io.BytesIO()
                redirect.headers = {'Location': 'https://example.test/fixture'}
                response = io.BytesIO(payload)
                response.headers = {'Content-Type': 'application/octet-stream'}
                attachment = {'id': 'fixture', 'size': len(payload), 'filehash': '0' * 40}
                filename = f'vector{index}.bin'
                with patch.object(exporter, '_open', return_value=redirect), \
                     patch.object(exporter.urllib.request, 'urlopen', return_value=response) as opened, \
                     patch.object(exporter.hashlib, 'sha1', side_effect=AssertionError('Attachment checksum must not be calculated')):
                    exporter.download_attachment('test-token', attachment, str(self.dest), filename)
                    self.assertEqual(opened.call_args.args[0].get_header('Accept-encoding'), 'identity')
                self.assertEqual((self.dest / filename).read_bytes(), payload)
        self.wait.assert_not_called()

    def test_only_integrity_value_errors_are_retried(self):
        action = Mock(side_effect=ValueError('invalid metadata'))
        with self.assertRaisesRegex(ValueError, 'invalid metadata'):
            exporter.retry_request(action, 'fixture')
        action.assert_called_once()
        self.wait.assert_not_called()

    def test_unverified_file_collision_preserves_existing_file(self):
        name = '_WARNING_UNVERIFIED_Müller2024_Trial _A_, with _quotes_ (3).pdf'
        (self.dest / name).write_bytes(b'existing file')
        Handler.damaged_pdf = 'short'
        self.assertEqual(self.run_export(), 1)
        self.assertEqual((self.dest / name).read_bytes(), b'existing file')
        retained = self.manifest()['download_failures'][0]['integrity']['saved_unverified_filename']
        self.assertNotEqual(retained, name)
        self.assertEqual((self.dest / retained).read_bytes(), PDF[:-5])

    def test_last_received_unverified_file_survives_later_network_failures(self):
        bad = PDF[:-5]
        def redirect(*args, **kwargs):
            response = io.BytesIO()
            response.headers = {'Location': 'https://example.test/file'}
            return response
        response = io.BytesIO(bad)
        response.headers = {'Content-Type': 'application/pdf'}
        failures = [response] + [ConnectionResetError('reset') for _ in range(exporter.REQUEST_ATTEMPTS - 1)]
        with patch.object(exporter, '_open', side_effect=redirect), \
             patch.object(exporter.urllib.request, 'urlopen', side_effect=failures), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ConnectionResetError) as caught:
                exporter.download_attachment('test-token', FILES[0], str(self.dest), 'fixture.pdf')
        details = caught.exception.download_integrity.details
        self.assertEqual((self.dest / details['saved_unverified_filename']).read_bytes(), bad)
        self.assertEqual(len(list(self.dest.iterdir())), 1)
        self.assertFalse((self.dest / 'fixture.pdf').exists())

    def test_successful_retry_leaves_no_unverified_or_temporary_files(self):
        Handler.damaged_pdf = 'short'
        Handler.damage_remaining = 1
        self.assertEqual(self.run_export(), 0)
        self.assertEqual(list(self.dest.glob('_WARNING_UNVERIFIED_*')), [])
        self.assertEqual(len(list(self.dest.iterdir())), 9)

    def test_final_451_still_reported_after_unverified_file_was_retained(self):
        original = exporter._download_attachment_once
        count = 0
        def mixed(token, attachment, dest, filename, retain_failed=False):
            nonlocal count
            if attachment['id'] == 'p3':
                count += 1
                if count > 1:
                    raise urllib.error.HTTPError('https://example.test', 451, 'Unavailable For Legal Reasons', {}, None)
            return original(token, attachment, dest, filename, retain_failed=retain_failed)
        Handler.damaged_pdf = 'short'
        with patch.object(exporter, '_download_attachment_once', side_effect=mixed):
            self.assertEqual(self.run_export(), 1)
        failure = self.manifest()['download_failures'][0]
        self.assertEqual(failure['http_status'], 451)
        retained = failure['integrity']['saved_unverified_filename']
        self.assertTrue((self.dest / retained).exists())
        report = (self.dest / exporter.LEGAL_REPORT).read_text('utf-8')
        self.assertIn('SameKey_2\t' + failure['filename'], report)


if __name__ == "__main__":
    unittest.main()
