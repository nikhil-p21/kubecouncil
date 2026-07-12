from app.observability import configure_logging
from app.runtime.workers import run_executor_worker

configure_logging()
run_executor_worker()
