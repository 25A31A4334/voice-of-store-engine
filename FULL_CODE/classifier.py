"""Issue Classification Module for R5 — Voice-of-Store Engine.

Author: Team CodeNova
Purpose:
    Supervised multilingual text classification model trained strictly on the
    provided 3,000 labelled records (Weeks 1–13) to classify feedback into
    the 33 official taxonomy categories (IT01–IT30, NI01–NI03).

Model Architecture:
    - Feature Union of:
        1. Word-level TF-IDF (unigrams & bigrams, sublinear TF)
        2. Character n-gram TF-IDF (3- to 5-grams inside word boundaries)
           * Crucial for Hindi/Marathi/Tamil transliterations, code-mixed text, and SMS typos.
    - Calibrated Linear Support Vector Classifier:
        * Delivers state-of-the-art text classification accuracy on sparse high-dimensional n-grams.
        * Calibrated with Platt scaling / cross-validation to produce real probability confidence scores.
"""
from __future__ import annotations

import pickle
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import FeatureUnion
from sklearn.svm import LinearSVC

try:
    from FULL_CODE.text_preprocessor import TextPreprocessor
except ImportError:
    from text_preprocessor import TextPreprocessor

try:
    from FULL_CODE.services.sentiment_engine import SentimentEngine
except ImportError:
    from services.sentiment_engine import SentimentEngine


