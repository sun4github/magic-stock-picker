# Goal

A verdict of **Watch** says the company passed a value-and-quality screen but the
case to buy it *today* is not compelling. That is a useful answer and an incomplete
one: it leaves the reader holding a name with no idea what they are waiting for.

Your job is to finish the sentence. Write the **buy case**: the specific price range,
and the specific observable events, at which this particular Watch becomes a Buy —
and write them so precisely that a machine can come back in three months, check each
one against fresh data, and answer yes or no without re-reading the argument.

This is the mirror image of the sale advisory. That one assumes the stock is owned
and names the events that would break the thesis. You assume it is **not** owned and
name the events that would make the thesis worth owning.

# Assumptions

1. The reader is a college-educated adult with no accounting or finance background,
   investing their own savings. Write in plain English. When a financial term earns
   its place, explain it in brackets immediately after — "forward price-to-earnings
   (what you pay today for each dollar of profit analysts expect next year)".
2. The reader does **not** own the stock. Nothing you write is advice to sell, hold,
   or average down.
3. This is one screened candidate in a basket strategy, not a stock tip. A buy
   trigger firing means "this now clears the bar the screen and the analysis set",
   not "this will go up".
4. **Never invent a fact.** "Not disclosed" and "no analyst covers this" are complete,
   useful answers. A fabricated estimate is worse than a gap, because the whole point
   of this document is that someone will act on it later without re-checking it.
5. Cite sources for every forward-looking claim, with dates. A projection with no
   source and no date cannot be checked later, which makes it decoration.

# The evidence you are given, and what each is good for

- **FINAL_REPORT** — the analyst's finished bull case, bear case, and the Watch
  verdict, including the "What Would Make This Wrong" section. **The reason the
  verdict was Watch and not Buy is the starting point of your whole document.** Read
  it first and name it explicitly.
- **VERIFIED_FIGURES** — debt, cash, market capitalisation, enterprise value,
  operating profit, P/E, growth and PEG, computed from the filings by this pipeline.
  Authoritative. They override any number you find anywhere else.
- **QUARTERLY_DATA** — the last 8 quarters with year-over-year comparisons. This is
  where you check whether a trend an analyst is projecting is actually underway.
- **PRICE_DATA** — the current price, the 52-week range, and the 50/200-day averages.
  Every price threshold you write is measured against this.
- Your tools: `fmp_forward_estimates` (consensus revenue/EPS by fiscal year, implied
  forward P/E, analyst counts, price targets and who set them), `fmp_earnings_calendar`
  (next report date for **any** ticker, and how the last four landed),
  `fmp_revenue_segments` (what the company actually sells and where),
  `fmp_pending_ma_filings` (SEC merger filings naming this company),
  `fmp_company_profile` (what another company actually does, before you call it a
  customer or a rival), `fmp_stock_news` (dated factual developments), and
  `web_search_tool` (everything else — reported deals, capital spending plans,
  contract awards, analyst commentary).

# Required output

Write a Markdown document titled `## Buy Case` with **exactly** the seven sections
below, in this order and with these headings. Section 5 is the one that gets checked
later; the first four exist to justify it and the last two to bound it.

## 1. Why this is a Watch and not a Buy

Two short paragraphs. State the single thing that kept the verdict at Watch, quoting
the final report. Then say what would have to change for that objection to go away —
in business terms, not price terms. If the objection is "the valuation already
reflects the good news", say so here; that tells the reader this is a price problem,
not a business problem, and Section 5's triggers will lean on price accordingly.

## 2. Forward valuation — what today's price is asking for

Call `fmp_forward_estimates`. Build a small table of every future fiscal year on
record: period end, consensus revenue, consensus earnings per share, the implied
**forward price-to-earnings at today's price**, and **the number of analysts behind
each figure**.

Then, in prose:

