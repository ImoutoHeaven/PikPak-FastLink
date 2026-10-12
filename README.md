# PikPak Fastlink PyFork

A personal PikPak instant-transfer CLI. Sign in through the browser, copy the current credentials, and use Python to calculate local manifests, import files with their directory structure, export cloud manifests, compare directories, and resume batch jobs.

Requests use `curl_cffi` with Chrome impersonation. Transfers are strictly instant: a file succeeds only when the server confirms that its content is complete. A miss is recorded as `not_reusable`; the tool never uploads file content. Captcha tokens are issued and renewed using the current login credentials. Access-token renewal and interactive verification remain browser tasks. The tool does not delete, overwrite, move, or perform ordinary OSS uploads.

## Installation and entry points

Requires Python 3.11 or newer. Run from this directory:

```bash
python -m pip install -e .
python pikpak.py --help
```

`python -m pikpak_fastlink` and the installed `pikpak-fastlink` command accept the same arguments. Examples below use `python pikpak.py`.

## Browser credentials

1. Install `userscripts/pikpak-credentials.user.js` in Tampermonkey.
2. Reload `https://mypikpak.com/`, sign in, and open a directory to trigger an API request.
3. Click the credential-copy button in the bottom-right panel.
4. Paste the clipboard contents into `.env` beside `pikpak.py`.

The clipboard output uses these three fields:

```dotenv
PIKPAK_AUTHORIZATION=""
PIKPAK_DEVICE_ID=""
PIKPAK_CAPTCHA_TOKEN=""
```

Fill the empty values with the captured credentials. `PIKPAK_AUTHORIZATION` is the complete `Bearer ...` header value. The userscript observes Authorization and the device ID from official requests. When clicked, it asks the logged-in page SDK for a fresh `POST:/drive/v1/files` captcha token and copies all three values as `.env` text. This token bootstraps the Python client; the client issues replacements for the required request action. Listing and creation use `GET:/drive/v1/files` and `POST:/drive/v1/files`, respectively.

If credentials or the SDK are unavailable, reload, sign in, and navigate to a directory before copying again. The userscript checks that the login and device remain unchanged during copying. Complete any interactive verification in the browser. The button copies credentials only: it has no background renewal or transfer UI and creates no cloud files.

Python loads `.env` from the project directory containing the package, regardless of the working directory. Process environment variables take precedence. Keep this plain-text secret file private. `.env`, runtime state, and E2E directories are Git-ignored; `.env.example` contains empty placeholders. Never commit real credentials.

### Captcha renewal

On `captcha_invalid`, Python calls the official `/v1/shield/captcha/init` endpoint for the current `HTTP method:path` action, then resends the original request once. Issuance metadata takes `user_id` from the access token's `sub`; decoding this field does not authenticate the token. Authentication remains server-side.

New captcha tokens are cached per action in memory. Before the next request, the client renews a token within 30 seconds of its server-reported expiry. Concurrent renewal is serialized within each client. Token lifetime comes from `expires_in`; a copied token is a short-lived bootstrap credential.

An expired access token, an interactive challenge, or rejection after renewal stops the command while preserving resumable state. Complete login or verification in the browser, update `.env`, and rerun the command. Python does not rewrite `.env` or solve challenges. Public web signing constants are stored in `pikpak_fastlink/captcha.py` and must be updated if PikPak changes that configuration. Chrome impersonation does not guarantee that risk checks will accept a request.

## Folder IDs and manifest format

Remote arguments use PikPak's opaque folder IDs, available as `parent_id` in browser requests. `root` selects the cloud-drive root. Use a dedicated `probe/` folder and its children for initial live validation.

Import and export share this JSON format:

```json
{
  "files": [
    {
      "path": "/Season 01/episode.mkv",
      "gcid": "0000000000000000000000000000000000000000",
      "size": "422649548"
    }
  ]
}
```

The all-zero GCID is a format placeholder. Obtain real values from the local calculator or a cloud export. Each entry requires a `path`, a 40-character hexadecimal `gcid`, and a non-negative byte `size`. The API's `hash` maps to `gcid`; output GCIDs are uppercase. Exports may also include `fileId` and `parentId`, which imports do not require.

Paths are relative to the selected remote directory. Imports create the parent directories needed by file paths. Parent traversal, backslashes, and ambiguous paths are rejected. Name conflicts never cause overwrites or deletions. Manifests describe files and their parent paths, not empty directories.

## Local calculation and structured import

```bash
python pikpak.py generate-json --source-dir Movies --output-file movies.json --workers 8
python pikpak.py import-json --file movies.json --target-parent-id <folder-id> --state-file .state/movies.sqlite3 --dry-run
python pikpak.py import-json --file movies.json --target-parent-id <folder-id> --state-file .state/movies.sqlite3 --workers 4
```

`Movies/Season 01/episode.mkv` becomes `Season 01/episode.mkv` beneath the remote target. The source directory's name is not added to manifest paths. To preserve it, create a same-named remote directory and select it as the target.