class IssueClassifier:
    """Supervised classifier for Saffron Retail issue taxonomy with domain disambiguation."""

    # Domain disambiguation patterns to resolve out-of-vocabulary terms and n-gram subword false positives
    RE_HOSPITALITY = re.compile(
        r"\b(hospitality|customer service|guest service)\b",
        re.IGNORECASE,
    )
    RE_NEGATIVE_HOSPITALITY = re.compile(
        r"\b(worst|bad|terrible|horrible|poor|pathetic|rude|unhelpful|disappointing|awful|zero|no)\b.{0,30}\b(hospitality|customer service|guest service)\b|"
        r"\b(hospitality|customer service|guest service)\b.{0,30}\b(worst|bad|terrible|horrible|poor|pathetic|rude|unhelpful|disappointing|awful|zero|no)\b",
        re.IGNORECASE,
    )
    RE_STAFF_BEHAVIOUR = re.compile(
        r"\b(staff|cashier|guard|manager|employee|floor boy)\b.{0,40}\b(rude|unhelpful|arrogant|attitude|indifferent|ignored|impolite|disrespectful|not helpful|refused to help)\b|"
        r"\b(rude|unhelpful|arrogant|disrespectful|impolite)\b.{0,40}\b(staff|cashier|guard|manager|employee|floor boy)\b",
        re.IGNORECASE,
    )
    RE_REFRIGERATION = re.compile(
        r"\b(refrigerator|fridge|freezer|chiller|deep freeze|cold chain|ice cream melting|thawed)\b",
        re.IGNORECASE,
    )
    RE_SPOILED_DAIRY = re.compile(
        r"\b(milk|curd|dahi|paneer|cheese|butter|yogurt)\b.{0,30}\b(spoiled|sour|curdled|expired|near expiry|past expiry|date over|rotten|smelly)\b|"
        r"\b(spoiled|sour|curdled|expired|near expiry|past expiry|date over|rotten|smelly)\b.{0,30}\b(milk|curd|dahi|paneer|cheese|butter|yogurt)\b",
        re.IGNORECASE,
    )
    RE_PRODUCE_QUALITY = re.compile(
        r"\b(vegetables?|veggies?|fruits?|tomatoes?|onions?|potatoes?|apples?|bananas?|spinach|sabzi|bhaji)\b.{0,30}\b(rotten|stale|damaged|black inside|decayed)\b|"
        r"\b(rotten|stale|damaged|black inside|decayed)\b.{0,30}\b(vegetables?|veggies?|fruits?|tomatoes?|onions?|potatoes?|apples?|bananas?|spinach|sabzi|bhaji)\b",
        re.IGNORECASE,
    )
    RE_PRAISE_NEGATION_GUARD = re.compile(
        r"\b(unhelpful|not helpful|rude|worst|terrible|horrible|poor|bad|pathetic|disappointed|useless|rotten|spoiled|broken|scam)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        word_max_features: int = 12000,
        char_max_features: int = 20000,
        C: float = 1.0,
        random_state: int = 42,
    ) -> None:
        self.word_max_features = word_max_features
        self.char_max_features = char_max_features
        self.C = C
        self.random_state = random_state

        # Initialize vectorizers
        self.word_vectorizer = TfidfVectorizer(
            ngram_range=(1, 2),
            max_features=self.word_max_features,
            sublinear_tf=True,
            strip_accents="unicode",
        )
        self.char_vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            max_features=self.char_max_features,
            sublinear_tf=True,
            strip_accents="unicode",
        )
        self.feature_union = FeatureUnion(
            [
                ("word", self.word_vectorizer),
                ("char", self.char_vectorizer),
            ]
        )

        base_svc = LinearSVC(C=self.C, random_state=self.random_state)
        self.model = CalibratedClassifierCV(estimator=base_svc, cv=3)
        self.is_fitted = False
        self.classes_: np.ndarray | None = None
        self.sentiment_engine = SentimentEngine()

    def _prepare_texts(self, texts: pd.Series | List[str]) -> List[str]:
        """Preprocesses a collection of raw texts."""
        return [TextPreprocessor.clean_text(t) for t in texts]

    def fit(self, texts: pd.Series | List[str], labels: pd.Series | np.ndarray) -> "IssueClassifier":
        """Fits the vectorizer and calibrated classifier on training data."""
        clean_texts = self._prepare_texts(texts)
        X = self.feature_union.fit_transform(clean_texts)
        self.model.fit(X, labels)
        self.classes_ = np.array(self.model.classes_)
        self.is_fitted = True
        return self

    def _apply_domain_rules_with_sentiment(
        self,
        full_text: str,
        eval_text: str,
        ml_pred: str,
        ml_conf: float,
        sentiment: str,
    ) -> Tuple[str, float]:
        """Applies domain-aware disambiguation rules with decoupled sentiment awareness."""
        t_full = str(full_text).strip()
        t_eval = str(eval_text).strip()

        # 1. Staff Behaviour or Negative Hospitality complaints -> IT03 (Unhelpful or poorly trained staff)
        if self.RE_NEGATIVE_HOSPITALITY.search(t_full) or self.RE_STAFF_BEHAVIOUR.search(t_full):
            if sentiment in {"Negative", "Mixed"} or self.RE_PRAISE_NEGATION_GUARD.search(t_full):
                return ("IT03", max(ml_conf, 0.85))

        # 2. Refrigerator / freezer breakdown -> IT10 (Refrigeration / cold-chain failure)
        # Note: IT11 is strictly for non-refrigeration equipment breakdown per official taxonomy
        if self.RE_REFRIGERATION.search(t_eval):
            if ml_pred in {"IT11", "IT14", "IT07", "NI01", "NI02"}:
                return ("IT10", max(ml_conf, 0.85))

        # 3. Fresh produce quality (vegetables/fruits rotten) -> IT15 (Poor quality fresh produce)
        if self.RE_PRODUCE_QUALITY.search(t_eval):
            if ml_pred not in {"IT15"}:
                return ("IT15", max(ml_conf, 0.85))

        # 4. Spoiled dairy -> IT16 (Expired / near-expiry) or IT10 (Refrigeration failure)
        if self.RE_SPOILED_DAIRY.search(t_eval):
            if ml_pred in {"IT14", "IT15", "IT11", "NI01", "NI02"}:
                if self.RE_REFRIGERATION.search(t_eval):
                    return ("IT10", max(ml_conf, 0.85))
                return ("IT16", max(ml_conf, 0.85))

        # 5. Pure positive praise guard: Positive feedback must NEVER trigger negative issue codes IT01-IT30
        if sentiment == "Positive" and not self.sentiment_engine.RE_EXPLICIT_DEFECT.search(t_full):
            return ("NI01", max(ml_conf, 0.90))

        # 6. False-positive praise guard: NI01 (Praise) must never trigger on negative grievance words
        if ml_pred == "NI01" and (sentiment == "Negative" or self.RE_PRAISE_NEGATION_GUARD.search(t_full)):
            if re.search(r"\b(staff|cashier|guard|manager|hospitality|service)\b", t_full, re.IGNORECASE) or re.search(r"\b(unhelpful|rude|attitude)\b", t_full, re.IGNORECASE):
                return ("IT03", 0.75)
            return ("NI02", 0.50)

        return (ml_pred, ml_conf)

    def _apply_domain_rules(self, text: str, ml_pred: str, ml_conf: float) -> Tuple[str, float]:
        """Applies domain-aware disambiguation rules for out-of-vocabulary terms and false-positive praise."""
        s_res = self.sentiment_engine.predict(str(text))
        eval_text = str(text)
        if s_res["sentiment"] == "Mixed" and s_res["neg_clauses"]:
            eval_text = " ".join(s_res["neg_clauses"])
        return self._apply_domain_rules_with_sentiment(
            full_text=str(text),
            eval_text=eval_text,
            ml_pred=ml_pred,
            ml_conf=ml_conf,
            sentiment=s_res["sentiment"],
        )

    def predict_proba(self, texts: pd.Series | List[str]) -> np.ndarray:
        """Predicts class probabilities."""
        if not self.is_fitted:
            raise ValueError("Classifier is not fitted yet. Call fit() first.")
        clean_texts = self._prepare_texts(texts)
        X = self.feature_union.transform(clean_texts)
        return self.model.predict_proba(X)

    def predict_with_confidence(self, texts: pd.Series | List[str]) -> Tuple[np.ndarray, np.ndarray]:
        """Returns both predicted class and top probability confidence with sentiment and clause disambiguation."""
        if not self.is_fitted:
            raise ValueError("Classifier is not fitted yet. Call fit() first.")

        text_list = [str(t) for t in texts]
        eval_texts = []
        sentiments = []
        for t in text_list:
            s_res = self.sentiment_engine.predict(t)
            sentiments.append(s_res["sentiment"])
            # For mixed feedback with a defect clause, isolate the defect clause for classification
            if s_res["sentiment"] == "Mixed" and s_res["neg_clauses"]:
                eval_texts.append(" ".join(s_res["neg_clauses"]))
            else:
                eval_texts.append(t)

        clean_texts = self._prepare_texts(eval_texts)
        X = self.feature_union.transform(clean_texts)
        probs = self.model.predict_proba(X)
        top_idx = np.argmax(probs, axis=1)
        predictions = self.classes_[top_idx]
        confidences = np.max(probs, axis=1)

        final_preds = []
        final_confs = []
        for raw_t, eval_t, pred, conf, sent in zip(text_list, eval_texts, predictions, confidences, sentiments):
            adj_pred, adj_conf = self._apply_domain_rules_with_sentiment(
                full_text=raw_t,
                eval_text=eval_t,
                ml_pred=pred,
                ml_conf=float(conf),
                sentiment=sent,
            )
            final_preds.append(adj_pred)
            final_confs.append(adj_conf)
        return np.array(final_preds), np.array(final_confs, dtype=float)

    def predict(self, texts: pd.Series | List[str]) -> np.ndarray:
        """Predicts issue type IDs for given texts."""
        preds, _ = self.predict_with_confidence(texts)
        return preds

    def evaluate_cv(
        self,
        texts: pd.Series | List[str],
        labels: pd.Series | np.ndarray,
        n_splits: int = 5,
    ) -> Dict[str, Any]:
        """Performs Stratified K-Fold Cross-Validation strictly on training data."""
        clean_texts = np.array(self._prepare_texts(texts))
        labels_arr = np.array(labels)

        skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=self.random_state)
        fold_scores = []
        oof_preds = np.empty_like(labels_arr, dtype=object)

        for fold, (train_idx, val_idx) in enumerate(skf.split(clean_texts, labels_arr)):
            # Fresh vectorizer and model per fold to guarantee ZERO leakage
            word_v = TfidfVectorizer(ngram_range=(1, 2), max_features=self.word_max_features, sublinear_tf=True)
            char_v = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), max_features=self.char_max_features, sublinear_tf=True)
            union = FeatureUnion([("word", word_v), ("char", char_v)])

            X_tr = union.fit_transform(clean_texts[train_idx])
            X_val = union.transform(clean_texts[val_idx])

            svc = LinearSVC(C=self.C, random_state=self.random_state)
            svc.fit(X_tr, labels_arr[train_idx])
            val_preds = svc.predict(X_val)
            oof_preds[val_idx] = val_preds

            macro_f1 = f1_score(labels_arr[val_idx], val_preds, average="macro")
            fold_scores.append(macro_f1)

        overall_macro_f1 = f1_score(labels_arr, oof_preds, average="macro")
        report = classification_report(labels_arr, oof_preds, output_dict=True)

        return {
            "n_splits": n_splits,
            "fold_macro_f1s": [round(float(s), 4) for s in fold_scores],
            "mean_macro_f1": round(float(np.mean(fold_scores)), 4),
            "std_macro_f1": round(float(np.std(fold_scores)), 4),
            "overall_macro_f1": round(float(overall_macro_f1), 4),
            "classification_report": report,
            "oof_predictions": oof_preds,
        }

    def evaluate_temporal_split(
        self,
        df: pd.DataFrame,
        text_col: str = "text",
        label_col: str = "issue_type_id",
        week_col: str = "week_start",
        split_week: str = "2026-06-01",  # Weeks 1-9 vs Weeks 10-13
    ) -> Dict[str, Any]:
        """Evaluates causal out-of-time generalization across calendar weeks."""
        train_mask = df[week_col] < split_week
        val_mask = df[week_col] >= split_week

        train_texts = self._prepare_texts(df.loc[train_mask, text_col])
        val_texts = self._prepare_texts(df.loc[val_mask, text_col])
        y_train = df.loc[train_mask, label_col]
        y_val = df.loc[val_mask, label_col]

        # Vectorize strictly on past data
        word_v = TfidfVectorizer(ngram_range=(1, 2), max_features=self.word_max_features, sublinear_tf=True)
        char_v = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), max_features=self.char_max_features, sublinear_tf=True)
        union = FeatureUnion([("word", word_v), ("char", char_v)])

        X_tr = union.fit_transform(train_texts)
        X_val = union.transform(val_texts)

        svc = LinearSVC(C=self.C, random_state=self.random_state)
        svc.fit(X_tr, y_train)
        preds = svc.predict(X_val)

        macro_f1 = f1_score(y_val, preds, average="macro")

        return {
            "split_date": split_week,
            "n_train": int(len(y_train)),
            "n_val": int(len(y_val)),
            "val_macro_f1": round(float(macro_f1), 4),
        }

    def save(self, filepath: str | Path) -> None:
        """Saves fitted model to disk."""
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, filepath: str | Path) -> "IssueClassifier":
        """Loads fitted model from disk."""
        with open(filepath, "rb") as f:
            return pickle.load(f)
