# Provider policy

The contract a real language-model adapter must meet, written **before** any
exists, so that meeting it is a design constraint rather than a retrofit.

Today the only implementation is `DeterministicPolicyModel`, a closed-form
function of the closing price that deliberately ignores the material each arm
is granted ([ADR-0017](adr/0017-a-fake-that-ignores-its-context.md)). Every
rule below is therefore currently untested against a real provider, and that is
stated rather than glossed.

## 1. The seam

A provider adapter implements one Protocol:

```python
class LanguageModel(Protocol):
    @property
    def model_id(self) -> str: ...
    def generate(self, request: ModelRequest) -> ModelResponse: ...
```

`marketlab.models` imports nothing internal and depends on no provider SDK.
Everything above it talks to this Protocol only. An adapter translates to and
from its provider's own wire format **at the edge** — mirroring one provider's
exact shape inside the platform would make that provider's quirks look like
part of the core contract.

Tool calling is modelled generically: a turn either asks for more evidence
(`tool_calls`), commits (`decision`), or refuses (`refused`). A response
setting both `tool_calls` and `decision` is treated as malformed rather than
having one of them guessed at.

## 2. Hard requirements

### 2.1 The adapter must not learn which condition it is serving

`ModelRequest` has exactly one field a condition may vary: `injected_context`.
There is no field for the arm, the repetition, or the run, and **none may be
added**.

This is not advice. Three tests enforce it
([ADR-0003](adr/0003-masking-is-partial.md)), and the third — a content scan
over every request produced by a real six-arm cycle — is the one an adapter is
most likely to trip.

**The realistic way an adapter breaks this is not a new field.** It is a
per-request identifier the provider's API wants:

| Provider concept | The trap | What to do instead |
|---|---|---|
| session / conversation id | Deriving it from the arm | Derive from `(run_id, cycle_id)` — never from the condition |
| cache key or prefix id | Keying the cache per arm, which is natural since arms differ | Key on the **shared prefix content**, which is identical across arms by design |
| user / customer id, trace id, tags | Tagging by arm for observability | Do not. Correlate through the event log, which has `arm_id` and is not on the model path |
| a "system fingerprint" echoed back | Nothing — it comes *from* the provider | Record it (§2.4); it does not flow to the model |

Any identifier an adapter attaches must be extended into the content scan.

### 2.2 The adapter must not retry across the condition boundary

Each arm gets a **fresh model instance** per elicitation. An adapter holding
state — a conversation handle, a cached prefix keyed on anything arm-specific,
a rate-limit backoff shared between calls — creates a channel between arms that
the isolation guards do not cover, because they inspect requests and not the
client object.

Retries within one elicitation are fine and expected. Retries that reuse a
handle from a different arm's call are not.

### 2.3 Provider unavailability is `CONDITION_MISSING`, and must be raised

After the allowed retries, an adapter raises `ModelProviderError`. It must not
return an empty response, a refusal, or a plausible default.

`DecisionAgent` deliberately does **not** catch it: a missing provider means
the whole decision is missing, which is a run-level fact
([FAILURE_POLICY.md](FAILURE_POLICY.md) §2), not an agent-level observation to
record and continue past. An adapter that substituted a fallback would convert
an outage into a data point, and the analysis would treat a network problem as
a model's behaviour.

### 2.4 Token usage must be measured, never estimated

`ModelResponse.usage` carries `TokenUsage(input, cached_input, output)` from
**the provider's own accounting**. An adapter that estimates by counting
characters is worse than one reporting nothing, because
`TokenUsage.is_measured` distinguishes "the provider said zero" from "nobody
asked" — and `measure_profile` **refuses** to build a cost profile from a run
that reported nothing, which is exactly what a run against the deterministic
fake reports.

Every figure in [POWER.md](POWER.md) is therefore labelled `ASSUMED`. The first
real pilot is what replaces them, and the CLI will say `MEASURED` when it does.

