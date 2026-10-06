# Shift Report & Incident Summarization Agent

AI agent for summarizing manufacturing shift logs into shift-end reports with incidents and follow-ups, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline)
> **Industry**: Manufacturing
> **Template ID**: MFG-C2-004

## Overview

Turns a shift's raw machine logs into the shift-end report a production supervisor
actually hands over: computed production metrics, the anomalies worth someone's attention, and a
short list of follow-up actions.

Feed it a shift log — timestamped machine events, stoppages, alarm codes and production counts —
and it validates every record, computes OEE and its component rates (availability, performance,
quality) alongside unit counts and downtime, flags stoppages that consumed more of the shift than
your configured threshold, detects alarm bursts on a single machine and quality outliers across the
shift, and renders the result as a structured Markdown report.

It is aimed at handover automation on the production floor, where the knowledge of what mattered
during a shift has historically lived with the supervisor who worked it.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | 3.11 or later |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded mode.
The agent imports the framework packages at start-up, so without them the process fails while
importing rather than serving requests in a partially working state. This is intentional — a
half-running agent is worse than one that refuses to start.

The narrative section of the report is the one part that degrades rather than failing: when no
language-model service is provisioned, the summary is composed from the metrics and anomalies the
pipeline computed, so the report is still derived entirely from your own shift data.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
