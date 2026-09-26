For EACH claim independently: PROVE or DISPROVE it with a test. Create exactly ONE new test file per claim, named with its test_file_name, in the existing test directory that fits (e.g. api/tests/ for silphcoanalytics, the owning package's tests/ for lake-of-rage). Do not modify ANY other file. Each test must exercise the real code path and FAIL at the current head because of that claimed defect (an assertion failure on the wrong behaviour), not because of import errors, missing fixtures, network or environment. Run each (memory-capped): `systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 uv run pytest -q <file>` from the package directory that owns pyproject.toml.
If a claim is false (the code behaves correctly), delete that test file and mark it REJECTED with the evidence. If it truly cannot be shown by a local test, delete the file and mark it UNTESTABLE.
Every claim in the list must appear exactly once in your answer.

Final answer: exactly one fenced json block:
```json
{"results":[{"id":"<id from the list>","verdict":"CONFIRMED|REJECTED|UNTESTABLE","test_file":"repo-relative path or null","reason":"one or two sentences of evidence"}]}
```
