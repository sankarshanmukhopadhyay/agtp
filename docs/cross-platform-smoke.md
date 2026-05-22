# Cross-platform smoke checklist

Run before tagging a release. The CI matrix in
`.github/workflows/test.yml` covers macOS, Linux, and Windows for
Python 3.10-3.12 automatically; this checklist captures the manual
verification that CI cannot easily exercise (terminals, GUI elemen,
human-in-the-loop sanity).

For each platform, mark the box once the listed commands run cleanly
from a fresh checkout. "Fresh" means: clone the repo, `pip install -e .`,
no leftover state.

## macOS

- [ ] `python -m agtp.server 4480 --agents-dir v1/server/agents` starts
      plaintext on loopback (no `--insecure` required).
- [ ] `agtp-curl DISCOVER agtp://localhost:4480` with
      `-d '{"target":"methods"}'` returns the bucketed response.
- [ ] `agtp-server 4480` (the installed entry point) works identically.
- [ ] `cd v1 && ./run_demo.sh` completes all 14 scenarios.
- [ ] `python -m unittest discover` is green.
- [ ] elemen launches via `python3 -m client.elemen.app` (or `elemen` after install) and renders Lauren's
      identity card after navigation.

## Linux (Ubuntu LTS)

Same six items as macOS. The deploy guide assumes Ubuntu 24.04, so
that distribution is the reference.

## Windows (Git Bash, MSYS_NT)

- [ ] `python -m agtp.server 4480 --agents-dir v1/server/agents` starts
      without `python3 not found` errors.
- [ ] `agtp-curl DISCOVER agtp://localhost:4480` works with
      `-d '{"target":"methods"}'`.
- [ ] `cd v1 && bash run_demo.sh` completes all 14 scenarios. **Pay
      attention to path resolution** here: Git Bash returns POSIX-form
      paths (`/x/agtp/v1`), and the demo script relies on
      `agtp._paths.normalize` to round-trip them through Python on
      Windows.
- [ ] `python -m unittest discover` is green.

## Windows (PowerShell)

- [ ] `python -m agtp.server 4480 --agents-dir v1/server/agents` starts.
- [ ] `agtp-curl DISCOVER agtp://localhost:4480` (after `pip install -e .`)
      runs from the PowerShell prompt.
- [ ] elemen launches via `py -3.13 elemen\app.py`.

## Windows (cmd.exe)

- [ ] `python -m agtp.server 4480 --agents-dir v1\server\agents` starts.
      (cmd uses backslash; the package handles both.)
- [ ] `python -m agtp.client agtp://{lauren-id}@localhost:4480` returns
      Lauren's identity document.

## Failure modes worth a second look

- **`Python was not found` on Windows**: the App Execution Alias for
  `python3` is enabled but Python isn't installed. The demo script
  probes interpreters with `--version` to skip the alias; if you see
  this, re-run with explicit `py` or install Python via
  `winget install Python.Python.3.13`.
- **Registry returns 404 after registration**: usually a path-resolution
  bug. Confirm that `v1/registry/registry_data.json` (Windows form)
  contains the registered agents. If the file is empty after the
  registration step, there's a path normalization regression.
- **TIME_WAIT collisions** between successive runs of the demo: harmless,
  the listening port reuses thanks to `SO_REUSEADDR`. If a real LISTEN
  is squatting the port, kill the leftover process before retrying.
