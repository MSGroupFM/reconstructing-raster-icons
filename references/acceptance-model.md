# Acceptance model 1.0.3

## Score

All raw calculations use `float64`; compare the unrounded composite with the confirmed target. Round half-up to two decimals only for reporting.

Let `delta = max(1, round_half_up(0.001D))`, `tau = 0.02D`, where `D` is the canonical-canvas diagonal. Frozen uncertainty pixels are excluded as defined by the schemas and evaluator.

```text
precision = |P ∩ dilate(R, delta)| / |P|
recall    = |R ∩ dilate(P, delta)| / |R|
S         = 100 × 2 × precision × recall / (precision + recall)

d_sym = mean of the two directed contour means after
        min(max(distance - delta, 0), tau) / tau
C     = 100 × (1 - d_sym)

e_center = min(1, centroid_distance / (0.05D))
e_width  = min(1, |ln(width_P / width_R)| / ln(1.25))
e_height = min(1, |ln(height_P / height_R)| / ln(1.25))
e_area   = min(1, |ln(area_P / area_R)| / ln(1.50))
L_i      = 100 × (1 - 0.40e_center - 0.20e_width
                    - 0.20e_height - 0.20e_area)
L        = frozen weighted mean of L_i

T = 50 × F1(component/hole node facts) + 50 × F1(topology edge facts)
score = 0.45S + 0.30C + 0.15L + 0.10T
```

Empty-set and topology semantics are normative in the schemas/evaluator; do not substitute another metric.

## Mandatory gates

Automatic gates are `pass|fail`: safe subset, canonical render, hashes, component presence, topology facts, viewport geometry, primitive constraints, path integrity, and monochrome style.

Semantic gates are `pass|fail|not_evaluated`: component completeness, connectivity, editability, visual meaning, target sizes, overlay/diff, and ambiguities. Every gate needs evaluator, UTC time, and artifact/hash or textual basis. Score cannot compensate for a failed gate.

## Status and exit precedence

1. `invalid_input` / `2`
2. `runtime_error` / `7`
3. `non_canonical` / `6`
4. `not_accepted` / `4` for failed gate, otherwise `/3` below target
5. `incomplete` / `5` for missing or `not_evaluated` semantic review
6. `accepted` / `0`

Accepted requires canonical hashes, score at target, and all 16 mandatory gates `pass`.

## Stop contract

The default `refinement_limit` is `8` after baseline; allowed range is `1..20`. At iteration `k >= 3`, stop as `stalled` when:

```text
best_score[0..k] - best_score[0..k-3] < 0.10
```

and no mandatory gate changes from `fail|not_evaluated` to `pass` during `k-2..k`. `stop_reason` is `accepted|stalled|iteration_limit|user_stopped|error`, independent of status. Stall/limit never authorizes changing target, tolerance, map, or uncertainty; return the best candidate as `not_accepted` or `incomplete` and list remaining differences.
