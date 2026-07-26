Feature: Magic Formula & Skeptical Stock Analysis Agent Workflow
  As an investor
  I want an automated multi-agent system powered by Google ADK
  So that I can identify top Magic Formula stock candidates and run deep skeptical analysis on them.

  Background:
    Given the Google ADK runtime is initialized
    And a custom run-specific logger is initialized in "logs/run_{timestamp}.log" and outputting to the console

  Scenario: Execute Phase A Magic Formula Screening
    Given the "Magic Formula Screener Agent" receives a request to screen the market
    When it executes the MCP tool "run_magic_formula_screener" wrapping "magic_formulae_screener.py"
    Then the tool should return a ranked list of valid stock candidates using real-time price inputs
    And the "Magic Formula Screener Agent" filters and selects exactly the Top 30 ranked companies
    And hands off the list of Top 30 companies to the Orchestrator Agent for Phase B analysis

  Scenario: Execute Phase B Balanced Analysis on Candidates
    Given the Orchestrator has received the candidates (or a single on-demand ticker)
    When it processes each company sequentially
    Then it gathers evidence via direct tool calls (SEC 10-K, FMP metrics) and the Magic Formula value/quality signal (ROC/Earnings Yield)
    And for an on-demand single ticker it computes ROC/Earnings Yield on the fly since the screener did not run
    And it runs a SequentialAgent of two advocates and a neutral judge:
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