- State the forward P/E for the nearest full fiscal year and **name its basis in the
  same sentence**: the price it is computed at, the fiscal-year end it applies to,
  the consensus EPS, and how many analysts contributed. "About 7.5 times the $5.86
  consensus for the year ending 30 June 2027, at today's $44.03, from three analysts"
  is a usable statement. "Trades at 7.5x forward earnings" is not.
- Compare it to what the reader is paying now — the trailing P/E in VERIFIED_FIGURES
  and the five-year average P/E — and say plainly whether the forward multiple is
  lower because earnings are expected to grow, or because the price has fallen.
- **Say how much of the forecast growth has actually started.** Check the consensus
  against QUARTERLY_DATA. A forecast of 12% revenue growth against two quarters
  already running at 11% is an extrapolation; the same forecast against two quarters
  of decline is a bet on a turn that has not begun, and the difference decides how
  much weight the multiple deserves.
- **Thin or divided coverage must be said out loud.** One or two analysts is one or
  two opinions; a low-to-high EPS spread of 1.5x or more means the mean is a midpoint
  between materially different views of the business. Where the tool flags either,
  repeat the flag in your prose. Where **no** future estimates exist at all, say that
  the forward multiple cannot be computed and build Section 5 on business events and
  the trailing multiple instead. Do not substitute your own forecast.
- Note that these are fiscal years, which for many companies are not calendar years,
  and quote the period-end dates.

## 3. What analysts expect — growth, and deals

Separate what is **CONFIRMED** (announced, contracted, filed, guided by management)
from what is **SPECULATIVE** (rumoured, "sources say", an analyst's model, a target
for a year that is years away). Label every item inline with one of those two words.

Cover:

- **Revenue and earnings growth** other analysts are projecting for the next one to
  three fiscal years — the rate, the source, and the date it was published. Where the
  forward estimates span more than one year, say whether the growth figure you quote
  is cumulative or annual; a 64% rise spread over three years is 18% a year and must
  not be reported as though it were one year's work.
- **Deals**: pending acquisitions, divestitures, major contracts, partnerships, and
  large customer wins. Call `fmp_pending_ma_filings` first — a company under an agreed
  bid trades on the offer price and a valuation-based buy range would be meaningless,
  so this has to be checked before Section 5 is written. That tool covers SEC merger
  registrations only; use `web_search_tool` and `fmp_stock_news` for reported and
  rumoured transactions, and label them SPECULATIVE.
- **The analyst price targets, and who set them.** Give the consensus, the high and
  the low, and then name at least the two most recent individual target changes with
  the firm, the date, and the price when it was published. A consensus target is an
  average of opinions, several of which may be years stale — the individual entries
  are what tell the reader whether the number reflects the company as it is today.
- **Where the disagreement actually is.** One sentence: what do the optimists believe
  that the pessimists do not?

**A speculative catalyst may never be the sole content of a buy trigger.** The same
rule governs the analyst's verdict, and it governs you: rumoured deals are upside on
top of a case that stands on its own. A trigger may be written *about* a speculative
event only in the form of its confirmation — "the acquisition closes", never "the
acquisition is rumoured".

## 4. The ecosystem — whose results move this company's revenue

A company's revenue arrives from somewhere. Name the specific outside parties whose
reported results and spending decisions determine whether the growth in Section 3
happens, and give the reader the dates on which they will find out.

- Use `fmp_revenue_segments` and the customer-concentration disclosure in the final
  report to establish **what actually drives the revenue line** before naming anyone.
  A segment worth 3% of sales does not carry the thesis.
- Identify up to three companies whose results are genuine leading indicators for
  this one, and say **in which direction** each relationship runs:
  - **Downstream customers / consumers of what this company sells.** A chip designer's
    revenue is set by the capital-spending decisions of the cloud operators buying the
    chips, so those buyers' quarterly capital expenditure and their guidance are
    leading indicators for the supplier's next year.
  - **Upstream suppliers**, whose capacity, lead times and pricing cap what this
    company can deliver or set what it pays.
  - **Close competitors**, whose results separate an industry-wide move from a
    company-specific one.
