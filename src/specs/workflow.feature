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

  Scenario: Execute Phase B Skeptical Analysis on Top Candidates
    Given the Orchestrator Agent has received the Top 30 stock candidates
    When it iterates over each candidate company sequentially
    Then it delegates task execution to the following sub-agents:
      | Agent Name                 | MCP Tool Used                | Objective                                              |
      | SEC Filings Data Agent     | sec_edgar_extractor          | Extract segment data and >10% customer concentration    |
      | Quantitative Metrics Agent | fmp_metrics_extractor        | Extract 3-yr metric trends & 5-yr P/E averages         |
      | Search & Bear Case Agent   | fmp_stock_news + web_search_tool (Tavily) | Find recent factual news (FMP) and qualitative bear-case risks (Tavily) |
    And the Orchestrator synthesizes the sub-agent outputs into a comprehensive Skeptical Investment Thesis report

  Scenario: Synthesize Data and Compose Final Research Report
    Given the three data extraction agents (SEC, Quant, Search) have completed their tasks for a specific company
    When the Orchestrator passes all collected raw context and the "research-instructions.md" file to the "Analysis & Composition Agent"
    Then the "Analysis & Composition Agent" must adopt the persona of a skeptical decomposer
    And it must generate a structured Markdown report answering every question in "research-instructions.md"
    And it must enforce the rule to not invent facts and explain complex terms simply
    And the final section of the report must explicitly state a "Buy", "Sell", or "Hold" suggestion based on the skeptical thesis
    And export the final report to a local Markdown file

 Scenario: Persist Intermediate Outputs and Final Report to PostgreSQL
    Given the PostgreSQL database with pgvector is initialized
    When a sub-agent completes its research task for a company
    Then the Orchestrator executes the MCP tool "db_store_agent_output" to store the text and its vector embedding
    And when the "Analysis & Composition Agent" completes the research report
    Then the Orchestrator executes the MCP tool "db_store_final_report" with the verdict and markdown body
    And verifies the record is saved with a non-null vector embedding

  Scenario: Execute On-Demand Skeptical Analysis for a Single Ticker
    Given a user supplies a specific stock ticker directly, bypassing Phase A screening
    When the Orchestrator starts an on-demand run for that single company
    Then it creates a "pipeline_runs" record containing only that one ticker
    And it executes the Phase B SequentialAgent graph (SEC, Quant, Search, Analysis) for that ticker
    And it persists the agent outputs and the final report under that run_id
    And it marks the pipeline run status as "COMPLETED"
    And the single-company record is structurally identical to one candidate within a full pipeline run

 Background:
    Given the Google ADK runtime is initialized
    And the application successfully loads secrets from the local ".env" file using python-dotenv
    And the application parses operational parameters from "specs/config.yaml"
    And a custom run-specific logger is initialized in "logs/run_{timestamp}.log" and outputting to the console