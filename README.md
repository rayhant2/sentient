<p align="center">
  <img src="public/Sapient_Logo.png" alt="Sapient Logo" width="100">
</p>

# Sapient

Sapient is a multi-user portfolio monitoring system that combines shared market
data, personalized position context, deterministic monitoring, and LangGraph
agents. It monitors and advises; it does not connect to a brokerage or execute
trades.

## Current foundation

- Twelve Data 15-minute OHLCV ingestion with a rolling 150-candle history
- Supabase persistence behind a single database interface
- In-memory ticker/subscriber event routing
- Rate-limited, prioritized APScheduler refresh coordination
- Volatility-aware sharp-move detection with personalized thresholds
- Per-user LLM credentials required before agent execution

## Five planned agents

| Agent | Trigger | Responsibility |
|---|---|---|
| Sharp Move Investigation | Intraday threshold or volatility anomaly | Investigate a sudden move and explain its effect on the user's position |
| Cross-Portfolio Reasoning | Completion of a user update cycle | Identify correlations, concentration, and combined portfolio risk |
| Motive Reassessment | Weekly per subscription | Compare the user's stated reason for the position with current evidence |
| Proactive Hypothesis Generation | Dynamic per-ticker cadence | Look for developing patterns before they become obvious |
| Scheduled Position Review | Daily or weekly per subscription | Provide the routine general assessment and recommendation for an owned or watched stock |

The Scheduled Position Review Agent consumes the existing `scheduled_update`
event. It reviews recent price behaviour, cost basis, P&L, volatility, prior
updates, alerts, and relevant external context. It is the normal daily or weekly
review path and is distinct from event-driven sharp-move analysis.

All five agents will use the API key belonging to the user whose position is being
analyzed. User credentials must never enter prompts, agent state, alerts, update
records, WhatsApp messages, logs, or LangSmith traces.

## Later: opportunity discovery

A separate Opportunity Discovery Agent is planned after the five core agents and
delivery surfaces are stable. It will scan a bounded market universe, filter
candidates quantitatively, and research only a small shortlist. It will produce
opportunities for further research with explicit catalysts and risks, not automatic
buy instructions.

Opportunity discovery is not included in the current five-agent count because it
requires separate market-data coverage, cost controls, ranking logic, licensing
review, and safety evaluation.

## Core flow

```text
Twelve Data → Supabase → Scheduler / Monitor → EventBus
    → per-user AgentContext → LangGraph agent → update / alert
    → WhatsApp and dashboard
```

See `AGENTS.md` for the complete architecture and planned build order.
