# HackDavis Statistical Modeling Report (Judge One-Pager)

## Core question
Can we predict cyclist safety scores from OSM roadway features with statistically grounded modeling?

## Dataset and EDA summary
- Total scored segments: 4261
- Measured (non-inferred) scores: 3 (0.07%)
- Engineered features used in modeling: 4
- EDA informed model choice by checking feature distributions, outliers, correlations, and multicollinearity.
- Top target correlations:
- `length_m`: |corr| = 0.0392
- `highway_speed_interaction`: |corr| = 0.0381
- `highway_encoded`: |corr| = 0.0101
- `intersection_density`: |corr| = 0.0101
- `maxspeed_encoded`: |corr| = N/A

## Model evaluation
| Model | MSE | R² | Adjusted R² | CV R² Mean | CV R² Std |
|---|---:|---:|---:|---:|---:|
| gradient_boosting | 0.0277 | 0.3488 | 0.3488 | -0.0390 | 0.2173 |
| random_forest | 0.0361 | 0.1528 | 0.1528 | 0.0634 | 0.2949 |
| ridge_regression | 0.0429 | -0.0087 | -0.0087 | -0.0030 | 0.0104 |
| linear_regression | 0.0435 | -0.0213 | -0.0261 | -0.0030 | 0.0104 |

- Best holdout model: `gradient_boosting`

## Significance tests and assumptions
- Global linear model F-test p-value: 0.1639
- Breusch-Pagan p-value (heteroscedasticity): 0.1153
- Jarque-Bera p-value (residual normality): 0.0000
- Hypothesis tests:
- `H1_speed_vs_safety`: skipped (maxspeed_encoded has no variation in current dataset)
- `H2_cycleway_vs_safety`: skipped (has_cycleway has no variation in current dataset)
- `H4_surface_quality_vs_safety`: skipped (surface_quality has no variation in current dataset)

## Key findings
- Best performing model: gradient_boosting (R² = 0.349)
- Most important feature: length_m
- Dataset quality: 3/4261 measured scores.
- No pre-registered hypothesis reached p<0.05 support.

## Limitations and next step
- Current measured-label coverage is low, which weakens inferential power.
- Next high-impact step: collect more measured safety labels and rerun the same pipeline to strengthen hypothesis testing and generalization claims.
