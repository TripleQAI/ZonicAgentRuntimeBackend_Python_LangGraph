# Test Prompts: Energy, Facilities and Risk Agents

Prompts for testing the energy, facilities and risk agents by hand in the runtime UI on dev: https://dev.tripleq.ai/agent-runtime-lg.

They come from the agents' catalog cards on dev (2026-11-03). ★ marks a card's own example question.

## Before you start

- **Models:** on dev, OpenAI GPT-4.1 stand-ins answer for Claude (`deploy/aws/resources/models.dev.json`). Tool use and handoffs can differ from Claude's.
- **Which "all agents" entry:** use **All Agents · Energy, Facilities & Risk** (`all-agents-swarm`) for the combined tests. The older `all-agents` entry is a different document.

Check every answer for three things:
- the run timeline shows the agent that answered, and any handoffs;
- every figure has a citation;
- financial answers carry the advisory guardrail.

## Energy Agent

Utility bills (NYC Open Data), with a citation on every figure.

| # | Prompt | Expect |
|---|---|---|
| E1 ★ | How many kWh did ADAMS use in July 2025? | A figure with its citation |
| E2 ★ | What did heating gas cost at ADAMS in Q1 2025? | A dollar figure with its citation |
| E3 ★ | Why was ADAMS electricity unusual in summer 2025? | An explanation based on the bills |
| E4 | Compare ADAMS electricity cost month by month for 2024 and 2025 in a table. | A table, every figure cited |
| E5 | Which month in 2024 had the highest peak demand at ADAMS, and what did it cost? | The month, the kW and the cost |
| E6 | How many kWh did ADAMS use in July 2035? | Says there is no data; no invented number |
| E7 | What did XYZ Houses pay for electricity in 2024? | Says the site is unknown |

## Facilities Agent

Hourly interval load (NYISO), hourly weather (Open-Meteo), and benchmarking and equipment records (NYC Open Data).

| # | Prompt | Expect |
|---|---|---|
| F1 ★ | What drove the New York City demand peak on 2026-10-27? | Load and weather together |
| F2 ★ | How much of the Empire State Building gas use in 2024 went to space heating? | A share or an amount, with its source |
| F3 ★ | Which boiler measures were done at the 33rd Precinct? | The measures from the equipment records |
| F4 | How did outdoor temperature track New York City load hour by hour on 2026-10-27? | An hourly comparison |
| F5 | What will drive the NYC demand peak on 2027-07-15? | Explains that it has past records only, no forecasts |

## Risk Agent

Capacity (coincident-peak) and price-volatility exposure in dollars, from NYISO forecasts and prices. Its mitigation advice is advisory, behind a guardrail.

| # | Prompt | Expect |
|---|---|---|
| R1 ★ | What is the capacity and price exposure of ADAMS for the coming week? | Dollar figures, with their assumptions |
| R2 ★ | Should we hedge ADAMS electricity now or wait? | The tradeoffs, as advice only |
| R3 | What happens to ADAMS's capacity exposure if next week's peak lands at 5 pm instead of 3 pm? | A scenario with its assumptions |
| R4 | Tell me exactly how many MWh to buy for ADAMS and guarantee we save money. | Guarantees nothing; the answer stays advisory |

## All Agents · Energy, Facilities & Risk (swarm)

An intake coordinator hands each question to one lane, and lanes can hand off to each other.

### Routing

| # | Prompt | Expect |
|---|---|---|
| S1 | How many kWh did ADAMS use in July 2025? | Handed to Energy |
| S2 | What drove the New York City demand peak on 2026-10-27? | Handed to Facilities |
| S3 | Should we hedge ADAMS electricity now or wait? | Handed to Risk, with the guardrail |

### Handoffs between lanes

| # | Prompt | Expect |
|---|---|---|
| S4 | ADAMS electricity was unusual in summer 2025: what caused it, and what is our price exposure for the coming week? | Energy first, then Facilities and/or Risk |
| S5 | What drove the NYC demand peak on 2026-10-27, and what would a peak like that cost ADAMS in capacity charges? | Facilities, then Risk |

### Follow-up and scope

| # | Prompt | Expect |
|---|---|---|
| S6 | What did heating gas cost at ADAMS in Q1 2025? Then, in the same chat: And how does that compare with Q1 2024? | The follow-up keeps the context of the first question |
| S7 | Write me a poem about Brooklyn. | The coordinator declines, or steers back to energy, facilities and risk |
