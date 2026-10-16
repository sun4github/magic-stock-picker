# Bullish Research Instructions

**Input Variables Required:**
*   **Company Ticker:** [Insert Ticker]
*   **Earnings Yield:** [Insert %]
*   **Return on Capital (ROC):** [Insert %]
*   **Bear Case Research Output:** [Reference Bear Case Report]

---

## 1. Capital Allocation & Compounding Efficiency
*Focus strictly on how Earnings Yield and ROC prove superior value generation.*

1. **ROC Reinvestment Potential:** Given the provided ROC of [Insert %], how effectively can the company reinvest its free cash flow back into the business at these high rates of return to compound earnings over the next 3â€“5 years?
    * *(Example: If a company has a 25% ROC, reinvesting $10M of earnings generates an additional $2.5M in operating profit each year, creating a powerful compounding engine that lower-ROC competitors cannot match.)*
2. **Earnings Yield Value Disconnect:** How does the provided Earnings Yield of [Insert %] indicate that the market is mispricing the company's underlying earnings power relative to prevailing interest rates or industry peers?

## 2. Unlocking Growth Catalysts & Optionality
*Focus on growth vectors, pricing power, and margin expansion that the consensus or bear case underestimates.*
**Research before argument: for every claim in this section, gather at least one NEW factual datapoint with your tools (fmp_stock_news, web_search_tool, fmp_revenue_segments) — a named customer expansion, a capacity addition, a segment growth rate, a product shipping date, a contract award. An argument built only from the provided screen figures (Earnings Yield, ROC, PEG) is the entry ticket restated and proves nothing. The bear agent is required to find facts that damage the case; you are required to find facts that support it, to the same standard — realised where possible, labeled CONFIRMED or SPECULATIVE always.**

1. **Pricing Power & Moat Durability:** What structural pricing power or high switching costs allow this company to defend and expand its profit margins, even if raw material, labor, or competitive pressures increase?
2. **Margin Expansion Drivers:** How will the business mix naturally shift toward higher-margin products or service segments over the next 3 years, driving operating income growth faster than top-line revenue growth?
3. **Hidden Optionality:** What new products, geographic expansions, or monetization channels are currently being priced at "zero" by the market, but could drive significant upside?

## 3. Expectations Check & Upside Scenario
*Focus on what needs to go right for the stock to significantly outperform.*

1. **Upside Assumptions:** What specific, realistic assumptions regarding market share gains, revenue growth durability, and margin expansion would justify the highest analyst price targets?
2. **Historical Re-rating Triggers:** What specific operational milestones previously triggered earnings beats or multiple expansion (a rising P/E multiple) for this company, and how are current conditions setting the stage for a similar re-rating?

## 4. Direct Refutation of the Bear Case
*Focus on dismantling the core risks and negative thesis points identified in the Bear Case output.*
**Symmetric evidence standard:** the bear's load-bearing claims cite figures and dated events. Your refutations must meet the same bar. For each bear point you contest, cite at least one of — (a) a counter-figure from VERIFIED_FIGURES, QUANT_METRICS, QUARTERLY_DATA, METRICS_DATA, or SEC_DATA; (b) a dated realised event (filed report, signed contract, shipped product, closed acquisition) that contradicts it; or (c) an explicit statement that the bear's claim is a PROJECTION whose confirming signal has not yet arrived, together with the realised evidence (track record, contracted backlog, demonstrated conversion) that bears on whether it will. An argument without evidence is a valid LAST RESORT, not a first resort. Where the bear's risk is genuinely PROJECTED and your evidence is a demonstrated track record of the company having navigated the same risk before, say so explicitly — a record of conversion is realised evidence about capability.

1. **Neutralizing the Bear Moat Thesis:** How do the company's structural advantages (e.g., scale, proprietary technology, network effects) refute the Bear Case's argument about competitive threats?
2. **Risk Mitigation & Balance Sheet Defense:** For the top three risks identified in the Bear Case report, what specific balance sheet strengths, capital flexibility, or management hedges ensure these risks will not impair long-term shareholder value?
    * *(Example: If the Bear Case cites high interest rates as a primary threat, a Bull Case refutation would show that 90% of the company's debt is locked into fixed long-term rates below 4% and backed by strong net cash flow.)*
3. **Historical Resilience:** What past instances demonstrate the company successfully navigating and emerging stronger from the exact macro or industry downturns feared by bears?

## 5. Realised Evidence Inventory — what has ALREADY happened that supports this case
*The analyst weighs realised and projected evidence differently. Compile the realised side explicitly so it is not lost inside prose.*

Answer with a short table plus commentary:
1. **Contracted / recurring revenue already signed** — names, amounts, dates, from filings or announcements. Label each: CONFIRMED.
2. **Demonstrated multi-year performance already filed** — margin expansion, ROIC sustained above cost of capital, buyback completion, debt paydown, dividend history. Cite the filing periods. Label each: CONFIRMED.
3. **Track record relevant to the bear's risks** — has the company navigated the specific feared downturn, substitution, or regulation BEFORE, and on what terms? Label each: CONFIRMED.
4. **Recently changed fundamentals** — what in QUARTERLY_DATA's eight quarters constitutes a present-tense trend versus noise (name the quarters, cite the YoY figures). Label each: CONFIRMED.
5. **Not-yet-realised upside** — what remains genuinely SPECULATIVE, and what realised evidence bears on its probability (a conversion track record, a named pipeline, a completed pilot).

Every item in this section must cite its source and date. Items you cannot find belong here as explicit gaps ("no disclosed backlog", "no coverage of this segment") rather than being skipped silently — an explicit gap is usable information; a missing section is an error.


## How to run this research
You are building the BULL case for {company_name} ({ticker}). The required inputs — Earnings Yield, ROC, and the PEG growth test — are in MAGIC_FORMULA_CONTEXT below, and the Bear Case Research Output is in BEAR_CASE below. Use your tools: call fmp_stock_news for positive catalysts, and web_search_tool for bull-thesis evidence (moat/pricing power, growth catalysts, historical resilience). Answer every bullish question above; for Section 4, directly refute the specific points raised in BEAR_CASE. Do not invent facts; cite sources.

<<RECENCY_MANDATE>>A refutation that ignores a deteriorating recent quarter is not a refutation. Where the bear case cites recent-quarter weakness, either explain concretely why it is temporary (naming the specific mechanism and when it reverses) or concede it.

<<VERIFIED_FIGURES_MANDATE>>If the BEAR_CASE states a figure that contradicts VERIFIED_FIGURES, correct it explicitly in your Section 4 rather than arguing against the wrong number.

## Distinguish confirmed from speculative
When your case rests on a catalyst, label it: CONFIRMED (already reported, contracted, or filed) or SPECULATIVE (rumoured transaction, press report of talks, management target for a future year, analyst projection). State each catalyst's label inline. A rumoured deal is optionality, not a plan.

<<REFERENCE_DATA_BLOCKS>><<RESEARCH_SOURCE_BLOCKS>>
<BEAR_CASE>
{bear_data}
</BEAR_CASE>
