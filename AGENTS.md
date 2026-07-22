# Repository Guidelines

## Project Structure & Module Organization
- `src/agents/` — Agent implementations (business logic). Add new agents here.
- `src/collectors/` — Data collectors (quotes, kline, news, etc.).
- `src/core/` — Core utilities (AI client, notifier, scheduler helpers).
- `src/web/` — FastAPI app (models, API routes, DB setup).
- `frontend/` — React + TypeScript (Vite + Tailwind). UI lives in `frontend/src/`.
- `prompts/` — Prompt templates used by agents.
- `config/`, `data/` — Config files and runtime data (persisted at `DATA_DIR`).
- `server.py` — Backend entrypoint; also registers agents and data sources.
- `tests/` — Placeholder for backend tests.
- `build.sh`, `Dockerfile` — Build frontend and container images.

## Build, Test, and Development Commands
- Backend (dev): `make dev-api`（自动 venv+依赖+uvicorn reload，监听 `:8000`）；或手动 `python server.py`。
- Frontend (dev): `make dev-web`（自动 pnpm install+dev，served on `http://localhost:5183`）。
- Frontend (build): `cd frontend && pnpm install --frozen-lockfile && pnpm build`.
- Docker image: `./build.sh <version>` (copies `frontend/dist` to `./static` and builds image).
- Run via Docker: `docker run -d -p 8000:8000 -v panwatch_data:/app/data chentnt/panwatch:latest`.
- Tests (backend): add pytest tests under `tests/` then run `pytest`.

## Coding Style & Naming Conventions
- Python: PEP 8, 4-space indent, type hints required for new code. Files `snake_case.py`, classes `PascalCase`, functions/vars `snake_case`.
- Agents: implement in `src/agents/*.py`, register in `server.py` (`AGENT_REGISTRY`) and seed config in `seed_agents()`.
- Collectors: place in `src/collectors/`, keep stateless; return typed dataclasses.
- TypeScript: components `PascalCase.tsx` in `frontend/src/`, hooks `use-` prefix, utilities `camelCase.ts`.
- Prompts: one prompt file per agent in `prompts/` (e.g., `daily_report.txt`).

## Testing Guidelines
- Backend: structure tests as `tests/test_<module>.py`; prefer fast, isolated unit tests around agents, collectors, and core.
- Coverage: target meaningful coverage for new modules (no strict threshold yet, but include happy-path and error cases).
- Fixtures: use factory helpers for DB models; avoid network calls (mock collectors and AI clients).

## Commit & Pull Request Guidelines
- Commit format: `<type>: <subject>` where type ∈ `{feat, fix, docs, refactor, style, test}`.
  Example: `feat: add intraday monitor agent`.
- Pull Requests: include a clear description, linked issues, and screenshots/GIFs for UI changes. Update docs/prompts when applicable.
- CI hygiene: ensure backend runs (`python server.py`) and frontend builds (`pnpm build`). No secrets in commits; use `.env` or UI settings.

## Security & Configuration Tips
- Secrets: do not commit API keys; configure via UI or env vars (`.env`, `AUTH_USERNAME`, `AUTH_PASSWORD`, `JWT_SECRET`, `DATA_DIR`).
- Network/SSL: optional corporate CA via `data/ca-bundle.pem` is auto-managed; respect `HTTP(S)_PROXY`/app proxy settings.
- Playwright: in Docker, browsers install under `DATA_DIR/playwright` automatically; local dev uses system install.

## 二次开发协作约定（项目所有者要求，2026-07-21 起生效）
- 本项目为中等复杂度二次开发（fork 自 TNT-Likely/PanWatch），采用**阶段门禁制**：任何开发任务先出/更新计划文档（`docs/` 目录），经所有者逐条确认决策点后才能动代码；每个阶段完成后提交变更清单供审计，确认后才进入下一阶段。
- 发现计划外的新问题或新选项时，暂停并回报，不自行扩大改动范围。
- 项目解读文档与融合计划索引见 `docs/README.md` 与 `docs/12-融合计划-东芯方案与持仓交易.md`。
