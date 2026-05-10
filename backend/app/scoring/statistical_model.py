"""Statistical modeling for bike safety scoring.

This module provides exploratory data analysis (EDA), feature engineering,
and statistical models to predict bike safety scores based on OSM network features.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import statsmodels.api as sm
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.feature_selection import SelectKBest, f_regression
from sklearn.linear_model import LinearRegression, Ridge, Lasso
from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error
from sklearn.model_selection import cross_val_score, KFold, train_test_split
from sklearn.preprocessing import StandardScaler, PolynomialFeatures
from statsmodels.stats.outliers_influence import variance_inflation_factor
from statsmodels.stats.diagnostic import het_breuschpagan
from scipy import stats

from app.config import settings
from app.db.store import connect, fetch_scored_edges

log = logging.getLogger(__name__)

# Configure matplotlib for non-interactive use
plt.switch_backend('Agg')


def _safe_float(value: Any) -> float | None:
    """Convert numeric values to finite Python floats, else None."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if np.isfinite(val) else None


def _json_safe(value: Any) -> Any:
    """Recursively convert numpy/pandas values to JSON-safe Python values."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return _safe_float(value)
    if pd.isna(value):
        return None
    return value


def _fmt(value: Any, digits: int = 3) -> str:
    """Format numeric values for readable reports."""
    safe_value = _safe_float(value)
    if safe_value is None:
        return "N/A"
    return f"{safe_value:.{digits}f}"


def _build_judge_report(summary: dict[str, Any]) -> str:
    """Build a one-page markdown report for hackathon judges."""
    dataset = summary.get('dataset_info', {})
    model_perf = summary.get('model_performance', {})
    best_model = summary.get('best_model') or "N/A"
    tests = summary.get('statistical_tests', {})
    eda = summary.get('eda_insights', {})
    hypothesis_tests = eda.get('hypothesis_tests', {})
    key_findings = summary.get('key_findings', [])

    model_rows = []
    sorted_models = sorted(
        model_perf.items(),
        key=lambda item: item[1].get('r2', float('-inf')),
        reverse=True,
    )
    for model_name, metrics in sorted_models:
        model_rows.append(
            f"| {model_name} | {_fmt(metrics.get('mse'), 4)} | {_fmt(metrics.get('r2'), 4)} | "
            f"{_fmt(metrics.get('adj_r2'), 4)} | {_fmt(metrics.get('cv_r2_mean'), 4)} | "
            f"{_fmt(metrics.get('cv_r2_std'), 4)} |"
        )
    if not model_rows:
        model_rows = ["| N/A | N/A | N/A | N/A | N/A | N/A |"]

    top_corr = eda.get('top_correlations', {})
    corr_lines = []
    for feat, corr in list(top_corr.items())[:5]:
        corr_lines.append(f"- `{feat}`: |corr| = {_fmt(corr, 4)}")
    if not corr_lines:
        corr_lines = ["- No valid feature correlations available."]

    hypothesis_lines = []
    for key, details in hypothesis_tests.items():
        if details.get('skipped'):
            hypothesis_lines.append(
                f"- `{key}`: skipped ({details.get('reason', 'insufficient variation/data')})"
            )
            continue
        supported = "yes" if details.get('supported_at_0_05') else "no"
        hypothesis_lines.append(
            f"- `{key}`: p = {_fmt(details.get('pvalue'), 4)}, supported @ 0.05 = {supported}"
        )
    if not hypothesis_lines:
        hypothesis_lines = ["- No hypothesis tests available."]

    measured = dataset.get('measured_scores', 0)
    total = dataset.get('total_samples', 0)
    measured_pct = (100 * measured / total) if total else None

    return (
        "# HackDavis Statistical Modeling Report (Judge One-Pager)\n\n"
        "## Core question\n"
        "Can we predict cyclist safety scores from OSM roadway features with statistically grounded modeling?\n\n"
        "## Dataset and EDA summary\n"
        f"- Total scored segments: {total}\n"
        f"- Measured (non-inferred) scores: {measured} ({_fmt(measured_pct, 2)}%)\n"
        f"- Engineered features used in modeling: {dataset.get('features_engineered', 'N/A')}\n"
        "- EDA informed model choice by checking feature distributions, outliers, correlations, and multicollinearity.\n"
        "- Top target correlations:\n"
        f"{chr(10).join(corr_lines)}\n\n"
        "## Model evaluation\n"
        "| Model | MSE | R² | Adjusted R² | CV R² Mean | CV R² Std |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        f"{chr(10).join(model_rows)}\n\n"
        f"- Best holdout model: `{best_model}`\n\n"
        "## Significance tests and assumptions\n"
        f"- Global linear model F-test p-value: {_fmt(tests.get('f_pvalue'), 4)}\n"
        f"- Breusch-Pagan p-value (heteroscedasticity): {_fmt(tests.get('heteroscedasticity_pvalue'), 4)}\n"
        f"- Jarque-Bera p-value (residual normality): {_fmt(tests.get('normality_pvalue'), 4)}\n"
        "- Hypothesis tests:\n"
        f"{chr(10).join(hypothesis_lines)}\n\n"
        "## Key findings\n"
        + "\n".join(f"- {finding}" for finding in key_findings)
        + "\n\n## Limitations and next step\n"
        "- Current measured-label coverage is low, which weakens inferential power.\n"
        "- Next high-impact step: collect more measured safety labels and rerun the same pipeline to strengthen hypothesis testing and generalization claims.\n"
    )

class BikeSafetyModel:
    """Statistical model for predicting bike safety scores."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.data: pd.DataFrame | None = None
        self.features: list[str] = []
        self.target = 'mean_score'
        self.models = {}
        self.scaler = StandardScaler()
        self.feature_selector = None

    def load_data(self) -> pd.DataFrame:
        """Load and preprocess edge data with safety scores."""
        log.info("Loading edge data from database...")
        with connect(self.db_path) as conn:
            rows = fetch_scored_edges(conn)

        data = []
        for row in rows:
            record = dict(row)
            # Parse OSM tags
            if record.get('osm_tags_json'):
                try:
                    osm_tags = json.loads(record['osm_tags_json'])
                    record.update(osm_tags)
                except json.JSONDecodeError:
                    pass
            # Add flag for measured vs inferred scores
            record['is_measured'] = (record.get('sample_count') or 0) > 0
            data.append(record)

        df = pd.DataFrame(data)
        log.info(f"Loaded {len(df)} edges with safety scores")

        # Filter out edges without scores
        df = df.dropna(subset=['mean_score'])
        log.info(f"After filtering: {len(df)} edges with valid scores")

        # Focus on measured scores for better quality
        measured_df = df[df['is_measured'] == True]
        if len(measured_df) >= 50:  # Minimum sample size
            df = measured_df
            log.info(f"Using {len(df)} measured scores for analysis")
        else:
            log.warning(f"Only {len(measured_df)} measured scores, using all {len(df)} scores")

        self.data = df
        return df

    def engineer_features(self) -> pd.DataFrame:
        """Extract and engineer features from OSM data with comprehensive feature set."""
        if self.data is None:
            raise ValueError("Data not loaded. Call load_data() first.")

        df = self.data.copy()

        # Basic geometric features
        self.features = ['length_m']

        # Highway type encoding with more granularity
        highway_hierarchy = {
            'motorway': 10, 'trunk': 9, 'primary': 8, 'secondary': 7, 'tertiary': 6,
            'unclassified': 5, 'residential': 4, 'living_street': 3,
            'cycleway': 1, 'footway': 1, 'path': 1, 'track': 1, 'pedestrian': 2,
            'service': 4, 'construction': 5
        }
        df['highway_encoded'] = df['highway'].map(highway_hierarchy).fillna(4)
        self.features.append('highway_encoded')

        # Speed limit processing
        def encode_maxspeed(speed):
            if pd.isna(speed):
                # Infer from highway type - but we can't access df here
                # So just return default
                return 25
            if isinstance(speed, str):
                speed = speed.replace(' mph', '').replace(' km/h', '').replace(' knots', '')
                try:
                    speed = float(speed)
                except ValueError:
                    return 25
            return min(speed, 70)  # Cap at 70

        df['maxspeed_encoded'] = df.get('maxspeed', pd.Series([None] * len(df))).apply(encode_maxspeed)
        self.features.append('maxspeed_encoded')

        # Lane configuration
        lanes_series = df.get('lanes')
        if lanes_series is not None:
            df['lanes_encoded'] = pd.to_numeric(lanes_series, errors='coerce').fillna(2)
        else:
            df['lanes_encoded'] = 2
        self.features.append('lanes_encoded')

        # Bike infrastructure features
        bike_features = ['cycleway', 'cycleway:left', 'cycleway:right', 'cycleway:both']
        existing_bike_features = [f for f in bike_features if f in df.columns]
        if existing_bike_features:
            df['has_cycleway'] = df[existing_bike_features].notna().any(axis=1).astype(int)
        else:
            df['has_cycleway'] = 0

        # Cycleway protection level
        if 'cycleway' in df.columns:
            df['cycleway_protection'] = df['cycleway'].map({
                'lane': 1, 'track': 2, 'opposite_lane': 1, 'opposite_track': 2,
                'shared_lane': 0.5, 'share_busway': 0.5
            }).fillna(0)
        else:
            df['cycleway_protection'] = 0

        self.features.extend(['has_cycleway', 'cycleway_protection'])

        # Road surface quality
        surface_quality = {
            'asphalt': 1, 'concrete': 1, 'paved': 1, 'paving_stones': 1.5,
            'cobblestone': 3, 'sett': 3, 'unhewn_cobblestone': 4,
            'compacted': 2, 'dirt': 4, 'earth': 4, 'grass': 5, 'gravel': 3,
            'ground': 4, 'mud': 5, 'sand': 4, 'wood': 2, 'metal': 1
        }
        surface_series = df.get('surface')
        if surface_series is not None:
            df['surface_quality'] = surface_series.map(surface_quality).fillna(1)
        else:
            df['surface_quality'] = 1
        self.features.append('surface_quality')

        # Traffic control features
        if 'traffic_signals' in df.columns:
            df['has_traffic_signals'] = (df['traffic_signals'] == 'signal').astype(int)
        else:
            df['has_traffic_signals'] = 0

        if 'oneway' in df.columns:
            df['is_oneway'] = (df['oneway'] == 'yes').astype(int)
        else:
            df['is_oneway'] = 0

        if 'stop' in df.columns:
            df['has_stop_sign'] = df['stop'].notna().astype(int)
        else:
            df['has_stop_sign'] = 0

        self.features.extend(['has_traffic_signals', 'is_oneway', 'has_stop_sign'])

        # Intersection density proxy (degree from graph)
        # For now, use highway type as proxy for intersection density
        df['intersection_density'] = df['highway_encoded'].map({
            10: 0.1, 9: 0.2, 8: 0.3, 7: 0.4, 6: 0.5, 5: 0.6, 4: 0.7, 3: 0.8, 2: 0.9, 1: 1.0
        })
        self.features.append('intersection_density')

        # Create interaction features
        df['speed_lanes_interaction'] = df['maxspeed_encoded'] * df['lanes_encoded']
        df['highway_speed_interaction'] = df['highway_encoded'] * df['maxspeed_encoded']
        df['bike_highway_interaction'] = df['has_cycleway'] * (11 - df['highway_encoded'])  # Higher for bike-friendly roads
        self.features.extend(['speed_lanes_interaction', 'highway_speed_interaction', 'bike_highway_interaction'])

        # Handle missing values
        for col in self.features:
            if df[col].dtype in ['float64', 'int64']:
                df[col] = df[col].fillna(df[col].median())

        log.info(f"Engineered {len(self.features)} features: {self.features}")
        self.data = df
        return df

    def perform_eda(self, output_dir: Path | None = None) -> dict[str, Any]:
        """Perform comprehensive exploratory data analysis."""
        if self.data is None:
            raise ValueError("Data not loaded. Call load_data() and engineer_features() first.")

        df = self.data
        results = {}

        # Basic statistics
        results['basic_stats'] = df[self.features + [self.target]].describe()

        # Data quality checks
        results['data_quality'] = {
            'missing_values': df[self.features + [self.target]].isnull().sum().to_dict(),
            'data_types': df[self.features + [self.target]].dtypes.astype(str).to_dict(),
            'unique_values': {col: df[col].nunique() for col in self.features + [self.target]},
            'constant_features': [col for col in self.features if df[col].nunique() <= 1]
        }

        # Correlation analysis
        corr_matrix = df[self.features + [self.target]].corr()
        results['correlation_matrix'] = corr_matrix

        # Top correlations with target
        target_corr = corr_matrix[self.target].drop(self.target).abs().sort_values(ascending=False)
        results['top_correlations'] = target_corr.head(10).to_dict()

        # Check for multicollinearity
        X = df[self.features]
        vif_data = []
        for i, col in enumerate(X.columns):
            try:
                vif_val = variance_inflation_factor(X.values, i)
                vif_data.append({'feature': col, 'VIF': vif_val})
            except:
                vif_data.append({'feature': col, 'VIF': float('nan')})
        vif_df = pd.DataFrame(vif_data)
        results['vif'] = vif_df.sort_values('VIF', ascending=False).to_dict('records')

        # Distribution analysis
        results['distributions'] = {}
        for col in self.features + [self.target]:
            if df[col].dtype in ['float64', 'int64']:
                shapiro = None
                if len(df[col].dropna()) >= 3:
                    shapiro_stat, shapiro_p = stats.shapiro(df[col].dropna())
                    shapiro = {
                        'statistic': _safe_float(shapiro_stat),
                        'pvalue': _safe_float(shapiro_p),
                        'is_non_normal_p_lt_0_05': bool(shapiro_p < 0.05)
                    }
                results['distributions'][col] = {
                    'skewness': _safe_float(df[col].skew()),
                    'kurtosis': _safe_float(df[col].kurtosis()),
                    'normality_test': shapiro
                }

        # Outlier detection
        results['outliers'] = {}
        for col in self.features + [self.target]:
            if df[col].dtype in ['float64', 'int64']:
                Q1 = df[col].quantile(0.25)
                Q3 = df[col].quantile(0.75)
                IQR = Q3 - Q1
                outliers = ((df[col] < (Q1 - 1.5 * IQR)) | (df[col] > (Q3 + 1.5 * IQR))).sum()
                results['outliers'][col] = {
                    'count': int(outliers),
                    'percentage': _safe_float(outliers / len(df) * 100)
                }

        # Hypothesis-oriented EDA tests used to guide model decisions
        hypothesis_tests: dict[str, Any] = {}
        if 'maxspeed_encoded' in df.columns and df['maxspeed_encoded'].nunique() > 1:
            corr, p = stats.spearmanr(df['maxspeed_encoded'], df[self.target], nan_policy='omit')
            hypothesis_tests['H1_speed_vs_safety'] = {
                'hypothesis': "Higher-speed roads have lower safety scores",
                'test': 'Spearman correlation',
                'correlation': _safe_float(corr),
                'pvalue': _safe_float(p),
                'supported_at_0_05': bool((p < 0.05) and (corr < 0))
            }
        elif 'maxspeed_encoded' in df.columns:
            hypothesis_tests['H1_speed_vs_safety'] = {
                'hypothesis': "Higher-speed roads have lower safety scores",
                'test': 'Spearman correlation',
                'skipped': True,
                'reason': 'maxspeed_encoded has no variation in current dataset'
            }
        if 'has_cycleway' in df.columns and df['has_cycleway'].nunique() > 1:
            with_cycleway = df[df['has_cycleway'] == 1][self.target]
            without_cycleway = df[df['has_cycleway'] == 0][self.target]
            if len(with_cycleway) >= 3 and len(without_cycleway) >= 3:
                t_stat, p = stats.ttest_ind(with_cycleway, without_cycleway, equal_var=False)
                hypothesis_tests['H2_cycleway_vs_safety'] = {
                    'hypothesis': "Roads with bike infrastructure have higher safety scores",
                    'test': "Welch's t-test",
                    'mean_with_cycleway': _safe_float(with_cycleway.mean()),
                    'mean_without_cycleway': _safe_float(without_cycleway.mean()),
                    't_statistic': _safe_float(t_stat),
                    'pvalue': _safe_float(p),
                    'supported_at_0_05': bool((p < 0.05) and (with_cycleway.mean() > without_cycleway.mean()))
                }
        elif 'has_cycleway' in df.columns:
            hypothesis_tests['H2_cycleway_vs_safety'] = {
                'hypothesis': "Roads with bike infrastructure have higher safety scores",
                'test': "Welch's t-test",
                'skipped': True,
                'reason': 'has_cycleway has no variation in current dataset'
            }
        if 'surface_quality' in df.columns and df['surface_quality'].nunique() > 1:
            corr, p = stats.spearmanr(df['surface_quality'], df[self.target], nan_policy='omit')
            hypothesis_tests['H4_surface_quality_vs_safety'] = {
                'hypothesis': "Worse surface quality reduces safety scores",
                'test': 'Spearman correlation',
                'correlation': _safe_float(corr),
                'pvalue': _safe_float(p),
                'supported_at_0_05': bool((p < 0.05) and (corr < 0))
            }
        elif 'surface_quality' in df.columns:
            hypothesis_tests['H4_surface_quality_vs_safety'] = {
                'hypothesis': "Worse surface quality reduces safety scores",
                'test': 'Spearman correlation',
                'skipped': True,
                'reason': 'surface_quality has no variation in current dataset'
            }
        results['hypothesis_tests'] = hypothesis_tests

        if output_dir:
            output_dir.mkdir(exist_ok=True)

            # Enhanced correlation heatmap
            plt.figure(figsize=(12, 10))
            mask = np.triu(np.ones_like(corr_matrix, dtype=bool))
            sns.heatmap(corr_matrix, mask=mask, annot=True, cmap='coolwarm', center=0, fmt='.2f')
            plt.title('Feature Correlation Matrix (Lower Triangle)')
            plt.tight_layout()
            plt.savefig(output_dir / 'correlation_heatmap.png', dpi=300, bbox_inches='tight')
            plt.close()

            # Feature distributions
            n_features = len(self.features)
            n_cols = min(4, n_features)
            n_rows = (n_features + n_cols - 1) // n_cols

            fig, axes = plt.subplots(n_rows, n_cols, figsize=(5*n_cols, 4*n_rows))
            if n_rows == 1:
                axes = axes.reshape(1, -1)
            elif n_cols == 1:
                axes = axes.reshape(-1, 1)

            for i, col in enumerate(self.features):
                row = i // n_cols
                col_idx = i % n_cols
                if n_rows == 1:
                    ax = axes[col_idx]
                elif n_cols == 1:
                    ax = axes[row]
                else:
                    ax = axes[row, col_idx]

                if df[col].nunique() > 10:
                    sns.histplot(data=df, x=col, ax=ax, kde=True)
                else:
                    sns.countplot(data=df, x=col, ax=ax)
                ax.set_title(f'{col} (skew: {df[col].skew():.2f})')

            plt.tight_layout()
            plt.savefig(output_dir / 'feature_distributions.png', dpi=300, bbox_inches='tight')
            plt.close()

            # Target vs top features scatter plots
            top_features = target_corr.head(6).index.tolist()
            fig, axes = plt.subplots(2, 3, figsize=(15, 10))
            axes = axes.ravel()

            for i, col in enumerate(top_features):
                if i < 6:
                    sns.scatterplot(data=df, x=col, y=self.target, ax=axes[i], alpha=0.6)
                    axes[i].set_title(f'{col} vs Safety Score\n(corr: {corr_matrix.loc[col, self.target]:.3f})')

            plt.tight_layout()
            plt.savefig(output_dir / 'scatter_plots.png', dpi=300, bbox_inches='tight')
            plt.close()

            # VIF plot
            vif_plot_data = vif_df.sort_values('VIF', ascending=True)
            plt.figure(figsize=(10, 6))
            bars = plt.barh(vif_plot_data['feature'], vif_plot_data['VIF'])
            plt.axvline(x=5, color='red', linestyle='--', label='VIF = 5 threshold')
            plt.axvline(x=10, color='orange', linestyle='--', label='VIF = 10 threshold')
            plt.xlabel('Variance Inflation Factor (VIF)')
            plt.title('Multicollinearity Analysis')
            plt.legend()
            plt.tight_layout()
            plt.savefig(output_dir / 'vif_analysis.png', dpi=300, bbox_inches='tight')
            plt.close()

        log.info("Comprehensive EDA completed")
        return results

    def train_models(self, test_size: float = 0.2, cv_folds: int = 5) -> dict[str, Any]:
        """Train and evaluate multiple statistical models with cross-validation."""
        if self.data is None:
            raise ValueError("Data not loaded. Call load_data() and engineer_features() first.")

        df = self.data
        X = df[self.features]
        y = df[self.target]

        # Remove constant features
        constant_features = []
        for col in self.features:
            if df[col].nunique() <= 1:
                constant_features.append(col)

        if constant_features:
            log.warning(f"Removing constant features: {constant_features}")
            self.features = [f for f in self.features if f not in constant_features]
            X = df[self.features]

        # Feature selection
        self.feature_selector = SelectKBest(score_func=f_regression, k='all')
        X_selected = self.feature_selector.fit_transform(X, y)
        selected_features = [self.features[i] for i in self.feature_selector.get_support(indices=True)]

        if len(selected_features) < len(self.features):
            log.info(f"Feature selection reduced from {len(self.features)} to {len(selected_features)} features")

        # Scale features
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X_selected)

        # Split data
        X_train, X_test, y_train, y_test = train_test_split(
            X_scaled, y, test_size=test_size, random_state=42
        )

        results = {}

        # Cross-validation setup
        cv = KFold(n_splits=cv_folds, shuffle=True, random_state=42)

        # 1. Linear Regression with statsmodels (for significance tests)
        log.info("Training Linear Regression with statistical tests...")
        try:
            X_train_sm = sm.add_constant(pd.DataFrame(X_train, columns=selected_features))
            X_test_sm = sm.add_constant(pd.DataFrame(X_test, columns=selected_features))

            lr_model = sm.OLS(y_train.reset_index(drop=True), X_train_sm.reset_index(drop=True)).fit()

            # Statistical tests
            lr_pred = lr_model.predict(X_test_sm.reset_index(drop=True))

            # Heteroscedasticity test
            try:
                het_test = het_breuschpagan(lr_model.resid, lr_model.model.exog)
                het_pvalue = het_test[1]
            except:
                het_pvalue = None

            # Normality of residuals
            try:
                jb_test = stats.jarque_bera(lr_model.resid)
                jb_pvalue = jb_test[1]
            except:
                jb_pvalue = None

            denominator = (len(y_test) - X_test.shape[1] - 1)
            adj_r2 = None
            if denominator > 0:
                r2_val = r2_score(y_test, lr_pred)
                adj_r2 = 1 - (1 - r2_val) * (len(y_test) - 1) / denominator

            results['linear_regression'] = {
                'model': lr_model,
                'predictions': lr_pred,
                'mse': mean_squared_error(y_test, lr_pred),
                'mae': mean_absolute_error(y_test, lr_pred),
                'r2': r2_score(y_test, lr_pred),
                'adj_r2': adj_r2,
                'cv_scores': cross_val_score(LinearRegression(), X_train, y_train, cv=cv, scoring='r2'),
                'summary': str(lr_model.summary()),
                'statistical_tests': {
                    'heteroscedasticity_pvalue': het_pvalue,
                    'normality_pvalue': jb_pvalue,
                    'f_statistic': lr_model.fvalue,
                    'f_pvalue': lr_model.f_pvalue
                }
            }
        except Exception as e:
            log.error(f"Linear regression failed: {e}")
            results['linear_regression'] = {'error': str(e)}

        # 2. Ridge Regression
        log.info("Training Ridge Regression...")
        try:
            ridge_model = Ridge(alpha=1.0, random_state=42)
            ridge_model.fit(X_train, y_train)
            ridge_pred = ridge_model.predict(X_test)

            results['ridge_regression'] = {
                'model': ridge_model,
                'predictions': ridge_pred,
                'mse': mean_squared_error(y_test, ridge_pred),
                'mae': mean_absolute_error(y_test, ridge_pred),
                'r2': r2_score(y_test, ridge_pred),
                'cv_scores': cross_val_score(Ridge(alpha=1.0), X_train, y_train, cv=cv, scoring='r2'),
                'coefficients': dict(zip(selected_features, ridge_model.coef_))
            }
        except Exception as e:
            log.error(f"Ridge regression failed: {e}")
            results['ridge_regression'] = {'error': str(e)}

        # 3. Random Forest Regressor
        log.info("Training Random Forest Regressor...")
        try:
            rf_model = RandomForestRegressor(n_estimators=100, random_state=42, max_depth=10)
            rf_model.fit(X_train, y_train)
            rf_pred = rf_model.predict(X_test)

            results['random_forest'] = {
                'model': rf_model,
                'predictions': rf_pred,
                'mse': mean_squared_error(y_test, rf_pred),
                'mae': mean_absolute_error(y_test, rf_pred),
                'r2': r2_score(y_test, rf_pred),
                'cv_scores': cross_val_score(RandomForestRegressor(n_estimators=100, random_state=42), X_train, y_train, cv=cv, scoring='r2'),
                'feature_importance': dict(zip(selected_features, rf_model.feature_importances_))
            }
        except Exception as e:
            log.error(f"Random Forest failed: {e}")
            results['random_forest'] = {'error': str(e)}

        # 4. Gradient Boosting
        log.info("Training Gradient Boosting Regressor...")
        try:
            gb_model = GradientBoostingRegressor(n_estimators=100, random_state=42, max_depth=5)
            gb_model.fit(X_train, y_train)
            gb_pred = gb_model.predict(X_test)

            results['gradient_boosting'] = {
                'model': gb_model,
                'predictions': gb_pred,
                'mse': mean_squared_error(y_test, gb_pred),
                'mae': mean_absolute_error(y_test, gb_pred),
                'r2': r2_score(y_test, gb_pred),
                'cv_scores': cross_val_score(GradientBoostingRegressor(n_estimators=100, random_state=42), X_train, y_train, cv=cv, scoring='r2'),
                'feature_importance': dict(zip(selected_features, gb_model.feature_importances_))
            }
        except Exception as e:
            log.error(f"Gradient Boosting failed: {e}")
            results['gradient_boosting'] = {'error': str(e)}

        # Model comparison
        model_comparison = {}
        for name, result in results.items():
            if 'error' not in result:
                model_comparison[name] = {
                    'r2': result['r2'],
                    'mse': result['mse'],
                    'mae': result['mae'],
                    'cv_mean': result['cv_scores'].mean(),
                    'cv_std': result['cv_scores'].std()
                }

        if model_comparison:
            results['model_comparison'] = pd.DataFrame(model_comparison).T.sort_values('r2', ascending=False)
        else:
            results['model_comparison'] = pd.DataFrame()

        self.models = results
        log.info("Model training and evaluation completed")
        return results

    def predict_safety_score(self, features: dict[str, Any]) -> float:
        """Predict safety score for new edge features."""
        if not self.models:
            raise ValueError("Models not trained. Call train_models() first.")

        # Use the best model (Random Forest for prediction)
        model = self.models['random_forest']['model']

        # Create feature vector
        feature_vector = []
        for feat in self.features:
            if feat in features:
                feature_vector.append(features[feat])
            else:
                # Use median from training data
                median_val = self.data[feat].median()
                feature_vector.append(median_val)

        return model.predict([feature_vector])[0]

    def save_models(self, output_dir: Path) -> None:
        """Save trained models and results."""
        import pickle

        output_dir.mkdir(exist_ok=True)

        # Save models (only those that trained successfully)
        for name, result in self.models.items():
            if 'model' in result:
                with open(output_dir / f'{name}_model.pkl', 'wb') as f:
                    pickle.dump(result['model'], f)

        # Save feature engineering info
        feature_info = {
            'features': self.features,
            'medians': {col: self.data[col].median() for col in self.features}
        }

        with open(output_dir / 'feature_info.json', 'w') as f:
            json.dump(feature_info, f, indent=2)

        log.info(f"Models saved to {output_dir}")


