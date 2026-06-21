"""Category-pair weight dispatch for Pipeline Stage 3 (Pairwise Scoring).

The V1 substitute for XGBoost (rules §11). A single `Dict[Tuple[str, str],
WeightConfig]` maps `(source_category, target_category)` pairs to a
weight profile; unconfigured pairs fall through to `DEFAULT_WEIGHTS`.
The table is deliberately small (one configured profile in V1) so the
tuning surface is interpretable and grep-able.

`WeightConfig` carries:

- Five multiplicative weights on the string-metric signals
  (token_sort, token_set, partial, jaro_winkler, ngram_jaccard).
- One additive `alias_boost`, applied post-weighted-sum when the
  incoming entity matches any candidate alias above the boost
  threshold. Treated as the sixth "tier-1 evidence" share — included
  in the sum-to-1.0 invariant.
- One additive `abbreviation_bonus`, gated by the PSA shortcode
  heuristic. NOT included in the sum-to-1.0 (it's upside, not budget).
- `profile_id` for debuggability: surfaces on every `ScoredMatch`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WeightConfig:
    token_sort_ratio: float
    token_set_ratio: float
    partial_ratio: float
    jaro_winkler: float
    ngram_jaccard: float
    alias_boost: float
    abbreviation_bonus: float
    fasttext_cosine: float
    profile_id: str


DEFAULT_WEIGHTS: WeightConfig = WeightConfig(
    token_sort_ratio=0.25,
    token_set_ratio=0.25,
    partial_ratio=0.15,
    jaro_winkler=0.10,
    ngram_jaccard=0.10,
    alias_boost=0.15,
    abbreviation_bonus=0.0,
    fasttext_cosine=0.0,
    profile_id="default_v1",
)


PSA_ACCOUNTING_WEIGHTS: WeightConfig = WeightConfig(
    token_sort_ratio=0.25,
    token_set_ratio=0.25,
    partial_ratio=0.15,
    jaro_winkler=0.10,
    ngram_jaccard=0.10,
    alias_boost=0.15,
    abbreviation_bonus=0.20,
    fasttext_cosine=0.35,
    profile_id="psa_accounting_v1",
)


_DISPATCH: dict[tuple[str, str], WeightConfig] = {
    ("accounting", "psa"): PSA_ACCOUNTING_WEIGHTS,
    ("psa", "accounting"): PSA_ACCOUNTING_WEIGHTS,
}


def get_weights(source_category: str, target_category: str) -> WeightConfig:
    """Return the `WeightConfig` for `(source_category, target_category)`.

    Falls through to `DEFAULT_WEIGHTS` when the pair has no configured
    profile. V1 only configures the cross-category cell (accounting↔psa
    in either direction); other pairs are filtered out by Stage 2d's
    intra-system rule, so default firing is a safety net rather than a
    common path.
    """
    return _DISPATCH.get((source_category, target_category), DEFAULT_WEIGHTS)
