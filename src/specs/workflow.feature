Feature: Magic Formula & Skeptical Stock Analysis Agent Workflow
  As an investor
  I want an automated multi-agent system powered by Google ADK
  So that I can identify top Magic Formula stock candidates and run deep skeptical analysis on them.

  Background:
    Given the Google ADK runtime is initialized
    And a custom run-specific logger is initialized in "logs/run_{timestamp}.log" and outputting to the console

  Scenario: Execute Phase A Magic Formula Screening
    Given the "Magic Formula Screener Agent" receives a request to screen the market
    When it executes the MCP tool "run_magic_formula_screener" wrapping "magic_formula_starter_screener.py"
    Then the tool should return a ranked list of valid stock candidates using real-time price inputs
    And the "Magic Formula Screener Agent" filters and selects exactly the Top 30 ranked companies
    And hands off the list of Top 30 companies to the Orchestrator Agent for Phase B analysis

  Scenario: Run Phase A without paying for Phase B or Phase C
    Given the investor wants a refreshed ranking but does not intend to act on it yet
    When they run the screener in screen-only mode
    Then the rankings CSV is refreshed and the top candidates are logged with their ROC, Earnings Yield, ROA and P/E
    And no agent is invoked and no LLM cost is incurred
    And no pipeline run, ticker run or report record is written, so nothing appears in the web UI
    And it warns when fewer candidates cleared the gates than the analysis phase expects
    And combining screen-only with any Phase B or Phase C option is rejected rather than silently ignored

  Scenario: Run Phase B without Phase C
    Given the investor wants the bear, bull and analyst reasoning but not the sale advisory
    When they run any Phase B mode with the sale advisor skipped
    Then the bear, bull and analyst agents run and the sale advisor does not
    And no SALE_CASE is stored for that run, so a later sell-condition check has no conditions from it to test
    And the omission is reported as deliberate rather than logged as a failure
    And a report produced without Phase C is fingerprinted separately, so it is never reused to satisfy a later full run

  Scenario: Apply Greenblatt's step-by-step eligibility gates before ranking
    Given the screener has fetched the stock universe from FMP
    Then it eliminates financial stocks, including banks, insurers, asset managers and mutual-fund-like vehicles
    And it eliminates utilities
    And it eliminates foreign companies, identified by a depositary-receipt name (ADR/ADS) or a non-US country
    And it eliminates every company whose Return on Assets is below 25 percent
    And it eliminates every company whose price-to-earnings ratio is below 5, a company with no positive earnings having no P/E to test
    And it asks FMP which of the remaining companies announced earnings in the last 7 days, and eliminates those
    And it applies all of these before ranking, so an eliminated company cannot affect the ranks of the survivors
    And it ranks the survivors on Return on Capital and Earnings Yield, which the gates do not replace
    And it warns when fewer than 30 companies survive, since Phase B expects a Top 30

  Scenario: Report both return figures without conflating them
    Given a final report is generated for a candidate
    Then the "Magic Formula Metrics" section states Return on Capital as the ranking metric
    And it states Return on Assets as a screening hurdle, explaining that it divides by all assets rather than capital employed
    And it states the price-to-earnings ratio and why a ratio below 5 is a rejection rather than a bargain

  Scenario: Execute Phase B Balanced Analysis on Candidates
    Given the Orchestrator has received the candidates (or a single on-demand ticker)
    When it processes each company sequentially
    Then it gathers evidence via direct tool calls (SEC 10-K, FMP metrics) and the Magic Formula value/quality signal (ROC/Earnings Yield)
    And for an on-demand single ticker it computes ROC/Earnings Yield on the fly since the screener did not run
    And it runs three roles in sequence over one shared session -- two advocates and a neutral judge -- as a plain ordered list of LlmAgents (not ADK's SequentialAgent class), so a mid-graph rate limit can retry just the failed role:
      | Agent Name    | Instruction / Tools                                  | Objective                                                       |
      | Bear Agent    | research-instructions.md + fmp_stock_news + Tavily   | Build the skeptical BEAR case (bear_data)                       |
      | Bull Agent    | bullish-research-instructions.md + fmp_stock_news + Tavily | Build the BULL case (bull_data); Section 4 refutes the bear case |
      | Analyst Agent | neutral judge (no skeptical prompt)                  | Weigh bull vs bear and compose the final report + verdict       |

  Scenario: Execute Phase C Sale Advisor agent on Candidates
    Given the Orchestrator has finished analyzing a candidate (or a single on-demand ticker) and produed final research report
    When it processes each company sequentially
    Then it should use the instructions in sale-advisor-instructions.md as a guide
    And it should identify business events and other calculations that favor selling a given candidate (company)
    And it should use the FMP and Web search tools as needed to be accurate

  Scenario: Check whether a stock's prior sale conditions are now met (on-demand)
    Given a previous run produced and stored a SALE_CASE for a ticker the investor owns
    When the investor runs the sell-condition check for that single ticker
    Then it loads a stored SALE_CASE via "db_get_sale_case" (a specific run when pinned with --run, else the most recent)
    And it gathers current fundamentals via a direct "fmp_metrics_extractor" call
    And it uses the FMP news and Web search tools to check each sale condition against current data
    And it marks each condition as MET, NOT MET, or UNCLEAR with sourced evidence
    And it recommends SELL if any condition is clearly met, otherwise HOLD
    And it writes the evaluation to "reports/{Ticker}_Sell_Check.md" without creating a new pipeline run

  Scenario: Synthesize Bull vs Bear and Compose Final Research Report
    Given the Bear Agent and Bull Agent have produced bear_data and bull_data for a company
    When the neutral Analyst Agent weighs the two cases (plus the Magic Formula value/quality context)
    Then it must remain balanced (skepticism lives in the Bear Agent, offset by the Bull Agent)
    And it must generate a Markdown report with "## Bull Case", "## Bear Case", and "## Final Verdict" sections
    And it must enforce the rule to not invent facts beyond the two provided cases
    And the final section must explicitly state a "Buy", "Watch", or "Avoid" verdict (written for a non-owner) reached by weighing the bull case against the bear case
    And export the final report to a local Markdown file

 Scenario: Persist Intermediate Outputs and Final Report to PostgreSQL
    Given the PostgreSQL database with pgvector is initialized
    When the pipeline gathers SEC and metrics data (direct tool calls)
    Then it stores them via "db_store_agent_output" with embed=False (raw provenance, no embedding)
    And when the Bear Agent, Bull Agent, sale advisor agent complete their research
    Then it stores BEAR_CASE, BULL_CASE and SALE_CASE via "db_store_agent_output" with vector embeddings
    And when the neutral Analyst Agent completes the report
    Then the Orchestrator executes "db_store_final_report" with the Buy/Watch/Avoid verdict and markdown body
    And it executes "db_store_ticker_run" to index the run under the ticker for the web UI

  Scenario: Run Phase B and Phase C from an existing screener CSV (skip Phase A)
    Given a previous run wrote the rankings CSV "magic_formula_rankings_live.csv"
    And Phase A (the full FMP universe scan) is slow to repeat
    When the Orchestrator is invoked with the --from-csv option
    Then it loads the ranked candidates from the CSV instead of running the screener
    And it selects the Top N (top_n_candidates from config.yaml)
    And it runs Phase B over those candidates
    And it runs Phase C over those candidates, persisting a normal pipeline run

  Scenario: Execute On-Demand Skeptical Analysis for a Single Ticker
    Given a user supplies a specific stock ticker directly, bypassing Phase A screening
    When the Orchestrator starts an on-demand run for that single company
    Then it creates a "pipeline_runs" record containing only that one ticker
    And it computes the Magic Formula ROC/Earnings Yield on the fly (the screener did not run)
    And it executes the Phase B graph (direct SEC/metrics gathering, then Bear -> Bull -> neutral Analyst) for that ticker
    And it executes the Phase C sale advisor agent for that ticker
    And it persists the agent outputs and the final report under that run_id
    And it marks the pipeline run status as "COMPLETED"
    And the single-company record is structurally identical to one candidate within a full pipeline run

 Background:
    Given the Google ADK runtime is initialized
    And the application successfully loads secrets from the local ".env" file using python-dotenv
    And the application parses operational parameters from "specs/config.yaml"
    And a custom run-specific logger is initialized in "logs/run_{timestamp}.log" and outputting to the console
  # --- Phase D: independent critic review (Producer-Critic / reflection loop) ---
  # Deliberately NOT part of a pipeline run: it costs several times what producing
  # the report cost, so it is a separate command aimed at names about to be acted on.

  Scenario: Review a finished report with an independent critic
    Given a previous run produced and stored a final report for a ticker
    When the investor runs the critic refinement loop for that single ticker
    Then it loads that report via "db_get_final_report" (a specific run when pinned with --run, else the most recent)
    And it strips the deterministic sections the pipeline added, so the critic reviews the analyst's own prose
    And it recomputes the Magic Formula figures so the critic has verified figures to check claims against
    And it loads the BEAR_CASE and BULL_CASE of the reviewed run, so the report's summaries can be checked against the originals
    And the critic agent uses its own instructions in critic-instructions.md, which never include the analyst's prompt
    And the critic uses the FMP news, company profile, metrics, quarterly and Web search tools to verify claims independently
    And it grades each finding as BLOCKING, MATERIAL or MINOR
    And the refinement is recorded as its own pipeline run, leaving the reviewed report untouched

  Scenario: Revise the report and re-review until the critic agrees
    Given the critic recorded at least one BLOCKING or MATERIAL finding
    When the loop has budget for a revision and the review that must follow it
    Then the analyst rewrites the report in full under exactly the same rules that produced it
    And it either fixes each finding or rebuts it with a stated reason
    And its point-by-point reply is separated from the report and shown to the critic on the next round, never to the investor
    And the critic reviews the rewritten report again
    And the loop ends on a review, so the report finally shipped has always been checked as it stands

  Scenario: Agreement is decided from the findings, not from the critic's own summary
    Given the critic has written its review
    When the loop reads the verdict
    Then a declared AGREE while a BLOCKING or MATERIAL finding stands is treated as REVISE, and the override is logged
    And a review with no parsable verdict line falls back to the recorded severities rather than being read as agreement
    And MINOR findings never prevent agreement, because another paid round costs more than the point is worth

  Scenario: Report a review that never reached agreement
    Given the loop stopped on the round limit or the spend ceiling with objections standing
    When the refined report is written
    Then it states plainly that the independent critic has NOT agreed the report
    And it gives the number of blocking and material objections still standing and why the review stopped
    And it reproduces the critic's full final review underneath, so the reader can weigh the objections themselves

  Scenario: Report the minor points that agreement did not fix
    Given the critic agreed while MINOR findings remained
    When the refined report is written
    Then it states that the critic agreed, and names each remaining minor point with the fix that was never applied
    And it says the report was not revised for them, so they read as caveats rather than corrections already made
    And it states that agreement means no fault was found in the argument, not that the verdict is correct

  Scenario: Remember critic findings across rounds and across sessions
    Given the critic has recorded findings for a ticker in this or an earlier session
    When any later round of any later session runs for that ticker
    Then those findings are replayed to both the critic and the analyst with how each was settled
    So that the analyst does not reintroduce a correction it already made
    And the critic does not spend a paid round re-raising a point already resolved
    And findings outlive the runs they came from, because memory that cascades away would be paid for twice

  Scenario: Keep the refinement inside a spend ceiling
    Given the investor named a maximum budget for the review
    When the loop decides whether to run another round
    Then it checks the running cost plus an estimate of the next round against that ceiling
    And it checks the same total against the rolling daily ceiling, so an ad-hoc command cannot route around it
    And it reserves enough for a revision plus the review that must follow it before starting either
    And it never abandons a round already under way, since a part-paid round produces nothing

  Scenario: Keep the sale advisory consistent with the reviewed report
    Given the sale advisory was written against the report as it stood before the review
    And the critic never examines the advisory itself
    When the refinement finishes
    Then the refinement run gets its own sale advisory rather than none at all
    And if the report was never revised, the existing advisory is carried forward unchanged and labelled as such
    And if the report was revised, the advisory is re-derived against the revised report
    And if it was revised but the budget cannot cover re-deriving it, the previous advisory is carried with a visible staleness warning rather than shipped silently
    And the sell-condition check can then be pinned to the refinement's own run id, which is the id the refined report tells the reader to record

  # --- Standalone sale advisory (sale_advisory.py) ---
  # A report can outlive its advisory: a refinement revised the report, or
  # --skip-sale-advisor was used, or the advisor produced nothing, or the advisory's
  # thresholds are simply anchored to figures that have aged.

  Scenario: Generate a sale advisory for any stored report on demand
    Given a run has a final report whose sale advisory is missing, stale, or out of date
    When the investor runs the standalone sale advisory command for that ticker
    Then it derives the advisory from that run's report, whether that run came from the pipeline or from a critic review
    And it anchors the advisory to the figures current at generation time, not to the figures the report was written against
    And it says so on the advisory itself, so an advisory written later is never mistaken for one written alongside its report
    And it runs the same reconciliation gate the pipeline runs over an advisory
    And re-running the whole pipeline is not required to recover one artefact

  Scenario: Attach the advisory to the report it belongs to, without rewriting that run's cost
    Given the investor generated a sale advisory for a specific run
    Then the advisory is stored against that run, so the sell-condition check pinned to that run finds it
    And the web viewer shows it on that run's sale advisory tab
    And where a run holds more than one advisory, every reader takes the newest
    But the cost is recorded as its own run, because adding it to a finished run's totals would rewrite the record of what that run cost
    And that cost run is not browsable and is never mistaken for a critic review
    And the spend still counts against the rolling daily ceiling
