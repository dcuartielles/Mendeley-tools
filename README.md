# Mendeley Group Export with BibTeX

`mendeley_group_export_with-bib.py` exports a Mendeley group into a portable local backup containing:

- all references in a single BibTeX file,
- all downloadable attachment types, not only PDFs,
- relative attachment links in the BibTeX entries,
- Mendeley folder membership information,
- Zotero-compatible collection metadata,
- an exact machine-readable copy of the Mendeley folder hierarchy,
- CSV and JSON metadata,
- a report of attachments blocked with HTTP 451,
- retry handling for temporary network problems,
- file-size validation for downloaded attachments, and
- a persistent BibTeX cache that allows interrupted bibliography retrieval to continue.

The exporter uses the Mendeley REST API and OAuth2. It does not modify Mendeley's local files or database.

The script is based on [Mendeley-tools](https://github.com/dcuartielles/Mendeley-tools) by David Cuartielles and is licensed under GPL-3.0-or-later. See `LICENSE`.

## Requirements

The exporter uses only the Python standard library. No Python packages need to be installed for normal operation.

Recommended environment:

```text
Python 3.8 or newer
uv
A Mendeley account
A Mendeley API application with client ID and client secret
```

The script has primarily been used from Windows PowerShell, but it does not depend on Windows-specific Python libraries.

## Mendeley API application

Create a Mendeley API application and configure this redirect URL:

```text
http://localhost:8080/callback
```

The exporter starts a temporary local HTTP listener on port `8080` while authentication is in progress.

Store the application's credentials in a local `.env` file:

```dotenv
MENDELEY_CLIENT_ID=your-client-id
MENDELEY_CLIENT_SECRET=your-client-secret
```

Do not commit `.env` to version control.

A suitable `.gitignore` entry is:

```gitignore
.env
```

The browser is opened for Mendeley authorization when the script starts. If the browser cannot be opened automatically, the authorization URL is printed so that it can be opened manually.

The access token, client ID and client secret are not written to the export directory.

## Running the exporter

With `uv`, run:

```powershell
uv run --env-file .env mendeley_group_export_with-bib.py `
  --group "Exact Group Name" `
  --dest "C:\MendeleyBackup"
```

Group matching is case-insensitive but otherwise exact.

To list the available groups:

```powershell
uv run --env-file .env mendeley_group_export_with-bib.py --list-groups
```

Running the script without `--group` also lists the available groups:

```powershell
uv run --env-file .env mendeley_group_export_with-bib.py
```

If `--dest` is omitted, the default destination is:

```text
~/Desktop/Mendeley-export/<group name>
```

The complete command-line interface is:

```text
--group <name>       Exact Mendeley group name, matched case-insensitively
--dest <directory>   Destination directory
--list-groups        List available groups and exit
```

## Export directory

All generated files are written directly into the destination directory. The Mendeley folder hierarchy is stored as metadata; it is not reproduced as directories on disk.

A typical export looks like this:

```text
MendeleyBackup/
├── __My Group.bib
├── _Mendeley_BibTeX_cache_0123456789abcdef.jsonl
├── _No PDF written - Unavailable For Legal Reasons
├── references.csv
├── references.json
├── Smith2024_Study title.pdf
├── Smith2024_Study title.docx
├── Smith2024_Study title.xlsx
└── ...
```

If a downloaded file repeatedly fails the file-size validation, the received bytes are retained for inspection:

```text
_WARNING_UNVERIFIED_Smith2024_Study title.pdf
```

Existing export files are never overwritten. If a filename is already present, a numbered filename is generated:

```text
Smith2024_Study title.pdf
Smith2024_Study title (2).pdf
Smith2024_Study title (3).pdf
```

The same collision handling applies to generated `.bib`, CSV, JSON and HTTP 451 report files.

For a clean standalone backup, use a new destination directory for each complete export.

## Attachment filenames

Attachment filenames are derived from:

```text
<first author surname><year>_<reference title>.<original extension>
```

Example:

```text
Smith2024_Continuous glucose monitoring in hospital.pdf
```

If the author or year is unavailable:

```text
UnknownAuthorn.d._Untitled.pdf
```

Invalid filesystem characters are replaced, reserved Windows filenames are avoided, and long filenames are shortened.

The original attachment extension is preserved when possible. Compound extensions such as these are retained:

```text
.tar.gz
.tar.bz2
.tar.xz
.nii.gz
```

If the source has no usable extension, the exporter attempts to determine one from the MIME type. If that is also unavailable, `.bin` is used.

## All attachment types are exported

The script downloads all attachments returned by the Mendeley API, not only PDFs.

Examples include:

```text
.pdf
.docx
.xlsx
.csv
.txt
.zip
.png
.jpg
```

The file content is transferred as arbitrary binary data. The exporter does not attempt to interpret the attachment as a PDF or other particular document format.

## Single BibTeX bibliography

Exactly one new bibliography is created for each export run:

```text
__<group name>.bib
```

For example:

```text
__Diabetes Literature.bib
```

Every group reference appears once in this bibliography, including:

```text
references with attachments
references without attachments
references in folders
references in several folders
references not assigned to any folder
```

Bibliographic fields are obtained from Mendeley's BibTeX API.

The exporter also adds fields required for the backup, for example:

```bibtex
mendeley_id = {document-uuid},
mendeley_folder_ids = {folder-uuid-1; folder-uuid-2},
mendeley_folder_paths = {Methods; Methods | Reviews}
```

The folder fields represent direct Mendeley folder membership. Membership in a child folder does not implicitly add membership in its parent folder.

Citation keys are made unique within the exported bibliography. If Mendeley returns duplicate keys, suffixes are added:

```bibtex
Smith2024
Smith2024_2
Smith2024_3
```

The final citation key is also used by the metadata files and the HTTP 451 report.

## Attachment links in BibTeX

Successfully downloaded attachments are linked with relative paths.

Example:

```bibtex
file = {:Smith2024_Study title.pdf:application/pdf}
```

Several attachments can be linked to one reference:

```bibtex
file = {:Smith2024_Study title.pdf:application/pdf;:Smith2024_Supplement.xlsx:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet}
```

Because the paths are relative, the bibliography and attachment files should be kept together when the backup is moved.

A missing attachment is not linked.

A file retained after failed size validation is linked explicitly as unverified:

```bibtex
file = {UNVERIFIED:_WARNING_UNVERIFIED_Smith2024_Study title.pdf:application/pdf},
mendeley_unverified_attachments = {_WARNING_UNVERIFIED_Smith2024_Study title.pdf}
```

`UNVERIFIED` means only that the received byte count did not match the file size reported by the Mendeley API. It does not mean that the exporter performed a checksum or document-content validation.

## Mendeley folders and Zotero collections

The single BibTeX file stores the folder information in two forms.

### Zotero-compatible collection information

The bibliography contains JabRef `groupsversion:3` / `groupstree` comments that Zotero's built-in BibTeX importer can use to reconstruct collections and direct memberships.

A simplified example looks like:

```bibtex
@comment{jabref-meta: groupsversion:3;}
@comment{jabref-meta: groupstree:
0 AllEntriesGroup:;
1 ExplicitGroup:Methods;0;Smith2024;;
2 ExplicitGroup:Reviews;0;Jones2025;;
}
```

For Zotero, import the generated `__<group name>.bib` with Zotero's built-in BibTeX importer.

The exporter preserves empty folders and multiple direct memberships in the generated collection tree.

The legacy group-tree format used by Zotero has some limitations. The exporter adapts the Zotero representation when necessary:

```text
Maximum represented hierarchy depth: 9 levels
Duplicate sibling names: numbered in the Zotero tree
Braces in collection names: replaced for the Zotero representation
Newlines in collection names: replaced with spaces
Certain reserved JavaScript property names: prefixed with "_"
Paths deeper than 9 levels: combined at level 9
```

Warnings are printed when such adaptations are necessary.

These adaptations affect only the Zotero representation. The exact original Mendeley hierarchy is retained separately.

### Exact Mendeley hierarchy

The bibliography also contains a Base64-encoded UTF-8 JSON manifest:

```bibtex
@comment{mendeley-export-json-base64:...}
```

The manifest contains data such as:

```text
group information
original folder records
folder IDs
parent folder IDs
direct folder memberships
document IDs
final BibTeX citation keys
successful attachment mappings
unverified attachment mappings
download failures
```

Base64 is used only to protect arbitrary JSON data from BibTeX parsing. It is not encryption.

To inspect the embedded manifest:

```python
import base64
import json
import re
from pathlib import Path

bib_file = Path("__My Group.bib")
text = bib_file.read_text(encoding="utf-8")

match = re.search(
    r"@comment\{mendeley-export-json-base64:([^}]+)\}",
    text,
)
if not match:
    raise RuntimeError("No embedded Mendeley manifest found")

manifest = json.loads(base64.b64decode(match.group(1)))

print(json.dumps(manifest, ensure_ascii=False, indent=2))
```

Mendeley's normal BibTeX import does not reconstruct the original Mendeley folder hierarchy from these fields or comments. The embedded manifest therefore serves as the exact machine-readable representation needed for a dedicated restoration or migration tool.

## HTTP 451 report

Mendeley can deny access to an attachment with HTTP status `451 Unavailable For Legal Reasons`.

**Explanation:** For some sources (i.e. Journals), sharing seems to be generally forbidden, so for a Group the attachment download is not 
possible. The file also cannot be viewed or downloaded in the Mendeley Reference Manager. This can be overcome by **copying the reference to the local library** and download it from there.

Every export creates this report:

```text
_No PDF written - Unavailable For Legal Reasons
```

The filename is retained for compatibility, but the report applies to all attachment types, not only PDFs.

It is a UTF-8 tab-separated text file with this structure:

```text
BibTeX ID	Filename
Smith2024	Smith2024_Study title.pdf
Jones2025	Jones2025_Supplement.docx
```

The first column contains the final citation key used in the generated BibTeX file.

The second column contains the filename that would have been used for the attachment.

If there are no HTTP 451 errors, the file contains only the header:

```text
BibTeX ID	Filename
```

HTTP 451 can occur either while requesting the Mendeley file endpoint or while accessing the signed download URL. Both cases are recorded.

The exporter does not attempt to bypass HTTP 451 restrictions.

## Download validation

Attachments are first downloaded to temporary files.

If the Mendeley API supplies an expected file size, the exporter compares it with the actual downloaded size:

```text
expected size == actual downloaded size
```

If the sizes differ, the download is treated as temporarily invalid and retried.

The validation deliberately does not depend on the file type. For example, it does not check:

```text
PDF header
PDF EOF marker
ZIP structure
Office document structure
image format
```

The exporter currently does not calculate or compare attachment checksums. Mendeley's `filehash` value is not used for validation. 

**Note:** There were `filehash` mismatches for correctly downloaded files, when we tried it, so it was removed again.

This means that the validation verifies transfer length, not semantic document integrity.

## Unverified downloads

A size mismatch is retried as a temporary download failure.

Each retry obtains a fresh signed download URL.

If every attempt still fails the size check, the most recently received bytes are retained:

```text
_WARNING_UNVERIFIED_<normal filename>
```

The file is linked from BibTeX with the `UNVERIFIED` label and is also listed in:

```text
references.csv -> unverified_filenames
embedded manifest -> verified: false
download failure diagnostics
```

The diagnostics include information such as:

```text
Mendeley file ID
expected size
actual size
response Content-Type
response Content-Encoding
individual failed size-check attempts
```

Only one suspect copy is retained for an attachment.

If a later retry succeeds, the previous suspect temporary copy is discarded.

Network errors that produce no usable response body cannot create an unverified file.

## Retry behavior

Temporary GET failures are retried up to six times.

Retryable conditions include:

```text
HTTP 408
HTTP 429
HTTP 500
HTTP 502
HTTP 503
HTTP 504
timeouts
temporary connection failures
incomplete HTTP reads
remote disconnects
attachment file-size mismatches
```

The normal retry delays are:

```text
2 seconds
4 seconds
8 seconds
16 seconds
32 seconds
```

If the server supplies a longer `Retry-After` value, that value takes precedence. Both numeric delay values and HTTP dates are supported.

Permanent HTTP failures such as these are not automatically retried:

```text
HTTP 401
HTTP 403
HTTP 404
HTTP 451
```

The OAuth token-exchange POST is not automatically retried.

Network operations use a timeout of 120 seconds.

## Resumable BibTeX retrieval

Retrieving BibTeX for a large group can require many API calls.

After each successfully retrieved reference, the result is written to a group-specific cache:

```text
_Mendeley_BibTeX_cache_<group hash>.jsonl
```

If BibTeX retrieval is interrupted, rerun the exporter with the same destination directory:

```powershell
uv run --env-file .env mendeley_group_export_with-bib.py `
  --group "Exact Group Name" `
  --dest "C:\MendeleyBackup"
```

The script authenticates again and retrieves the current group information, but unchanged BibTeX records are reused from the cache.

Progress output indicates how many entries were reused.

A cached entry is associated with a fingerprint of the complete current Mendeley document metadata. If the document metadata has changed, the BibTeX entry is retrieved again.

Incomplete or invalid final cache records are ignored and fetched again.

To force all BibTeX records to be retrieved again, delete only the corresponding cache file:

```text
_Mendeley_BibTeX_cache_<group hash>.jsonl
```

The cache applies to BibTeX retrieval only.

Attachment downloads do not have cross-run checkpoints. If an export is restarted after attachments have already been written, new numbered attachment filenames are created rather than replacing the existing files.

## CSV metadata

`references.csv` contains one row for each reference.

The columns are:

```text
title
authors
year
type
source
doi
isbn
issn
pmid
has_pdf
pdf_filename
id
bibtex_id
has_files
filenames
folder_paths
unverified_filenames
```

The CSV is written as UTF-8 with BOM (`utf-8-sig`), which improves compatibility with spreadsheet applications such as Microsoft Excel.

`has_files` refers to locally available downloaded attachments. An attachment that could not be downloaded is therefore not counted as a local file.

## JSON metadata

`references.json` contains the complete document records returned by the Mendeley API for the references in the group.

Example structure:

```json
[
  {
    "id": "document-id",
    "title": "Example reference",
    "year": 2024,
    "authors": [
      {
        "first_name": "Anna",
        "last_name": "Smith"
      }
    ]
  }
]
```

The raw JSON metadata is separate from the exact export manifest embedded in the BibTeX file.

## Export consistency checks

Before attachments are downloaded, the exporter validates important API relationships.

The export is stopped if conditions such as these are detected:

```text
duplicate document IDs
duplicate attachment IDs
duplicate folder IDs
attachment references a document absent from the group
folder membership references a document absent from the group
folder hierarchy contains a cycle
folder references a missing parent
```

These checks reduce the risk of silently creating a structurally inconsistent backup if the group changes while it is being exported or the API returns inconsistent data.

## Exit status

A successful export returns:

```text
0
```

An export with one or more attachment download or validation failures returns:

```text
1
```

Fatal errors also terminate the program with a non-zero exit status.

Files already written before a fatal error are not automatically deleted. Such a directory should therefore be considered an incomplete export.

## What is not exported

The script is intended to back up group references, attachments and collection structure.

It is not a complete backup of all Mendeley application state.

Items outside its scope include, for example:

```text
PDF annotations
comments
application preferences
local Mendeley database state
other application-specific state not exposed by the used API endpoints
```

## Testing

The repository includes:

```text
test_mendeley_export.py
```

The tests use Python's built-in `unittest` framework and do not require access to a real Mendeley account.

Run them with `uv`:

```powershell
uv run python -m unittest -v test_mendeley_export.py
```

Or with the active Python interpreter:

```powershell
python -m unittest -v test_mendeley_export.py
```

The tests create a local HTTP server that simulates the relevant Mendeley API behavior. They cover areas including:

```text
pagination
single-bibliography export
nested folders
empty folders
duplicate folder names
exact embedded manifests
arbitrary non-PDF attachment types
citation-key collision handling
relative attachment links
repeated exports and filename collisions
HTTP 451 at different download stages
multiple blocked attachments
temporary HTTP failures
Retry-After handling
failed response reads
BibTeX cache persistence
cache reuse after restart
cache invalidation after metadata changes
recovery from incomplete cache records
file-size mismatch retries
recovery after a later valid download
retention of unverified bytes after repeated size mismatches
retention after a later network failure
large and empty binary attachments
```

The tests are integration-style offline tests: the exporter communicates through HTTP with the local test server, but no real Mendeley API calls are made.

### Using an existing uv / PyCharm project

No test dependency needs to be added to `pyproject.toml` because the tests use the Python standard library.

A minimal project setup can therefore remain:

```toml
[project]
name = "mendeley-group-export"
version = "0.1.0"
requires-python = ">=3.8"
dependencies = []
```

Create or synchronize the environment as usual:

```powershell
uv sync
```

Then run:

```powershell
uv run python -m unittest -v test_mendeley_export.py
```

In PyCharm, configure the project's uv-created virtual environment as the project interpreter and create a Python unittest run configuration for:

```text
test_mendeley_export.py
```

No `pytest`, HTTP client library or Mendeley SDK is required.

## Suggested repository layout

A compact repository layout is:

```text
.
├── LICENSE
├── README.md
├── mendeley_group_export_with-bib.py
├── test_mendeley_export.py
└── .gitignore
```

Keep the local credential file outside version control:

```text
.env
```

## Security notes

The exporter uses OAuth2 authorization-code authentication.

The client secret is read from the environment and should be treated as confidential.

The script deliberately avoids logging complete signed attachment URLs because those URLs can contain temporary credentials.

The Bearer access token used for the Mendeley API is not forwarded to the signed attachment download destination.

Exported bibliography and metadata files can contain bibliographic information, identifiers, folder names and attachment filenames from the Mendeley account. Store the export according to the confidentiality requirements of the source library.

## License

This program is distributed under the GNU General Public License version 3 or, at your option, any later version.

See:

```text
SPDX-License-Identifier: GPL-3.0-or-later
```

and the repository's `LICENSE` file.
