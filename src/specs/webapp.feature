Feature: Magic Formula & Skeptical Stock Analysis report viewer
As an investor
I want an easy app that I can access on my mobile and computer
So that I can easily view the reports generated for individual stocks as well as pipeline runs

Background:
	Given magic formula & skeptical stock analysis is ran (referred as "pipeline run") for a set of stocks defined by config.yaml
	And a single ticker is analyzed using an on-demand command
	
	
  Scenario: Browse reports in the web UI (report viewer) for a single ticker symbol
    Given the web app is running and connected to the database via its own ".env"
    When a user selects a ticker (alphabetical dropdown or 3-letter type-ahead search)
    Then the app lists that ticker's pipeline runs from "ticker_runs", sorted by date (date shown)
    And when the user selects a run
    Then the app displays the Bear Case, Bull Case, Sale Advisory, and Final Report as rendered markdown
    And it shows the Buy/Watch/Avoid recommendation for that run
    And it offers a download of each report as a markdown (.md) file to the viewing device
	
  Scenario: Browse purchase decisions in the web UI (report viewer) for a single pipeline run involving multiple stocks
    Given the web app is running and connected to the database via its own ".env"
    When a user selects a pipeline run (sorted by date, most latest first) with more than a single ticker is analyzed
    Then the app lists all the stock tickers in that pipeline run grouped by recommendation, Buy first, then Watch, then Avoid
    And within each group the tickers are ordered by their Magic Formula rank, best (lowest) rank first
    And a ticker with no recorded rank sorts last within its group and falls back to alphabetical order
    And it shows the Buy/Watch/Avoid recommendation for each ticker from that run next to the ticker symbol
	And it shows the Magic Formula rank next to each ticker, or a dash when that run predates rank recording
	And it shows a link to the screen showing reports for that stock ticker from that specific pipeline run
    And it offers a download of all stock tickers, their recommendations and ranks as a CSV file to the viewing device, in the same order

  Scenario: Learn the terms without leaving the app
    Given the web app is running
    When a user opens the "Learn the terms" mode
    Then an interactive lemonade-stand simulator is shown with sliders that flow a change through the income statement, cash flow, balance sheet and the Magic Formula ratios
    And every line item and ratio offers a hover or tap definition with its formula and a worked example
    And Return on Assets is shown alongside Return on Capital, with its definition stating that the two are different measures and how
    And every definition is reachable and fully populated, since a term with no matching entry shows nothing at all rather than reporting an error
    And the figures shown match the downloadable cheat sheet exactly, which is the authoritative source for them

  Scenario: Rank is recorded per ticker per run
    Given a pipeline run analyzes candidates selected by the Phase A screener
    Then each ticker's "Final_Rank" from that screen is stored on its "ticker_runs" row as "magic_rank"
    And an on-demand single-ticker run stores no rank, because it never went through a ranking
    And runs recorded before this column existed keep a null rank rather than a fabricated one

  Scenario: Tell the reader whether a report passed independent critic review
    Given a report was reviewed by the independent critic agent (Phase D)
    When a user browses that ticker's runs
    Then the review appears as its own run, and the report it reviewed is left unchanged
    And the run is marked in the run picker before the user opens it, since it is the newest run and so the one selected by default
    And the marking says whether the critic agreed or did not agree
    And on opening it, a standing chip next to the Buy/Watch/Avoid badge repeats that, with the number of rounds
    And an ordinary pipeline run shows no such marking, because dressing it with an empty one would suggest a review that never happened

  Scenario: Read the full critic exchange in the web UI
    Given a critic-reviewed run is open
    Then a "Critic Review" tab shows every round of the exchange, oldest first
    And that tab is the only place the whole exchange is visible, since the stored report carries only the last review and only when the critic never agreed
    And each round's own headings sit below its round heading, so one round cannot read as part of another
    And the tab offers the same markdown download as the other reports
    And the Bear Case, Bull Case and Sale Advisory shown for that run are the ones from the run it reviewed, labelled as such, because a review does not re-run that research
