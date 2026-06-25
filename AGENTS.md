# Repository Guidelines

## Project Structure & Module Organization

This repository contains Xiaomi Miloco, an OpenClaw plugin with Python services and a React dashboard. Key paths:

- `backend/`: uv workspace. `backend/miloco/src/miloco/` is the FastAPI service, perception engine, rules, MIoT gateway, and static dashboard host; `backend/miot/` is the MIoT SDK.
- `cli/`: Python `miloco-cli` package and command modules under `cli/src/miloco_cli/`.
- `web/`: React 19 + Vite dashboard; source is in `web/src/`, tests in `web/tests/`, build output is written into `backend/miloco/src/miloco/static/`.
- `plugins/openclaw/`: TypeScript OpenClaw plugin; `plugins/skills/` contains agent skill docs.
- `knowledge/`, `assets/`, and `scripts/`: project docs, images, and install/build tooling.

## Build, Test, and Development Commands

- Root build: `bash scripts/build.sh` builds packages into `dist/`; use `--packages miloco,web` to limit scope.
- Backend: from `backend/`, run `uv sync --all-packages`, `uv run task dev`, `uv run task test`, `uv run task lint`, and `uv run task check`.
- CLI: from `cli/`, run `uv sync`, `uv run pytest`, and `uv run miloco-cli --help`.
- Web: from `web/`, run `pnpm install`, `pnpm build`, `pnpm build:watch`, `pnpm typecheck`, and `pnpm test`.
- OpenClaw plugin: from `plugins/openclaw/`, run `pnpm install`, `pnpm build`, `pnpm test`, `pnpm check`, and `pnpm lint`.

## Coding Style & Naming Conventions

Python uses Ruff with `E`, `F`, and `I` rules; line length `E501` is ignored. Keep modules snake_case, tests named `test_*.py`, and CLI commands under `cli/src/miloco_cli/commands/`. TypeScript is ESM; prefer PascalCase React components, camelCase utilities and hooks, and domain helpers under `src/lib/` or `src/api/`.

## Testing Guidelines

Use pytest for Python packages and Vitest for TypeScript. Place backend tests in the relevant package `tests/` tree, mirroring areas such as `perception/`, `observability/`, or `node_monitor/`. Web tests live in `web/tests/*.test.ts`; OpenClaw plugin tests live in `plugins/openclaw/tests/*.test.ts`. Run focused tests first, then the package-level command before submitting.

## Commit & Pull Request Guidelines

Recent history uses conventional prefixes such as `feat:`, `fix:`, and `fix(openclaw):`. Keep subjects concise; Chinese or English is acceptable. Include issue or PR references when relevant, for example `(#276)`. Pull requests should summarize behavior changes, list verification commands, link issues, and include screenshots for dashboard UI changes.

## Security & Configuration Tips

Do not commit API keys, local tokens, or generated user configuration. Shared runtime config lives in `$MILOCO_HOME/config.json`, with overrides like `MILOCO_MODEL__OMNI__API_KEY`. Treat ONNX models, native MIoT libraries, and static build artifacts as large assets; avoid churn unless required.
