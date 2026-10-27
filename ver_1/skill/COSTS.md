# API unit costs and accounting

All amounts below are USD. Token rates are per **1,000,000 tokens**, not per record or API call. The current shared estimator is [`cost_of()`](scripts/llm_extract.py); all specialized API stages import it.

## What this snapshot actually calculates

The preserved code constants are `PRICE_IN = 2.00` and `PRICE_OUT = 10.00`. Cache creation is weighted at 1.25 times input and cache reads at 0.10 times input. That means:

- Fresh input: **$2.00 / million tokens**.
- Output: **$10.00 / million tokens**.
- Cache creation: **$2.50 / million tokens**.
- Cache read: **$0.20 / million tokens**.

```text
estimated_token_cost =
  (fresh_input * 2.00
   + cache_write * 2.50
   + cache_read * 0.20
   + output * 10.00) / 1,000,000
```

For example, 10,000 fresh input and 1,000 output tokens cost **$0.03** under these constants. Adding 10,000 cache-read tokens adds **$0.002**. These are arithmetic examples, not measured per-site costs.

The code comment describes historical introductory pricing. These fixed constants do not automatically follow model changes, pricing updates, context tiers, cache lifetimes, or account-specific charges. Treat `비용_USD` as the snapshot's token estimate, not a reconciled invoice. `batch=True` halves that estimate, but batch execution is outside this archive's intended sequential workflow.

## Where each function spends money

- **Collection, rules, browser capture, annotation, merge, comparison, XLSX and location maps:** no model API charge. Network, compute and storage still have infrastructure costs.
- **[Text review](agents/text-review/README.md):** input text, prompts, tool schema, and generated extraction output. Default output cap: 3,000 tokens, configurable with `DISCLOSURE_LLM_MAX_TOKENS`.
- **[Vision diagnosis](agents/vision-diagnosis/README.md):** text plus image input tokens and output; output cap 4,000 tokens. It is not billed at one fixed amount per screenshot.
- **[Publication date](agents/publication-date/README.md):** saved main/article evidence and potentially multiple calls; output cap 2,000 tokens per call.
- **[Failed-site search](agents/site-resolution/README.md):** model tokens plus separately billed server-side searches; output cap 3,000 tokens and configured maximum four tool uses per request.
- **[Site classification](agents/site-classification/README.md):** saved I-3 text; output cap 700 tokens. This is a separate utility, not every record's default path.
- **[Run inspection](agents/run-inspection/README.md):** aggregate diagnostics plus model interpretation; output cap 3,000 tokens. `--no-llm` computes metrics without API judgment.
- **Review UI:** no built-in model calls. Reprocessing requests are stored/exported, with no connected execution worker.

Output caps are upper bounds for one response, not fixed usage or an end-to-end budget. Retries, repair turns, and multi-stage routing can create additional calls.

## Official pricing and search charges

Checked 2026-10-27 against [Anthropic's official pricing](https://platform.claude.com/docs/en/about-claude/pricing): the displayed Sonnet 5 rates are $2 input, $10 output, $2.50 five-minute cache creation and $0.20 cache read per million tokens. Web search is **$10 / 1,000 searches**, equivalent to **$0.01 per billed search**, in addition to token costs. Recheck that source before a paid run; other models and tiers differ.

**Accounting gap:** `resolve_failed.py` records a count of returned `server_tool_use` blocks, but the shared `cost_of()` ignores that count. Its `비용_USD` and budget tracking therefore exclude separate search fees. Four billed searches would add **$0.04** to a request, before tokens. The returned block count is not an authoritative billing reconciliation, especially for tool errors. Do not read the resolver's budget as an all-in spending cap.

## Estimate a run

Start from the number routed into each agent, multiply by measured average token usage under the selected rates, then add actual billed searches. Account for cache read/write separately and include retries. Use per-record `work/` and `audit/` usage artifacts where the stage records them; reconcile against provider usage/billing for final totals. No live cost benchmark is claimed by this archive.
