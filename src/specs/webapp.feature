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
    Then the app lists all the stock tickers in that pipeline run in a alphabetical order
    And it shows the Buy/Watch/Avoid recommendation for each ticker from that run next to the ticker symbol
	And it shows a link to the screen showing reports for that stock ticker from that specific pipeline run
    And it offers a download of all stock tickers and their recommendations as a CSV file to the viewing device