Local calculation needs no credentials. The standalone calculator also runs without third-party dependencies:

```bash
python pikpak_fastlink/local_calculator.py --source-dir Movies --output-file movies.json --workers 8
```

GCID is SHA-1 over the ordered concatenation of each block's binary SHA-1 digest. Block selection matches the web WASM implementation: the low 32 bits of the file size select blocks from 256 KiB to 2 MiB, while reading, hashing, and the output size cover the entire file. Server and other-client compatibility for complete files above 4 GiB remains unverified.

The calculator uses bounded read queues and checks size, timestamps, and the opened file's identity for changes. A changed input or output failure preserves the existing output. Reduce `--workers` to limit concurrent reads on mechanical disks.

## Cloud manifest export

```bash
python pikpak.py export-json --source-parent-id <folder-id> --output-file remote.json --state-file .state/export.state.json --workers 4
```

Export recursively lists directories and follows server pagination tokens. It builds relative paths from file `name`, `hash`, and `size` without downloading content. The result is an importable manifest for this tool, not an official share link.

Listing validation covers pagination tokens, duplicate IDs and names, parent directories, file names, and completed-file hashes and sizes. Short pages and empty intermediate pages do not end pagination; only an empty `next_page_token` does. Ambiguous or incomplete scans never publish partial output. A scan is not a directory snapshot, so keep the source stable while it runs.

Rerun an interrupted export with the same arguments to resume its checkpoint. Completed state refers to the existing result. Use a new state file to scan a changed remote directory again.

## Retries and resumption

- Rerun with the same manifest, target ID, and state path; completed records are skipped.
- State is bound to the manifest's SHA-256 and the target directory. Changed input or a different target requires new state.
- HTTP 429, server errors, and transport failures receive bounded retries. `captcha_invalid` first triggers one issuance attempt for the current action. Unrecoverable authentication or interactive verification stops further scheduling.
- If a creation response is lost, the tool queries the target directory before deciding whether to retry, avoiding blind duplicate creation.
- Failed and non-reusable records are written to a `.retry.export.json` beside the state file.
- `--retry-failed` retries failed and `not_reusable` records. Once all records complete, obsolete retry manifests are removed.
- Import commands return a nonzero exit status for failures, unrecoverable credentials, or instant-transfer misses.

```bash
python pikpak.py import-json --file movies.json --target-parent-id <folder-id> --state-file .state/movies.sqlite3 --retry-failed --max-retries 5 --flush-every 100
```

An instant-transfer miss may leave a server-created incomplete file. The tool reports the miss and leaves that file intact; incomplete files do not count as completed content. If it creates a name conflict, inspect it in the browser before retrying.

Keep state separate for each job, target, and account. The tool has no account-management UI.

## Batch import and comparison

```bash
python pikpak.py batch-import-json --input-dir manifests --target-parent-id <folder-id> --state-dir .state/import --json-parallelism 2 --workers 4 --dry-run
python pikpak.py batch-import-json --input-dir manifests --target-parent-id <folder-id> --state-dir .state/import --json-parallelism 2 --workers 4
python pikpak.py batch-check-json --input-dir manifests --target-parent-id <folder-id> --state-dir .state/check --output-dir deltas --with-checksum
```

Batch commands recursively discover JSON manifests and maintain separate state for each one. All paths are relative to the same remote target; JSON file names do not introduce another directory level. Imports validate cross-manifest path conflicts before creating files. Input, state, and delta-output directories must not overlap.

`batch-check-json` checks paths by default. `--with-checksum` also compares GCID and size. Output `.delta.export.json` files contain only mismatched records and can be imported. Incomplete remote files are not matches. Completed checks reuse their stored results; use a new state directory to compare remote changes again.

## Single-directory comparison

Compare relative paths and report local entries missing remotely:

```bash
python pikpak.py compare-folder --local-folder Movies --remote-folder-id <folder-id>
```

`--remote-only` reverses the comparison. Directory mode reads neither file contents nor local hashes.

Compare an existing manifest against remote content by GCID:

```bash
python pikpak.py compare-folder --local-json movies.json --remote-folder-id <folder-id> --output-file missing.json
```

JSON mode matches content even when remote files are renamed or flattened. Multiple paths for the same content are treated as present when that GCID exists. `--remote-only` selects content present only remotely. Output preserves paths from the selected side and atomically replaces the output file.

## Verification

```bash
python -m pip install -e '.[test]'
python -m pytest -q
node --test tests/test_userscript.cjs
```

Python tests cover protocol shapes, action-specific captcha issuance, expiry and concurrent renewal, challenges and credential redaction, environment loading, transport options, strict instant-transfer decisions, pagination, directory conflicts, retries, checkpoints, batch operations, and atomic output. Userscript tests cover request observation and clipboard behavior. Tests use synthetic credentials; real-account validation is restricted to `probe/`.

Browser main-world injection checks request capture and UI behavior but does not verify Tampermonkey's extension sandbox. A dedicated userscript for manifest extraction or import is outside this project's current scope.
