.PHONY: verify backend-test backend-lint backend-type frontend-test frontend-lint frontend-build

verify: backend-test backend-lint backend-type frontend-test frontend-lint frontend-build

backend-test:
	cd backend && python -m pytest

backend-lint:
	cd backend && python -m ruff check .

backend-type:
	cd backend && python -m mypy app

frontend-test:
	cd frontend && npm test

frontend-lint:
	cd frontend && npm run lint

frontend-build:
	cd frontend && npm run build
