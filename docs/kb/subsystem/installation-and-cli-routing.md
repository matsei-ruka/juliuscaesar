---
title: Installation and CLI routing
section: subsystem
status: active
code_anchors:
  - path: install.sh
    symbol: "BINARIES=(jc jc-memory jc-heartbeat jc-voice jc-watchdog jc-workers jc-init jc-setup jc-doctor)"
  - path: bin/jc
    symbol: "case \"$SUB\" in"
last_verified: 2026-04-25
verified_by: l.mattei
related:
  - contract/instance-layout-and-resolution.md
---

## Summary

`install.sh` installs JuliusCaesar by creating a venv at `~/.local/share/juliuscaesar/venv`, installing Python dependencies, and writing executable shims into `~/.local/bin`. The shims call the binaries in the current framework checkout, so `git pull` updates behavior without reinstalling.

The top-level `jc` command is a bash router. It dispatches `memory`, `heartbeat`, `voice`, `watchdog`, `workers`, `init`, `setup`, and `doctor` to matching `jc-*` binaries on PATH.

## Source of truth

- `install.sh` owns dependency setup and shim generation.
- `bin/jc` owns the public router surface.
- Individual binaries own subcommand behavior.
- `bin/jc-setup` owns the guided first-run configurator.

## Important behavior

- Python dependencies are currently `pyyaml`, `python-dotenv`, `dashscope`, and `requests`.
- Python 3.10+ is required because the library code uses modern type syntax.
- The installer refuses to overwrite existing `~/.local/bin/jc-*` shims that point to a different JuliusCaesar clone unless run with `--force`.
- Python binaries run through the venv wrapper with the framework `lib/` on `PYTHONPATH`.
- Native bash binaries are invoked directly by their shim.
- `jc setup` uses `jc init` underneath, writes `.env`, L1 memory, watchdog config, rebuilds the memory index, and runs `jc doctor`.

## Failure modes

- If `~/.local/bin` is missing from PATH, installation still writes shims but warns the user.
- If a different clone already owns the shims, install fails until the user chooses the existing clone or forces overwrite.
- If `jc` cannot find `jc-<subcommand>` on PATH, it exits 127 and tells the user to run `install.sh`.
- If the target for `jc init` is non-empty, it refuses portably on macOS and Linux, except for `.git`, `.gitignore`, `README.md`, and `LICENSE`.

## Open questions / known stale

- 2026-04-25: Roadmap still lists public distribution via npm, brew, or curl as future work.
