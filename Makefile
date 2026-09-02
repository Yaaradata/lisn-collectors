.PHONY: preflight database storage iam registry smoke all trace clean mock-schema mock-schema-verify seed seed-verify mock-run mock-test image deploy-mock collector-schema collector-schema-verify procrastinate-schema-read procrastinate-schema-apply procrastinate-verify test-source test-raw worker api frontend bigquery e2e sweeper sweep-now pause resume drain ks-status failure-demos demo trace-s4 deploy-preflight grant-invoker grant-admin-reset deploy-services deploy-workers deploy-agent deploy-agent-frontend workers-start workers-stop workers-restart workers-status workers-logs workers-scale e2e-cloud measure-rate demo-cloud trace-s5 reset two-stage-demo reset-api reset-api-force

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
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; source scripts/_common.sh; need gcloud; : "$${IMG:?IMG required}"; echo "Building $${IMG} (one image, no default CMD — mock/API/workers all use it)"; gcloud builds submit --tag "$$IMG" --project "$$PROJECT"; DIGEST=$$(gcloud artifacts docker images describe "$$IMG" --project="$$PROJECT" --format="value(image_summary.digest)" 2>/dev/null || true); ok "Pushed $$IMG"; echo "digest=$${DIGEST:-<unknown>}"'

deploy-mock:
	@bash scripts/07_deploy_mock.sh

deploy-services:
	@bash scripts/26_deploy_services.sh

deploy-workers:
	@bash scripts/27_deploy_workers.sh

deploy-agent:
	@bash scripts/deploy_agent.sh

deploy-agent-frontend:
	@bash scripts/deploy_agent_frontend.sh

workers-start:
	@bash scripts/28_workers_control.sh start $(SOURCE)

workers-stop:
	@bash scripts/28_workers_control.sh stop $(SOURCE)

workers-restart:
	@bash scripts/28_workers_control.sh restart $(SOURCE)

workers-status:
	@bash scripts/28_workers_control.sh status

workers-logs:
	@bash scripts/28_workers_control.sh logs $(SOURCE) $(N)

workers-scale:
	@bash scripts/28_workers_control.sh scale $(SOURCE) $(N)

reset:
	@bash scripts/33_reset_collector.sh $(if $(RESTART),--restart,)

two-stage-demo:
	@bash scripts/34_two_stage_demo.sh $(if $(RESET),--reset,) $(if $(AUTO),--auto,)

reset-api:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; : "$${COLLECTOR_API_URL:?}"; TOKEN=$$(gcloud auth print-identity-token 2>/dev/null || true); HDR=(); if [[ -n "$$TOKEN" ]]; then HDR=(-H "Authorization: Bearer $$TOKEN"); fi; URL="$${COLLECTOR_API_URL}/v1/admin/collector-data?confirm=reset-collector-data&dry_run=true"; echo "DELETE $$URL"; curl -sS -X DELETE "$${HDR[@]}" "$$URL" | python -m json.tool'

reset-api-force:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; : "$${COLLECTOR_API_URL:?}"; TOKEN=$$(gcloud auth print-identity-token 2>/dev/null || true); HDR=(); if [[ -n "$$TOKEN" ]]; then HDR=(-H "Authorization: Bearer $$TOKEN"); fi; URL="$${COLLECTOR_API_URL}/v1/admin/collector-data?confirm=reset-collector-data&dry_run=false"; echo "DELETE $$URL"; curl -sS -X DELETE "$${HDR[@]}" "$$URL" | python -m json.tool'

e2e-cloud:
	@bash scripts/29_e2e_cloud.sh

measure-rate:
	@bash scripts/30_measure_rate.sh

collector-schema:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; if [[ -z "$${COLLECTOR_DSN:-}" ]]; then echo "COLLECTOR_DSN missing — run scripts/05_smoke.sh (or set local DSN) first"; exit 1; fi; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -f sql/001_collector.sql; echo "Applied sql/001_collector.sql to collector"'

collector-schema-verify:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; if [[ -z "$${COLLECTOR_DSN:-}" ]]; then echo "COLLECTOR_DSN missing — run scripts/05_smoke.sh (or set local DSN) first"; exit 1; fi; echo "== tables =="; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c "SELECT tablename FROM pg_tables WHERE schemaname='\''public'\'' AND tablename IN ('\''collector_request'\'','\''collector_job'\'','\''raw_manifest'\'') ORDER BY 1;"; echo "== CHECK constraints on collector_job =="; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='\''collector_job'\''::regclass AND contype='\''c'\'' ORDER BY 1;"; echo "== UNIQUE constraints on collector_job =="; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='\''collector_job'\''::regclass AND contype='\''u'\'' ORDER BY 1;"; echo "== indexes on collector_job =="; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='\''public'\'' AND tablename='\''collector_job'\'' AND indexname LIKE '\''idx_collector_job_%'\'' ORDER BY 1;"'

