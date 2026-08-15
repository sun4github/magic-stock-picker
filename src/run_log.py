"""The pipeline's logger, configured once per process.

Every entry point wants the same thing: log lines on the console AND in
`logs/run_<stamp>.log`, so a finished run leaves a record. That setup used to live
in `main.py`, which meant every other entry point — `refine`, `buy_case`,
`sale_advisory` — got it by doing `import main` and then `logger = main.logger`.
Importing the CLI in order to configure logging is what tied those modules to
`main`, and that knot is what forced the `sys.modules` aliasing hack that used to
sit at the top of `main.py`.

**Attaching handlers is guarded.** `logging.getLogger(name)` returns the same object
every time, so calling this twice in one process would attach a second console
handler and a second file handler to the logger the first call already configured:
every subsequent line printed twice, and the run split across two files. That is the
exact damage the old `sys.modules` hack existed to prevent, and the guard here means
it cannot happen regardless of how many entry points ask.

Import it for the side effect and take `logger` from it:

    from run_log import logger
"""
import logging
import os

from runstamp import RUN_STAMP

LOGGER_NAME = "InvestmentAgentPipeline"

# CWD-relative, deliberately: the documented workflow is `cd src` first, which puts
# these beside the code. Running from the repo root puts them there instead — see
# docs/07-observability-and-logging.md, which treats that as a known quirk rather
# than a bug, since the config path IS anchored to __file__ and still resolves.
os.makedirs("logs", exist_ok=True)
os.makedirs("reports", exist_ok=True)

log_filename = f"logs/run_{RUN_STAMP}.log"

logger = logging.getLogger(LOGGER_NAME)


def _configure():
    """Attach the console and file handlers, once."""
    if getattr(logger, "_pipeline_configured", False):
        return
    logger.setLevel(logging.INFO)

    c_handler = logging.StreamHandler()
    f_handler = logging.FileHandler(log_filename)

    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
    c_handler.setFormatter(formatter)
    f_handler.setFormatter(formatter)

    logger.addHandler(c_handler)
    logger.addHandler(f_handler)

    # A plain attribute rather than an inspection of logger.handlers: a caller may
    # legitimately have added a handler of their own, and "has handlers" would then
    # skip the setup this module exists to perform.
    logger._pipeline_configured = True
    logger.info(f"Initialized run logging to console and '{log_filename}'")


_configure()