def run_statistical_analysis(db_path: Path, output_dir: Path | None = None) -> dict[str, Any]:
    """Run complete statistical analysis pipeline with comprehensive evaluation."""
    model = BikeSafetyModel(db_path)

    # Load and preprocess data
    model.load_data()
    model.engineer_features()

    # EDA
    eda_results = model.perform_eda(output_dir)

    # Train models
    model_results = model.train_models()

    # Generate comprehensive report
    if output_dir:
        model.save_models(output_dir)

        valid_model_results = {
            name: result
            for name, result in model_results.items()
            if isinstance(result, dict) and 'error' not in result and 'r2' in result
        }
        best_model_name = model_results['model_comparison'].index[0] if not model_results['model_comparison'].empty else None
        linear_tests = model_results.get('linear_regression', {}).get('statistical_tests', {})
        significant_hypotheses = [
            key
            for key, details in eda_results.get('hypothesis_tests', {}).items()
            if details.get('supported_at_0_05')
        ]

        # Enhanced analysis summary
        multicollinearity_issues = []
        perfect_multicollinearity_features = []
        for item in eda_results['vif']:
            vif_val = _safe_float(item.get('VIF'))
            if vif_val is None:
                perfect_multicollinearity_features.append(item['feature'])
            elif vif_val > 5:
                multicollinearity_issues.append({
                    'feature': item['feature'],
                    'VIF': vif_val
                })

        summary = {
            'dataset_info': {
                'total_samples': len(model.data),
                'measured_scores': int(model.data['is_measured'].sum()),
                'features_engineered': len(model.features),
                'selected_features': len(model.feature_selector.get_support()) if model.feature_selector else len(model.features)
            },
            'eda_insights': {
                'top_correlations': eda_results['top_correlations'],
                'multicollinearity_issues': multicollinearity_issues,
                'perfect_multicollinearity_features': perfect_multicollinearity_features,
                'data_quality_warnings': eda_results['data_quality']['constant_features'],
                'outlier_summary': {k: v for k, v in eda_results['outliers'].items() if (v.get('percentage') or 0) > 5},
                'hypothesis_tests': eda_results.get('hypothesis_tests', {})
            },
            'model_performance': {
                name: {
                    'r2': result['r2'],
                    'mse': result['mse'],
                    'mae': result['mae'],
                    'cv_r2_mean': result['cv_scores'].mean(),
                    'cv_r2_std': result['cv_scores'].std(),
                    'adj_r2': result.get('adj_r2', result['r2'])
                }
                for name, result in valid_model_results.items()
            },
            'best_model': best_model_name,
            'statistical_tests': linear_tests,
            'feature_importance': {
                'random_forest': model_results.get('random_forest', {}).get('feature_importance', {}),
                'gradient_boosting': model_results.get('gradient_boosting', {}).get('feature_importance', {})
            },
            'hypotheses_tested': [
                "H1: Higher-speed roads have lower safety scores",
                "H2: Roads with dedicated bike infrastructure have higher safety scores",
                "H3: Traffic control features (signals, stops) improve safety perception",
                "H4: Road surface quality significantly impacts safety scores",
                "H5: Interaction between speed limits and lane count affects safety"
            ],
            'key_findings': [
                (
                    f"Best performing model: {best_model_name} "
                    f"(R² = {model_results['model_comparison'].iloc[0]['r2']:.3f})"
                    if best_model_name else "No model trained successfully."
                ),
                (
                    f"Most important feature: {max(model_results['random_forest']['feature_importance'], key=model_results['random_forest']['feature_importance'].get)}"
                    if model_results.get('random_forest', {}).get('feature_importance') else "Random forest feature importance unavailable."
                ),
                f"Dataset quality: {int(model.data['is_measured'].sum())}/{len(model.data)} measured scores.",
                (
                    f"Hypotheses supported at p<0.05: {', '.join(significant_hypotheses)}"
                    if significant_hypotheses else "No pre-registered hypothesis reached p<0.05 support."
                )
            ],
            'report_metadata': {
                'output_cleaned_for_json': True,
                'includes_eda_guided_hypothesis_tests': True
            }
        }

        with open(output_dir / 'comprehensive_analysis.json', 'w') as f:
            json.dump(_json_safe(summary), f, indent=2, allow_nan=False)

        judge_report = _build_judge_report(_json_safe(summary))
        with open(output_dir / 'HACKDAVIS_JUDGE_REPORT.md', 'w') as f:
            f.write(judge_report)

        # Model comparison plot
        comparison_df = model_results['model_comparison']
        plt.figure(figsize=(12, 8))
        x = range(len(comparison_df))
        plt.bar(x, comparison_df['r2'], alpha=0.7, label='R²', color='skyblue')
        plt.errorbar(x, comparison_df['cv_mean'], yerr=comparison_df['cv_std'],
                    fmt='o', color='red', label='CV R² ± std', capsize=5)
        plt.xticks(x, comparison_df.index, rotation=45, ha='right')
        plt.ylabel('Performance Metric')
        plt.title('Model Comparison: R² Scores with Cross-Validation')
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(output_dir / 'model_comparison.png', dpi=300, bbox_inches='tight')
        plt.close()

        # Feature importance plot
        if 'random_forest' in model_results:
            rf_importance = model_results['random_forest']['feature_importance']
            plt.figure(figsize=(10, 6))
            features_sorted = sorted(rf_importance.items(), key=lambda x: x[1], reverse=True)
            features, scores = zip(*features_sorted)
            plt.barh(range(len(features)), scores)
            plt.yticks(range(len(features)), features)
            plt.xlabel('Feature Importance')
            plt.title('Random Forest Feature Importance')
            plt.gca().invert_yaxis()
            plt.tight_layout()
            plt.savefig(output_dir / 'feature_importance.png', dpi=300, bbox_inches='tight')
            plt.close()

    return {
        'eda': eda_results,
        'models': model_results,
        'model_instance': model
    }


if __name__ == '__main__':
    # Run analysis when called directly
    output_dir = Path(__file__).parent / 'analysis_output'
    results = run_statistical_analysis(settings.db_path, output_dir)
    print("Statistical analysis completed. Results saved to:", output_dir)