- **Verify each relationship before asserting it.** Call `fmp_company_profile` on any
  company you name and check it actually does what you are claiming. A shared sector
  label is not a supply relationship. Where a real relationship is with a private
  company, a foreign one, or an unnamed customer in a filing, say so — that is a fact
  about the thesis, not a gap to be filled with a listed company that happens to fit.
- For every company you name, call `fmp_earnings_calendar` **on that company's
  ticker** and give its next scheduled report date. Those dates are the calendar the
  reader watches. Note that scheduled dates are provisional until confirmed.
- Include this company's own next earnings date, from the same tool.

## 5. Buy Triggers

The section that gets checked. Write **three to five** triggers, numbered, each in
exactly this shape — the labels are fixed and a later automated check reads them:

- **Trigger 1 — Price.** *(mandatory, and always first)*
  - **Condition:** the buy range, as a price or a band.
  - **Basis:** the arithmetic that produced it, in one sentence.
  - **Currently:** the price today, from PRICE_DATA, with the date.
  - **Check:** what to compare it against, and where.
- **Trigger 2 … N — Event.**
  - **Condition:** one observable, measurable business event.
  - **Basis:** why this event answers the objection in Section 1.
  - **Currently:** the latest actual value or state, with its date.
  - **Check:** the report, filing, or announcement it will appear in, and the
    earliest date it could appear.

Rules that make these worth writing:

1. **The price range must be derived, not chosen.** Show the arithmetic: a multiple
   applied to a stated forward earnings figure, or a discount to a stated analyst
   target, or a level from the 52-week range. "Below $38, which is 6.5 times the
   $5.86 consensus for the year to June 2027 — the low end of its own five-year
   range" can be checked and argued with. "Below $38" cannot.
2. **Both ends of the range, where the case has both.** If the stock becoming cheap
   enough is the whole trigger, an upper bound is enough. If the case needs the
   business to prove something first, say that the price is a *necessary* condition
   and pair it with an event trigger.
3. **Test every threshold against the numbers you were given before you write it.**
   A price trigger already satisfied at today's price is not a trigger, it is a Buy
   verdict written by the wrong agent — and one 60% below the 52-week low is a way of
   never buying. State the gap in percentage terms so the reader can see the distance.
4. **Anchor every numeric event threshold to a figure in VERIFIED_FIGURES,
   QUARTERLY_DATA, or the forward estimates, and print the current actual value beside
   it.** "Operating margin above 12% — currently 9.4% in the quarter ended 30 June"
   is checkable. "Margins improve" is not.
5. **Respect seasonality.** QUARTERLY_DATA gives eight quarters with their period-end
   dates. Compare a quarter to the same quarter a year earlier, never to the one
   before it, unless you have checked the quarters are genuinely comparable. Test each
   trigger against those eight quarters: one that would have fired while the business
   was performing normally is broken — rewrite it.
6. **Every trigger carries a date or a named event.** "By the FY2027 second-quarter
   report, scheduled for early November" or "on completion of the announced
   acquisition". A trigger with no time bound can never be declared failed.
7. **Say how many must fire.** Close the section with one sentence: which triggers are
   necessary, which are sufficient, and how many together make this a Buy. The price
   trigger alone is rarely sufficient — say plainly whether it is here.

## 6. What would take this off the watchlist

Two or three developments that would turn this from "not yet" into "no". These are
not sell triggers — the reader owns nothing. They are the events after which waiting
is pointless and the name should be dropped, so that a Watch does not quietly become
a permanent holding pattern. Give each one a threshold and a current value in the same
style as Section 5.

## 7. How to use this

Four sentences at most: that the triggers are checkable with
`python main.py --buy-check TICKER`, that the price figures are anchored to the date
in Section 5 and move every session, that analyst estimates are revised constantly
and a trigger built on them ages, and that a fired trigger is a screen-level buy
signal for a diversified basket rather than a recommendation to buy one stock.
