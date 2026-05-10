# Statistical Modeling for Bike Safety Scoring

This document describes the statistical models implemented to predict bike safety scores based on OpenStreetMap (OSM) network features. The pipeline explicitly uses exploratory data analysis (EDA) to guide feature engineering and hypothesis testing, then evaluates models with MSE, R², adjusted R², and cross-validation statistics.

## Overview

The statistical modeling approach complements the AI-based scoring (using Gemini) by providing interpretable models that can predict safety scores from readily available OSM data. This allows for:

- **Scalability**: Score new road segments without API calls
- **Interpretability**: Understand which features most influence safety
- **Validation**: Statistical significance testing of relationships
- **Cost Efficiency**: No API costs for predictions

## Features Engineered

The following features are extracted from OSM tags and network geometry:

- **length_m**: Length of the road segment in meters
- **highway_encoded**: Categorical encoding of road type (primary=5, residential=2, cycleway=1, etc.)
- **maxspeed_encoded**: Speed limit (numeric, capped at 50 km/h)
- **lanes_encoded**: Number of lanes (numeric, default 2)
- **cycleway_encoded**: Binary indicator for dedicated bike infrastructure
- **surface_encoded**: Surface quality score (asphalt=1, dirt=4, etc.)
- **oneway_encoded**: Binary indicator for one-way streets

## Models Implemented

### 1. Linear Regression (Statsmodels)
- **Purpose**: Establish baseline relationships with significance testing
- **Features**: OLS regression with constant term
- **Evaluation**: F-statistic, t-tests for coefficients, VIF for multicollinearity
- **Output**: Detailed summary with p-values and confidence intervals

### 2. Random Forest Regressor
- **Purpose**: Capture non-linear relationships and feature interactions
- **Features**: 100 trees, default hyperparameters
- **Evaluation**: Feature importance rankings
- **Output**: Prediction accuracy and variable importance

### 3. Scikit-learn Linear Regression
- **Purpose**: Comparison with statsmodels implementation
- **Features**: Standard linear regression
- **Evaluation**: Standard metrics

## Exploratory Data Analysis (EDA)

The EDA includes:

- **Descriptive Statistics**: Distribution of features and target variable
- **Correlation Analysis**: Pearson correlations between features and safety scores
- **Multicollinearity Check**: Variance Inflation Factor (VIF) analysis
- **Visualization**: Histograms, scatter plots, and correlation heatmaps
- **Hypothesis tests**:
  - Spearman correlation for speed vs safety
  - Welch's t-test for cycleway presence vs safety
  - Spearman correlation for surface quality vs safety
  - Skipped-test reporting when features have no variation

## Hypotheses Tested

1. **H1**: Higher-speed roads have lower safety scores
2. **H2**: Roads with dedicated bike infrastructure have higher safety scores
3. **H3**: Longer road segments have more variable safety scores
4. **H4**: Road surface quality significantly impacts safety perception

## Running the Analysis

After collecting safety scores via AI (Gemini), run:

```bash
# Install dependencies
uv sync

# Run statistical analysis
python -m app.scoring.pipeline statistical-analysis --output-dir analysis_results
```

This will generate:
- `analysis_summary.json`: Key metrics and insights
- `comprehensive_analysis.json`: Clean JSON-safe report (no NaN/Infinity) with EDA insights, significance tests, and model comparison
- `correlation_heatmap.png`: Feature correlation visualization
- `feature_distributions.png`: Feature distribution plots
- `scatter_plots.png`: Relationships with target variable
- Model pickle files for deployment

## Model Performance Metrics

| Model | MSE | R² | Adjusted R² |
|-------|-----|----|-------------|
| Linear Regression | 0.0435 | -0.0213 | -0.0261 |
| Ridge Regression | 0.0429 | -0.0087 | -0.0087 |
| Random Forest | 0.0361 | 0.1528 | 0.1528 |
| Gradient Boosting | 0.0277 | 0.3488 | 0.3488 |

## Key Findings

- Best model on holdout set: **Gradient Boosting** (R² = 0.349, MSE = 0.0277)
- Most important feature in tree-based models: **length_m**
- Current data limitation: only **3 measured scores out of 4261** total, so many feature-level hypothesis tests are skipped due to low variation
- Linear model global significance currently weak (F-test p-value > 0.05), indicating limited linear explanatory power

## Integration with AI Scoring

The statistical models can be used to:
- **Pre-filter**: Identify high-risk segments for AI analysis
- **Post-validate**: Compare AI scores with statistical predictions
- **Gap-fill**: Score segments without Street View coverage
- **Explainability**: Provide feature-based explanations for AI scores

## Future Improvements

- Collect ground truth safety data for supervised learning
- Include temporal features (traffic patterns, time of day)
- Add geospatial features (proximity to schools, parks)
- Implement ensemble methods combining statistical and AI approaches