# Look at the SQL before applying. You are adding 4 tables, 3 enum types,
# 18 functions, 7 indexes and 5 triggers to the same database that holds
# collector state.
procrastinate-schema-read:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; export PYTHONPATH=.; export PROCRASTINATE_APP=collector.app.app; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m procrastinate schema --read'

# MUST run AFTER `make collector-schema`.
procrastinate-schema-apply:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; export PYTHONPATH=.; export PROCRASTINATE_APP=collector.app.app; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m procrastinate schema --apply; echo "Procrastinate schema applied to collector"'

# worker_id on procrastinate_jobs references procrastinate_workers ON DELETE SET NULL,
# so when a stalled worker is pruned its in-flight jobs are left with status='doing'
# and worker_id IS NULL — a precise orphan signal the Sprint 4 sweeper uses.
procrastinate-verify:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; if [[ -z "$${COLLECTOR_DSN:-}" ]]; then echo "COLLECTOR_DSN missing"; exit 1; fi; echo "== procrastinate_* tables =="; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c "SELECT tablename FROM pg_tables WHERE schemaname='\''public'\'' AND tablename LIKE '\''procrastinate_%'\'' ORDER BY 1;"; echo "== enum types =="; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c "SELECT typname FROM pg_type WHERE typname LIKE '\''procrastinate_%'\'' AND typtype='\''e'\'' ORDER BY 1;"; echo "== procrastinate_* function count (expect 18) =="; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c "SELECT count(*) AS function_count FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='\''public'\'' AND p.proname LIKE '\''procrastinate_%'\'';"; echo "== procrastinate_* indexes (expect 7) =="; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c "SELECT indexname FROM pg_indexes WHERE schemaname='\''public'\'' AND indexname LIKE '\''%procrastinate_%'\'' ORDER BY 1;"; echo "== \\\\d procrastinate_jobs =="; psql "$$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c "\\d procrastinate_jobs"'

test-source:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; export PYTHONPATH=.; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m pytest tests/test_sentinel_source.py -v'

test-raw:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; export PYTHONPATH=.; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m pytest tests/test_raw_determinism.py -v'

# -c 1 is load-bearing. One worker equals one connection to Sentinel.
# Leaving the default breaks the rate arithmetic.
worker:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; export PYTHONPATH=.; export PROCRASTINATE_APP=collector.app.app; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m procrastinate worker -q sentinel -c 1 --delete-jobs never'

# Maintenance queue only — runs the periodic sweep and sweep_now. Does not
# pull sentinel fetch_page jobs (those stay on `make worker`).
# ENABLE_PERIODIC=1: this process alone scrapes global OTel gauges (workers.live
# etc). Enrichment workers must NOT set it — triple-counted gauges break alerts.
# Procrastinate 3.9 requires --delete-jobs values in LOWERCASE (never,
# successful, always).
sweeper:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; export PYTHONPATH=.; export PROCRASTINATE_APP=collector.app.app; export COLLECTOR_SOURCE=$${COLLECTOR_SOURCE:-maintenance}; export ENABLE_PERIODIC=1; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m procrastinate worker -q maintenance -c 1 --delete-jobs never'

# Waiting two minutes for the cron tick in front of an audience is bad demo pacing.
# Invokes the sweep body directly (does not wait for a worker / cron tick).
sweep-now:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; export PYTHONPATH=.; export PROCRASTINATE_APP=collector.app.app; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" scripts/sweep_now.py'

api:
	@bash -lc 'set -euo pipefail; set -a; source .env; set +a; export PYTHONPATH=.; if [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi; "$$PY" -m uvicorn collector.api:api --port 8080 --reload'

# Demo UI stand-in for LiSN (Pass 1: connection + status bar). No build step.
frontend:
	@echo "LiSN Collector Console → http://127.0.0.1:3000/"
	@echo "API default: http://localhost:8080  (use gcloud run services proxy for Cloud Run)"
	@python -m http.server 3000 --directory frontend

bigquery:
	@bash scripts/08_bigquery.sh $(SOURCE)

e2e:
	@bash scripts/09_e2e.sh

# Killswitch — SOURCE=sentinel (required for pause/resume/drain).
pause:
	@bash scripts/11_killswitch.sh pause "$(SOURCE)"

resume:
	@bash scripts/11_killswitch.sh resume "$(SOURCE)"

drain:
	@bash scripts/11_killswitch.sh drain "$(SOURCE)"

ks-status:
	@bash scripts/11_killswitch.sh status

failure-demos:
	@bash scripts/12_failures.sh

demo:
	@bash scripts/10_demo.sh

demo-cloud:
	@bash scripts/31_demo_cloud.sh

trace-s4:
	@bash scripts/13_trace_s4.sh

trace-s5:
	@bash scripts/32_trace_s5.sh

deploy-preflight:
	@bash scripts/23_deploy_preflight.sh

grant-invoker:
	@bash scripts/24_grant_invoker.sh

grant-admin-reset:
	@bash scripts/35_grant_admin_reset.sh

clean:
	@echo "[clean] non-destructive cleanup"
	@bash -lc 'source scripts/_common.sh; ok "No resources destroyed. Clean is intentionally non-destructive."'
