"""Everything the pipeline writes down, and reads back.

    db.py          the connection and the schema bootstrap
    embeddings.py  vectors for semantic search, and their billing
    runs.py        pipeline runs, agent outputs, and the reuse path
    reports.py     final reports, verdicts, and the getters phases read them with
    critic.py      the refinement loop's findings, responses, and memory
    ledger.py      what each verdict claimed, recorded for grading later
    results.py     reading the status string every db_* tool hands back

The schema bootstrap is lazy — it fires on the first connection that needs it, not
at import — so importing this package costs nothing until something actually reads
or writes. That is deliberate and worth keeping: it used to run at module scope,
charging every test file, cron script and `--help` a two-minute remote round trip.
"""
from .critic import (
    db_get_critic_memory,
    db_record_analyst_response,
    db_resolve_critic_findings,
    db_store_critic_findings,
)
from .db import get_db_connection, initialize_database
from .embeddings import (
    EMBED_DIMS,
    EMBED_MODEL,
    EMBEDDING_USAGE,
    drain_embedding_usage,
    get_embedding,
)
from .ledger import _num, db_log_run_universe, db_log_verdict
from .reports import (
    db_get_agent_output,
    db_get_buy_case,
    db_get_final_report,
    db_get_sale_case,
    db_search_historical_reports,
    db_store_final_report,
    db_store_ticker_run,
)
from .results import _check_db
from .runs import (
    db_copy_inherited_outputs,
    db_copy_ticker_outputs,
    db_create_pipeline_run,
    db_finalize_pipeline_run,
    db_find_reusable_report,
    db_spend_since,
    db_store_agent_output,
    db_update_pipeline_status,
)

__all__ = [
    "EMBEDDING_USAGE",
    "EMBED_DIMS",
    "EMBED_MODEL",
    "_check_db",
    "_num",
    "db_copy_inherited_outputs",
    "db_copy_ticker_outputs",
    "db_create_pipeline_run",
    "db_finalize_pipeline_run",
    "db_find_reusable_report",
    "db_get_agent_output",
    "db_get_buy_case",
    "db_get_critic_memory",
    "db_get_final_report",
    "db_get_sale_case",
    "db_log_run_universe",
    "db_log_verdict",
    "db_record_analyst_response",
    "db_resolve_critic_findings",
    "db_search_historical_reports",
    "db_spend_since",
    "db_store_agent_output",
    "db_store_critic_findings",
    "db_store_final_report",
    "db_store_ticker_run",
    "db_update_pipeline_status",
    "drain_embedding_usage",
    "get_db_connection",
    "get_embedding",
    "initialize_database",
]
