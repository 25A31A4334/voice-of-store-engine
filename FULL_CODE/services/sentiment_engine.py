"""Sentiment Engine for R5 — Voice-of-Store Engine.

Author: Team CodeNova
Purpose:
    Decoupled sentiment classification that separates customer sentiment (Positive,
    Negative, Neutral, Mixed) from operational issue taxonomy (IT01–IT30, NI01–NI03).

Features:
    - Augmented VADER sentiment lexicon tailored for retail, grocery, and store operations.
    - Contrastive clause decomposition (detects 'but', 'however', 'although', etc.).
    - Distinguishes pure praise (NI01), pure complaints (IT01-IT30), and mixed feedback
      (e.g., praised staff but dirty floor) so operational defects are correctly identified
      without falsely accusing praised staff of misconduct.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer


class SentimentEngine:
    """Retail-domain sentiment analysis and contrastive clause extraction engine."""

    # Lexicon augmentations for retail, supermarket, and store operations
    RETAIL_LEXICON_UPDATES: Dict[str, float] = {
        # Positive retail & hospitality terms
        "welcoming": 2.6,
        "polite": 2.5,
        "helpful": 2.6,
        "prompt": 2.2,
        "courteous": 2.6,
        "fresh": 2.3,
        "spotless": 2.8,
        "clean": 2.0,
        "neat": 2.0,
        "hygienic": 2.4,
        "quick": 1.8,
        "efficient": 2.2,
        "cooperative": 2.2,
        "pleasant": 2.4,
        "delightful": 2.7,
        "friendly": 2.5,
        "smooth": 1.9,
        "seamless": 2.3,
        "attentive": 2.4,
        "kind": 2.2,
        "best": 3.0,
        "excellent": 3.0,
        "superb": 3.0,
        "great": 2.5,
        "good": 1.8,
        # Negative retail & operational defect terms
        "dirty": -2.6,
        "untidy": -2.2,
        "unclean": -2.5,
        "unhygienic": -2.8,
        "spoiled": -3.0,
        "rotten": -3.2,
        "decayed": -3.0,
        "cockroach": -3.5,
        "cockroaches": -3.5,
        "pest": -3.0,
        "pests": -3.0,
        "rat": -3.5,
        "rats": -3.5,
        "insect": -3.0,
        "insects": -3.0,
        "stale": -2.5,
        "expired": -2.8,
        "curdled": -2.6,
        "rude": -3.0,
        "unhelpful": -2.6,
        "arrogant": -2.7,
        "disrespectful": -3.0,
        "impolite": -2.5,
        "indifferent": -2.2,
        "overcharged": -2.8,
        "overpricing": -2.5,
        "cheat": -3.2,
        "cheated": -3.2,
        "scam": -3.2,
        "delayed": -2.0,
        "slow": -1.8,
        "worst": -3.2,
        "pathetic": -3.2,
        "terrible": -3.2,
        "horrible": -3.2,
        "disappointed": -2.4,
        "frustrated": -2.5,
        "useless": -2.6,
        "broken": -2.4,
        "breakdown": -2.5,
    }

    # Contrastive conjunctions that divide a sentence into potentially opposing clauses
    RE_CONTRASTIVE_SPLIT = re.compile(
        r"(?:,\s*|\s+)(?:but|however|although|though|yet|except that|except|whereas|on the other hand)\s+",
        re.IGNORECASE,
    )

    # Explicit defect terms to verify whether negative sentiment stems from an actual operational issue
    RE_EXPLICIT_DEFECT = re.compile(
        r"\b(dirty|unclean|untidy|stink|smell|cockroach|pest|rat|insect|spoiled|rotten|decayed|expired|"
        r"rude|arrogant|unhelpful|impolite|disrespectful|slow|queue|overcharged|overpricing|scam|"
        r"broken|leaking|not working|out of stock|empty shelf|missing)\b",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self.analyzer = SentimentIntensityAnalyzer()
        self.analyzer.lexicon.update(self.RETAIL_LEXICON_UPDATES)

    def split_into_clauses(self, text: str) -> List[str]:
        """Splits feedback into distinct clauses along contrastive conjunctions and sentence breaks."""
        text = str(text).strip()
        if not text:
            return []

        # First split by sentence delimiters
        sentence_parts = re.split(r"[.!?;\n]+", text)
        clauses = []
        for part in sentence_parts:
            part = part.strip()
            if not part:
                continue
            # Further split by contrastive transitions
            sub_clauses = self.RE_CONTRASTIVE_SPLIT.split(part)
            for sc in sub_clauses:
                sc_clean = sc.strip()
                if len(sc_clean) > 1:
                    clauses.append(sc_clean)
        return clauses or [text]

    def predict(self, text: str, rating: Optional[float] = None) -> Dict[str, Any]:
        """Classifies sentiment into Positive, Negative, Neutral, or Mixed.

        Returns:
            dict with:
                - sentiment: 'Positive', 'Negative', 'Neutral', or 'Mixed'
                - compound: overall compound sentiment score (-1.0 to 1.0)
                - pos_score: positive sentiment intensity
                - neg_score: negative sentiment intensity
                - neu_score: neutral sentiment intensity
                - is_contrastive: bool indicating if opposing sentiments were detected
                - pos_clauses: list of clauses with positive sentiment
                - neg_clauses: list of clauses with negative sentiment
        """
        raw_text = str(text).strip()
        if not raw_text:
            return {
                "sentiment": "Neutral",
                "compound": 0.0,
                "pos_score": 0.0,
                "neg_score": 0.0,
                "neu_score": 1.0,
                "is_contrastive": False,
                "pos_clauses": [],
                "neg_clauses": [],
            }

        scores = self.analyzer.polarity_scores(raw_text)
        overall_compound = float(scores["compound"])

        # Clause-level analysis for contrastive / mixed sentiment
        clauses = self.split_into_clauses(raw_text)
        pos_clauses: List[str] = []
        neg_clauses: List[str] = []

        if len(clauses) > 1:
            for clause in clauses:
                c_scores = self.analyzer.polarity_scores(clause)
                c_comp = float(c_scores["compound"])
                if c_comp >= 0.15:
                    pos_clauses.append(clause)
                elif c_comp <= -0.15 or self.RE_EXPLICIT_DEFECT.search(clause):
                    neg_clauses.append(clause)

        # Mixed condition: both distinctly positive and distinctly negative clauses exist
        is_mixed = bool(pos_clauses and neg_clauses)

        if is_mixed:
            sentiment = "Mixed"
        else:
            # Rating calibration if rating is provided
            eff_compound = overall_compound
            if rating is not None:
                if rating >= 4.0:
                    eff_compound = max(eff_compound, 0.25)
                elif rating <= 2.0:
                    eff_compound = min(eff_compound, -0.25)

            if eff_compound >= 0.10:
                sentiment = "Positive"
            elif eff_compound <= -0.10:
                sentiment = "Negative"
            elif self.RE_EXPLICIT_DEFECT.search(raw_text):
                sentiment = "Negative"
            else:
                sentiment = "Neutral"

        return {
            "sentiment": sentiment,
            "compound": round(overall_compound, 4),
            "pos_score": round(float(scores["pos"]), 4),
            "neg_score": round(float(scores["neg"]), 4),
            "neu_score": round(float(scores["neu"]), 4),
            "is_contrastive": is_mixed,
            "pos_clauses": pos_clauses,
            "neg_clauses": neg_clauses,
        }

    def is_pure_praise(self, text: str, rating: Optional[float] = None) -> bool:
        """Returns True if feedback is purely complimentary with no operational defects."""
        res = self.predict(text, rating=rating)
        if res["sentiment"] != "Positive":
            return False
        # Ensure no negative operational defects were masked
        if self.RE_EXPLICIT_DEFECT.search(text):
            return False
        return True


# Global singleton instance
sentiment_engine = SentimentEngine()
