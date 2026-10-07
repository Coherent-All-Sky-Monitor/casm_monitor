# Candidate classification

Candidates has **Browse** and **Classify** modes. Enter a reviewer name once;
the browser remembers it. Names are self-declared, without accounts or passwords.
Use this on the existing trusted, loopback/SSH-tunnel workspace only.

Browse preserves the plot grid and right-hand show/hide selector. Per-person
badges use the class colour and include the reviewer's name. Different usable
classifications get a **Different labels** badge. Unsure and Cannot assess do
not count as competing scientific classifications. No automatic majority label
or confirmed discovery is created.

Classify opens one large plot and a fixed review queue. Incoming events do not
move the active candidate. Queue filters are Unreviewed by me, All candidates,
My reviews, Different labels and Needs investigation. These operate on the
currently loaded candidate list (up to 500 events); exports cover all saved
annotations, including events no longer in that list.

## Labels and controls

| Button | Meaning | Shortcut |
| --- | --- | --- |
| FRB | Human assessment of a plausible dispersed burst for follow-up | 1 |
| Pulsar | Pulse supported by a pulsar association or additional evidence | 2 |
| Solar | Evidence supports a solar origin | 3 |
| RFI | The triggering event appears to be interference | 4 |
| Noise | Usable data without a convincing signal or structured interference at the trigger | 5 |
| Unsure | Needs more investigation | 6 |

**Cannot assess** records missing/bad data separately, with no scientific label.
Unreviewed means no active assessment. Neither is a Noise example.

Each label click saves immediately. **Advance after saving** is on by default
and remembered in this browser. It moves to the next candidate after the database
acknowledges the save. RFI stays on the current plot and opens optional details;
**Save RFI & next** saves those details and continues. It can also continue with
no descriptors. Previous/Next also use arrow keys;
**Undo** (U) restores the previous assessment, retaining every revision. A return
link stays available after automatic advance. Clicking the plot opens the common
zoom/pan/download viewer. Shortcuts pause while typing or using that dialog.

**Details & note** contains optional Narrowband, Broadband, Bursty, Frequency
comb, Swept/drifting and Repeating in time descriptors, an RFI-also-present flag,
and a short note. Multiple descriptors can coexist: bandwidth and time behaviour
are separate properties. Frequency comb means regularly spaced spectral lines;
Repeating describes recurrence in time. Choose a class to save them with a new review; **Save
details** updates an existing review without advancing. Missing plots disable
scientific classification, but still allow Cannot assess.

Classify can hide others' votes until the operator submits their own. The saved
`others_revealed` field describes visibility in that Classify view; the operator
may already have seen badges in Browse. This is not a blinded experiment.

Saving, Saved and Save failed are explicit. Failed saves do not advance. Retries
reuse their request ID. A stale tab gets HTTP 409 rather than overwriting another
tab's assessment for the same reviewer. Different reviewers remain independent.

The visible candidate list and badges refresh every five seconds; background
tabs pause polling and refresh on return. The review queue stays fixed until
**Refresh queue**, with a count of new candidates. The next two plots and their
metadata preload. Metadata has an eight-event, 60-second cache; up to four images
are kept warm. Saves still validate the exact plot hash and expected revision.
**Reload review** bypasses cached metadata after an error. Prefetching retains no
new evidence on disk. The first save can take longer while copying its filterbank.

## Storage and retention

Default durable root: `/mnt/nvme3/casm_candidate_annotations`, derived as a
sibling of `store_root`. Override with `paths.candidate_annotations_root` or
`CASM_MONITOR_CANDIDATE_ANNOTATIONS_ROOT`. This must be separate from production
stores and candidate inputs. It is not a render cache and has no automatic TTL.

- `annotations.sqlite3`: named reviewers, append-only assessments, exact evidence
  references and content-addressed artifact manifests. SQLite uses WAL and FULL
  synchronous commits. GET requests do not create the database.
- `artifacts/`: immutable copies of the reviewed PNG (up to 20 MiB), available
  candidate JSON (up to 4 MiB each), and the detection-beam `.fil` when present
  under `paths.candidate_archive_root` (default `/mnt/nvme3/T3/EVENTS`). Filterbank
  copies are bounded at 256 MiB. Availability/oversize is recorded explicitly.
  Copies are independent of source inodes; original removal cannot remove them.
- `backups/YYYY-MM-DD.sqlite3`: SQLite backup snapshots, refreshed on successful
  annotation writes at most once every five minutes. Backup failures leave the
  committed label intact and produce an explicit warning.

Backups are on the same disk. For disk-loss protection, back up this entire
directory off-host, including artifacts. Use SQLite's backup API for a live
database; do not copy only its main file while ignoring the WAL. Local backup
files alone do not contain the PNGs or filterbanks.

Evidence includes the original cluster and trigger records, recorded injection
associations (`matched_cluster`), plot/data checksums and a taxonomy version.
New annotations use version 2, adding `bursty` and `frequency_comb`. Existing
version-1 assessments remain unchanged; no relabeling or migration is performed.
An empty recorded injection match is not proof of an uninjected event. Existing
evidence is reused for repeated reviews of the same candidate/plot checksum.
Later ledger changes are not silently applied to a previously reviewed snapshot.

Label storage is small; retained filterbanks dominate growth. Existing T3 code
describes a typical detection-beam filterbank as about 55 MB. Retention applies
only to annotated events, not every event shown or prefetched. Monitor free space
and preserve a versioned dataset before introducing a retention policy.

**Export labels** downloads JSON Lines containing the manifest, reviewers,
artifacts, evidence and every annotation revision in one consistent read
transaction. Artifact URLs are checksum-addressed; file payloads remain in
`artifacts/`. Derive current votes by maximum revision per candidate/reviewer,
including undo's unreviewed state. Keep injection truth separate, adjudicate
disagreements explicitly, and group repeated/multi-beam detections of the same
event together when constructing ML train/test splits.

## API

All routes are under `/api/candidate-annotations`:

| Method/path | Result |
| --- | --- |
| GET `/` with `names=a,b,...` | Reviewer roster, latest votes for up to 500 names, write flag, CSRF token, taxonomy |
| POST `/reviewers` | Get/create a reviewer by whitespace-normalised, case-insensitive name |
| GET `/events/{name}` | Candidate metadata, checksum-bound plots, current votes, latest 200 history rows |
| GET `/events/{name}/plot/{filename}?sha256=...` | Exact version shown; rejects a changed plot |
| POST `/events/{name}` | Append an assessment or undo with expected revision and idempotency request ID |
| GET `/artifacts/{sha256}.{png,json,fil}` | Retained evidence bytes |
| GET `/export` | Complete annotation/evidence JSONL export |

POST requires explicit workspace mode, a same-origin loopback Origin header,
`X-CASM-Workspace: 1` and the current `X-CASM-Annotations-CSRF` token. Reviewer
names/IDs are attribution, not authenticated access control. Production T2/T3
databases are opened read-only. The old `/api/cands/events/{name}/label` operation
stays blocked in 8061; these annotations never promote an FRB, change dump
retention rules, send Slack messages or alter acquisition.

Verification: `tests/test_candidate_annotations.py`,
`frontend/tests/candidate-annotations.test.mjs`, and
`scripts/check_candidate_annotations_browser.py` (temporary databases and
synthetic plots only). No test classification belongs in the live database.
