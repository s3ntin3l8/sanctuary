# Real mail fixtures

Unmodified samples from the Apache Tika test corpus
(https://github.com/apache/tika, commit `fe5ba58bc1bee4f37217c32a05a9b74db7fa51ba`,
licensed Apache-2.0, https://www.apache.org/licenses/LICENSE-2.0).
Path in the repository: `tika-parsers/tika-parsers-standard/tika-parsers-standard-modules/tika-parser-microsoft-module/src/test/resources/test-documents/`.

| File | Source name | Size | Content |
| --- | --- | --- | --- |
| `test-outlook.msg` | `test-outlook.msg` | 20 KB | Outlook Express marketing mail (French, HTML/RTF body, a trailing NUL in the subject) |
| `testWINMAIL.dat` | `testWINMAIL.dat` | 66 KB | TNEF with `quick.doc`, `.html`, `.pdf`, `.txt`, `.xml` (pangram text) |

Both were checked for personal data: only a public vendor address and a pangram.
Used by `tests/integration/test_email_attachment_checks.py` without patching
`extract_msg`.
