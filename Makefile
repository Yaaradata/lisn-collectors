.PHONY: preflight database storage iam registry smoke all trace clean mock-schema mock-schema-verify seed seed-verify mock-run mock-test image deploy-mock

preflight:
	@bash scripts/00_preflight.sh

database:
	@bash scripts/01_database.sh

storage:
	@bash scripts/02_storage.sh

iam:
	@bash scripts/03_iam.sh

registry:
	@bash scripts/04_registry.sh

smoke:
	@bash scripts/05_smoke.sh

all: preflight database storage iam registry smoke

trace:
	@bash scripts/06_trace.sh

mock-schema:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; if [[ -z "$${SENTINEL_MOCK_DSN:-}" ]]; then echo "SENTINEL_MOCK_DSN missing — run scripts/05_smoke.sh (or set local DSN) first"; exit 1; fi; psql "$$SENTINEL_MOCK_DSN" -v ON_ERROR_STOP=1 -f sql/002_sentinel_mock.sql; echo "Applied sql/002_sentinel_mock.sql to sentinel_mock"'

mock-schema-verify:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; if [[ -z "$${SENTINEL_MOCK_DSN:-}" ]]; then echo "SENTINEL_MOCK_DSN missing — run scripts/05_smoke.sh (or set local DSN) first"; exit 1; fi; echo "sentinel_incident columns:"; psql "$$SENTINEL_MOCK_DSN" -v ON_ERROR_STOP=1 -tAc "SELECT count(*) FROM information_schema.columns WHERE table_schema = '\''public'\'' AND table_name = '\''sentinel_incident'\'';"; echo "sentinel_thread columns:"; psql "$$SENTINEL_MOCK_DSN" -v ON_ERROR_STOP=1 -tAc "SELECT count(*) FROM information_schema.columns WHERE table_schema = '\''public'\'' AND table_name = '\''sentinel_thread'\'';"'

seed:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m mock.seed_sentinel'

seed-verify:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m mock.seed_sentinel --verify-only'

mock-run:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m uvicorn mock.sentinel_api:app --port 8081 --reload'

mock-test:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m pytest tests/test_mock_sentinel.py -v'

image:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; source scripts/_common.sh; need gcloud; : "$${IMG:?IMG required}"; gcloud builds submit --tag "$$IMG" --project "$$PROJECT"; ok "Pushed $$IMG"'

deploy-mock:
	@bash scripts/07_deploy_mock.sh

clean:
	@echo "[clean] non-destructive cleanup"
	@bash -lc 'source scripts/_common.sh; ok "No resources destroyed. Clean is intentionally non-destructive."'
