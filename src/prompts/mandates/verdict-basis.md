## Verdict Basis (mandatory, machine-read — write it LAST)
After '## What Would Make This Wrong', append a final section headed exactly '## Verdict Basis' containing ONE fenced ```json block and nothing else. It records the factors that actually drove your verdict, so the system can later check which factors it reads well and which it misreads.
Format — a JSON array of 3 to 7 objects:
```json
[{"factor_key": "customer_concentration", "stance": "BEAR", "materiality": "HIGH", "observation": "Two sentences, plain English, specific to this company and citing the figure that matters."}]
```
Rules:
- `factor_key` MUST come from the list below. If a factor genuinely driving your verdict is not on the list, use `OTHER_<short_snake_case_name>` — do not force it into an approximate match, and do not coin a new bare key.
- `stance` is BEAR (argues against buying) or BULL (argues for). `materiality` is HIGH, MEDIUM or LOW — how much this factor moved YOUR verdict, not how dramatic it sounds.
- Include the factors that DECIDED it, including ones that cut against where you landed. A basis listing only factors agreeing with the verdict is a record of a conclusion, not of reasoning.
- Do NOT list the screen's entry criteria (high return on capital, high earnings yield, low PEG, EPS growth). Every candidate has those; they distinguish nothing and would swamp the ledger with a constant.
- `observation` is exactly two sentences and must contain the specific number, date or event — not a restatement of the factor's name.
Permitted factor_key values:
<<FACTOR_KEYS>>

