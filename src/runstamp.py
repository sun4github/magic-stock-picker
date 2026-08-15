"""One timestamp identifying this process, fixed at import.

Used twice, and the point is that both uses get the SAME value: `main` names the
run's log file `logs/run_<stamp>.log`, and `pipeline.execution` builds ADK session
ids from it. That is what lets a session id in a traceback be matched back to the
log file it came from.

Its own module because those two are far apart in the import graph — `pipeline`
cannot import `main`, and this has to be importable before logging is configured.
Nothing here depends on anything, so it can be imported first by anyone.

The value only has to be stable within a process; nothing persists it.
"""
from datetime import datetime

RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
