# ADR-0002 — The imposed panel, not the free decision, is the unit of analysis

- **Status:** accepted
- **Implemented by:** `marketlab.forecasting.panel`, `marketlab.agents.panel`, `marketlab.evaluation.panels`
- **Checked by:** `tests/unit/test_panel.py`, `tests/unit/test_panel_store.py`, `tests/unit/test_analysis.py`

## Context

The obvious thing to score is what the agent freely says: it forecasts what it
chooses to forecast, and we score those forecasts.

That does not work, for a reason that is easy to state and easy to miss. If arm
A forecasts Alpha and Delta while arm B forecasts Beta and Gamma, their Brier
scores are not comparable — they answered different questions. Worse, **which
questions an arm chooses is itself an outcome of the treatment.** An arm with
memory might learn to forecast only the instruments it finds easy. Its score
would improve without its skill improving at all, and the improvement would
look exactly like a memory effect.

This is selection on the dependent variable, arriving through a door most
agent evaluations leave open.

## Options considered

**Score the free forecasts, paired where they happen to overlap.** Rejected:
the overlap is itself treatment-dependent, so restricting to it does not fix
the selection — it concentrates it.

**Score the free forecasts, and control for which instruments were chosen.**
Rejected: the choice is a post-treatment variable. Conditioning on it induces
collider bias rather than removing confounding.

**Ask every arm the same fixed questions, in the same elicitation as the free
decision.** Rejected: the mandated questions would then compete with the free
decision for turns and evidence budget, and a model that spent its budget
answering the panel would produce a worse trade decision. That makes the panel
a treatment in its own right.

**Ask every arm the same fixed questions in a separate, isolated
elicitation.** Chosen.

## Decision

Each cycle poses an **imposed panel**: a fixed list of `(instrument, horizon)`
questions, identical for every arm, derived from the frozen snapshot rather
than from anything an arm said.

The panel is isolated **from the free decision** and deliberately **not from
the condition**:

- *Isolated from the decision:* its own model instance, its own tool budget,
  its own turn count. Nothing said while deciding what to trade is visible
  while answering the panel, and any trade intent that arrives in a panel
  response is discarded rather than executed.
- *Not isolated from the condition:* the panel receives the same injected
  memory/reflection text the decision did. It has to. The panel is where the
  treatment is measured; withholding the treatment from it would measure an arm
  that does not exist.

Every item is answered or the omission is recorded as a `MISSING_PANEL_ITEM`
agent failure. Silence is never read as a shorter answer set — "this condition
declined to answer" is one of the things the study exists to count.

`marketlab.analysis.pairing` pairs strictly on `(date, instrument, horizon)`
within the panel. Free-decision forecasts are recorded and resolved, and are
available for exploratory work, but they are **not** the confirmatory measure.

## Consequences

**Arms become comparable by construction.** Two arms' Brier scores on the same
date, instrument and horizon mean the same thing. This is what makes pairing —
and therefore the removal of most cross-sectional correlation, measured in
[POWER.md](../POWER.md) at a design effect of 1.16–1.20 paired against
1.35–1.73 unpaired — possible at all.

**It costs a second elicitation per arm per cycle.** Model spend roughly
doubles: 120 sessions × 6 arms becomes 1 440 elicitations, not 720. The cost
model prices this in.

**The panel questions are not chosen by any arm, and so are not adapted to
any.** A treatment whose real benefit is *knowing what to look at* will not
show up in the panel score. That effect is real and this design is blind to it
by construction. If memory's actual contribution is question selection rather
than answer quality, this study will report a null.

**Panel answers are an assessment, not a decision.** They never move the
portfolio, so the panel measures forecast quality in isolation from execution.
That is a feature for the primary metric and a limitation for anyone who wants
to know whether better forecasts produce better portfolios.

**A run configured without a panel produces nothing the analysis can
compare.** The panel runs only when a `PanelStore` is supplied — deliberately,
so that a study with no panel has *nothing recorded* rather than an empty panel
recorded as though it had been asked.

## What would make us revisit this

- **Evidence that answering the panel changes the free decision.** The two are
  separate elicitations against fresh model instances, so there is no mechanism
  today — but a provider with server-side session state would create one, and
  that would have to be detected rather than assumed absent.
- **A pilot showing arms differ mainly in *what* they attend to.** Then the
  panel is measuring the wrong thing and a second, selection-sensitive metric
  is needed alongside it — with its own pre-registration, since a metric chosen
  after seeing the panel result is not a confirmatory metric.
- **Cost pressure.** If a real study cannot afford two elicitations per arm per
  cycle, the panel is the one to keep and the free decision is the one to drop
  — which would mean giving up the portfolio arm of the study entirely.