`cached_input_tokens` is counted separately because it is billed differently
and because in this platform it is the **dominant** term: `DecisionAgent`
resends every accumulated tool result on every turn, so two thirds of the input
is a stable prefix. The measured consequence is a 41% saving at the mid tier
($133 → $78) — and generated tokens, ~40% of the bill, cannot be cached at all.

### 2.5 The model identity must be recorded exactly

`model_id` goes into every decision and panel bundle. It must name the exact
served version, including whatever the provider offers to disambiguate a silent
update — a dated alias, a system fingerprint, a snapshot id.

**This is the largest hole in the pre-registration mechanism.** The run's
configuration is fingerprinted and cannot change
([ADR-0011](adr/0011-a-run-is-declared-not-launched.md)), but if a provider
serves a different model under the same identifier, the fingerprint is
unchanged and the study is not. No code in this repository can detect that. An
adapter that can record a finer-grained identity **must**, and a study whose
provider offers none should say so in its write-up.

### 2.6 Sampling parameters are pre-registered

Temperature, top-p, seed if offered, and any reasoning-effort setting are
study parameters. They belong in `StudyConfig` — inside the fingerprint — not
in adapter defaults. An adapter that hard-codes `temperature=0.7` has made a
scientific decision on the study owner's behalf.

Note that temperature 0 does **not** buy reproducibility: providers do not
guarantee bit-identical outputs across time or hardware. That is why replay
treats sealed decisions as inputs
([ADR-0012](adr/0012-replay-recomputes-downstream-of-the-model.md)) rather than
re-eliciting.

## 3. What the platform guarantees to the adapter

- **Nothing arm-specific is in `ModelRequest` except the granted text.** An
  adapter cannot leak what it is not given.
- **The tool catalogue is stable within an elicitation.**
- **Evidence is pre-budgeted.** The evidence cap is applied before the request
  is built, so an adapter never has to truncate. It is a **character count, not
  a token count** — a real per-model tokenizer would make the budget
  provider-specific, which the core contract forbids at this layer. Crude, and
  deliberately so.
- **Every fact in a tool result is data, not instruction.** The system prompt
  says so, and the synthetic world deliberately scripts a prompt injection at
  session 22 to keep that claim exercised
  ([THREAT_MODEL.md](THREAT_MODEL.md) §3).

## 4. Credentials

Phase 1 handles none. When an adapter needs them:

- They live in the **environment**. Never in `configs/`, never in `data/`,
  never in an event payload, never in a blob.
- `.env`, `*.key` and `secrets.yaml` are already git-ignored.
- An adapter must not log a request body that could contain one.

## 5. Choosing a provider — what the numbers say

From [POWER.md](POWER.md) §7, for 120 sessions × 6 arms with the panel enabled:

| tier (input / output / cached, per Mtok) | projected total |
|---|---:|
| 0.50 / 2 / 0.05 | **$12** |
| 3 / 15 / 0.30 | **$78** |
| 3 / 15 / *no caching* | **$133** |
| 15 / 75 / 1.50 | **$390** |

**Cost is not the binding constraint.** Even a frontier model runs a
full-length study for a few hundred dollars, so the provider should be chosen
on whether it can express the behaviour under study — not on price.

The one thing that *does* matter for provider choice is
[PRE_REGISTRATION.md](PRE_REGISTRATION.md) §10's threat: a model whose
forecasts cluster within a couple of points of 0.5 whatever it is granted has
no skill to differentiate, and every duration in POWER.md becomes an
underestimate. **The pilot must report the observed spread of forecast
probabilities**, not only whether the decisions differ.

## 6. What a Phase 3 adapter must ship with

Not optional, because each closes a claim this document currently cannot make:

1. **A recorded-response fixture** of a real conversation, so the orchestration
   is testable without spending money on every CI run.
2. **An extension of the condition-isolation content scan** covering every
   identifier the adapter attaches (§2.1).
3. **A measured token profile** replacing the `ASSUMED` figures, produced by
   `measure_profile` over a real pilot run.
4. **An entry in [ROADMAP.md](ROADMAP.md)** naming the test that substantiates
   it — the roadmap is the only place completeness is claimed, and a provider
   adapter is the single largest completeness claim this project will make.
