# Goal

A buy case was written for this company at some earlier date. It named a price range
and a set of observable events that would turn a **Watch** into a **Buy**. Your job is
to decide, against **today's** data, whether those conditions are now met.

You are not re-running the analysis and you are not re-deciding whether the buy case
was right. You are testing conditions someone else wrote, one at a time, against
current facts. If the buy case set a bad threshold, say so in the summary — but still
report honestly whether the threshold as written has been met.

# Assumptions

1. The reader does **not** own this stock and is deciding whether to open a position.
2. **Unavailable is not met.** If you cannot establish whether a condition is
   satisfied, mark it UNCLEAR and say what you looked at. Never resolve a gap in the
   evidence in favour of buying.
3. Never invent a figure. Quote what the data actually shows, with its date.
4. Cite a source for every current figure or event you use as evidence.
5. Plain English, with any financial term explained in brackets the first time.

# What you are given

- **BUY_CONDITIONS** — the stored buy case, including its numbered Buy Triggers, the
  price range and its arithmetic, and the "what would take this off the watchlist"
  section. Its "Currently" figures are the values at the time it was written; they are
  the baseline, not the current state.
- **PRICE_DATA** — today's price, the 52-week range, and the 50/200-day moving
  averages, fetched fresh for this check. This is the authority for the price trigger,
  not any price mentioned in the buy case or in a news article.
- **CURRENT_METRICS** — the latest FMP fundamentals. These are **annual** and cannot
  answer a condition written in quarters.
- **QUARTERLY_DATA** — the last 8 quarters with year-over-year comparisons. Use this
  for every quarterly condition, and quote the specific quarter and its period end.
- Your tools: `fmp_forward_estimates` (have consensus earnings estimates and price
  targets moved since the buy case was written?), `fmp_earnings_calendar` (has the
  company — or a company the buy case named as a leading indicator — reported since?),
  `fmp_stock_news`, `fmp_pending_ma_filings`, and `web_search_tool` for anything the
  feeds do not cover.

# Required output

A Markdown document titled `## Buy-Condition Check`.

## Per-trigger evaluation

For **every** trigger in the buy case, in its original order and keeping its original
number, write:

- **Trigger N** — restate it in one line, including its threshold.
- **Status:** MET / NOT MET / UNCLEAR.
- **Evidence:** the current figure or event against the threshold, with the date and
  source. For a price trigger, quote today's price from PRICE_DATA and state the gap
  to the threshold as a percentage. For an event trigger, name the report or
  announcement you checked and what it said. Where the condition names a future
  earnings date that has not arrived yet, that is NOT MET with the date it is due —
  not UNCLEAR.

## Then: what has changed since the buy case was written

A short section, three or four bullets, on anything that materially changes the
picture even though it is not one of the triggers:

- Have the consensus earnings estimates or the forward multiple moved, and in which
  direction? A trigger of "buy below $38" set against a forward estimate that has
  since been cut is a cheaper price for a worse business, and the reader has to be
  told.
- Have the analyst price targets moved, and who moved them?
- Has anything appeared that belongs in the buy case's own "what would take this off
  the watchlist" section? Check that section explicitly and say whether any of its
  conditions has fired.
- Has a merger, an acquisition, or a bid been filed or reported? A company under an
  agreed bid trades on the offer, which makes a valuation-derived price trigger
  meaningless — say so rather than reporting the price trigger as met.

## Buy Recommendation

A `## Buy Recommendation` section stating, in this order:

- How many triggers are MET, out of how many — for example "2 of 4 triggers met".
- Exactly one recommendation line, written in one of these two forms:
  - `Recommendation: BUY` — the buy case's own stated firing condition (Section 5's
    closing sentence: which triggers are necessary and how many together suffice) is
    satisfied.
  - `Recommendation: WAIT` — it is not.
- If the buy case did not state its firing condition clearly, apply this default and
  say you are doing so: **BUY requires the price trigger to be MET and at least one
  event trigger to be MET.** Price alone is not enough — a stock that has become
  cheap without answering the objection that made it a Watch is usually cheap for
  that reason.
- A one-paragraph justification of how the met and unmet triggers net out, in plain
  English, that a reader could act on without reading the rest of the document.
- One closing sentence: this is a screened candidate for a diversified basket, and a
  buy signal here is not a recommendation to concentrate savings in one stock.

## Finally: a note on the buy case itself

One or two sentences, only if warranted: is any trigger now unusable — already
satisfied when written, impossible to observe, anchored to a figure that has since
been restated, or overtaken by events? Say so plainly and recommend regenerating the
buy case with `python buy_case.py TICKER` rather than quietly working around it.
