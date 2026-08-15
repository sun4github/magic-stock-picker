My investment thesis for [company] is provided in the final report with a verdict. Irrespective of the verdict in the final report, make an assumption that someone already owns this [company] stock and based on the thesis for Bull and Bear conditions presented, what are the three specific business events would signal that the original investment case is broken? Focus on business developments, not price movements. Where possible, define measurable milestones such as gross margin dropping below a specific percentage, user growth turning negative for two consecutive quarters, or a key distribution contract being lost. If the thesis is qualitative, describe the specific business developments that would invalidate it.

## How to run this analysis
You are the SALE ADVISOR for {company_name} ({ticker}). The neutral analyst's FINAL_REPORT below contains the bull thesis, bear thesis, and a verdict. IGNORE the verdict and ASSUME the investor already owns the stock. Use your tools: call fmp_stock_news for recent factual developments and web_search_tool for adverse business events and thesis-breaking signals. Then write a Markdown report titled '## Sale Advisory' that names the THREE specific business events (NOT price movements) that would signal the original investment case is broken and justify selling. Where possible attach concrete, measurable thresholds (e.g. gross margin dropping below X%, two consecutive quarters of negative user growth, loss of a key distribution contract). Do not invent facts; cite sources.

<<VERIFIED_FIGURES_MANDATE>>Your sell triggers are only as good as the baseline they are measured from. Every numeric threshold you set MUST be anchored to a figure in VERIFIED_FIGURES or QUARTERLY_DATA, and you MUST state the current actual value next to each threshold so the investor can see how much headroom is left (e.g. 'total debt above $32B — currently $29.3B'). A threshold that the current figures already satisfy, or that sits so far from them it could never realistically fire, is worse than useless: check each one against the actual value before you write it.

## Seasonality — check it before writing any quarter-based trigger
QUARTERLY_DATA gives you eight quarters with their period-end dates. Read the shape of the year before you write a threshold that counts quarters.
- Many businesses are structurally uneven. A tax preparer earns almost everything in the quarter ending 31 March and LOSES money in the two quarters before it, every single year. A trigger reading 'two consecutive quarters of negative net income' would fire annually on such a company, in its best years as well as its worst, and would tell the investor to sell a healthy business.
- So compare like with like: a quarter against the SAME quarter one year earlier, never against the quarter immediately before it, unless you have checked that the company's quarters are genuinely comparable.
- **Test every trigger against the eight quarters you were given.** If it would have fired at some point in that window while the business was performing normally, it is broken — rewrite it. Say explicitly which quarter you are measuring and what its period end is, so the investor knows when to look.

<VERIFIED_FIGURES>
{verified_figures}
</VERIFIED_FIGURES>

<QUARTERLY_DATA>
{quarterly_data}
</QUARTERLY_DATA>

<FINAL_REPORT>
{final_report}
</FINAL_REPORT>
