# Python 3.14 support

Status: complete

Target version: `0.2.0`

## Scope

- Declare standard GIL-enabled CPython `3.14` as a supported portable runtime
  alongside `3.11`–`3.13`; do not claim the free-threaded `t` ABI.
- Exercise the portable test suite on Python `3.14` in GitHub Actions.
- Verify the locked Python dependencies install on real Python `3.14` without weakening hash pinning.
- Keep the canonical renderer platform contract, VTracer-first workflow, monochrome scope, acceptance model, schemas, fixtures, and goldens unchanged unless testing proves a compatibility change is required.
- Update versioned release documentation and deterministic release packaging for `0.2.0`.

## Acceptance criteria

- The pre-change package metadata rejection is reproduced with a real CPython `3.14` runtime.
- A clean Python `3.14` environment installs `requirements-lock.txt` with `--require-hashes` and installs this project.
- The full portable suite passes on Python `3.14`; existing supported-runtime and canonical checks remain green where locally available.
- CI includes Python `3.14`, validators pass, Python sources compile, two `0.2.0` release builds are byte-identical, and `git diff --check` passes.
- Documentation states the tested platform boundary without treating a Darwin run as Linux runtime evidence.

## Verification evidence

- RED: official CPython `3.14.7` rejected the previous metadata with
  `3.14.7 not in '<3.14,>=3.11'`.
- GREEN: the unchanged hash lock installed in a clean CPython `3.14.7` Darwin
  arm64 venv, then the project built and installed as version `0.2.0`.
- The Python `3.14.7` portable suite passed all `302` tests with the single
  expected Linux-only skip; canonical Darwin arm64 conformance passed with
  zero skips.
- The prior-runtime Python `3.12.13` suite passed: `299` tests ran in the
  protected source tree, and the three write-near-repository security tests
  passed separately in an identical writable copy; one Linux-only skip was
  expected.
- Skill/schema validators, system quick validation, `py_compile`, and two
  byte-identical `0.2.0` release builds passed. Linux Python `3.14` runtime
  remains pending the new Ubuntu CI matrix job after push.
