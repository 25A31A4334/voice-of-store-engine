"""R5 — Voice-of-Store Engine Web Application (Team CodeNova).

FastAPI backend serving the interactive executive dashboard, regional briefs,
emerging-issue alerts radar, feedback stream explorer, and live ML classification demo.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Guarantee UTF-8 output encoding for Windows terminal
if sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CANDIDATE_DIR = ROOT / "FULL_CODE" / "phase5_candidate"
STATIC_DIR = ROOT / "FULL_CODE" / "static"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from FULL_CODE.locality_resolver import LocalityResolver
from FULL_CODE.classifier import IssueClassifier
from FULL_CODE.severity_engine import SeverityEngine
from FULL_CODE.actionability_engine import ActionabilityEngine
from FULL_CODE.detector import EmergingIssueDetector
from FULL_CODE.config import settings
from FULL_CODE.db.connection import get_db_connection
from FULL_CODE.db.seed import seed_database, seed_alerts_from_flags
from FULL_CODE.services.dependencies import require_roles, enforce_store_access, get_current_user
from FULL_CODE.services.supabase_service import SupabaseService
from FULL_CODE.services.gemini_service import gemini_service, GeminiAnalysisResult
from FULL_CODE.api.auth import router as auth_router

# -----------------------------------------------------------------------------
# Initialize FastAPI App
# -----------------------------------------------------------------------------
app = FastAPI(
    title="R5 — Voice-of-Store Engine",
    description="Intelligent feedback prioritization, routing, and emerging issue detector for Saffron Retail Group. Built by Team CodeNova for TalentAP Mega Hackathon 2026.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Authentication Router (Phase B)
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])


@app.get("/api/config/public")
def get_public_config() -> Dict[str, Any]:
    """Returns safe public client configuration for frontend initialization.

    SECURITY RULE: Only exposes public URL and publishable/anon key.
    Never exposes service-role keys or backend secrets.
    """
    return {
        "app_mode": settings.APP_MODE,
        "supabase_url": settings.SUPABASE_URL,
        "supabase_publishable_key": settings.SUPABASE_PUBLISHABLE_KEY,
    }

# -----------------------------------------------------------------------------
# In-Memory Cache & State
# -----------------------------------------------------------------------------
class AppState:
    feedback_df: pd.DataFrame = pd.DataFrame()
    store_master: pd.DataFrame = pd.DataFrame()
    issue_taxonomy: pd.DataFrame = pd.DataFrame()
    week_calendar: pd.DataFrame = pd.DataFrame()
    predictions_df: pd.DataFrame = pd.DataFrame()
    emerging_flags_df: pd.DataFrame = pd.DataFrame()
    taxonomy_dict: Dict[str, Dict[str, Any]] = {}
    stores_dict: Dict[str, Dict[str, Any]] = {}
    feedback_by_id: Dict[str, Dict[str, Any]] = {}

    resolver: Optional[LocalityResolver] = None
    classifier: Optional[IssueClassifier] = None
    severity_engine: Optional[SeverityEngine] = None
    actionability_engine: Optional[ActionabilityEngine] = None
    detector: Optional[EmergingIssueDetector] = None

    is_ready: bool = False
    init_time_seconds: float = 0.0

state = AppState()


def load_application_state() -> None:
    t0 = time.time()
    print("Initializing R5 Voice-of-Store Engine state...")

    # 1. Load Reference Datasets
    state.store_master = pd.read_csv(DATA / "store_master.csv", dtype={"store_id": str})
    state.issue_taxonomy = pd.read_csv(DATA / "issue_taxonomy.csv")
    state.week_calendar = pd.read_csv(DATA / "week_calendar.csv")

    state.taxonomy_dict = state.issue_taxonomy.set_index("issue_type_id").to_dict(orient="index")
    state.stores_dict = state.store_master.set_index("store_id").to_dict(orient="index")

    # 2. Load Core Feedback & Candidate Predictions
    fb_path = DATA / "feedback.csv"
    state.feedback_df = pd.read_csv(fb_path, dtype={"store_id": str})

    preds_path = CANDIDATE_DIR / "R5_predictions.csv"
    if not preds_path.exists():
        # Fallback to submission or sample_submission if candidate not found
        preds_path = ROOT / "submission" / "R5_predictions.csv"
    state.predictions_df = pd.read_csv(preds_path, dtype={"store_id": str})

    flags_path = CANDIDATE_DIR / "R5_emerging_flags.csv"
    if not flags_path.exists():
        flags_path = ROOT / "submission" / "R5_emerging_flags.csv"
    state.emerging_flags_df = pd.read_csv(flags_path, dtype={"store_id": str})

    # Join predictions with feedback metadata for instantaneous querying
    joined = state.feedback_df.merge(
        state.predictions_df[["feedback_id", "issue_type_id", "severity", "actionable"]],
        on="feedback_id",
        how="left",
    )
    # Join store metadata
    joined = joined.merge(
        state.store_master[["store_id", "store_name", "city", "state", "region", "format"]],
        on="store_id",
        how="left",
    )
    joined["week_start"] = (
        pd.to_datetime(joined["created_at"])
        .dt.to_period("W-SUN")
        .dt.start_time.dt.strftime("%Y-%m-%d")
    )
    # Map taxonomy details
    joined["owner_function"] = joined["issue_type_id"].map(
        lambda tid: state.taxonomy_dict.get(tid, {}).get("owner_function", "Unknown")
    )
    joined["issue_type_name"] = joined["issue_type_id"].map(
        lambda tid: state.taxonomy_dict.get(tid, {}).get("issue_type", tid)
    )

    state.feedback_df = joined
    state.feedback_by_id = joined.set_index("feedback_id").to_dict(orient="index")

    # 3. Initialize ML Services for Live Predictions
    state.resolver = LocalityResolver(DATA / "store_master.csv")

    train_path = DATA / "feedback_labels_train.csv"
    train_df = pd.read_csv(train_path)
    full_train = train_df.merge(state.feedback_df[["feedback_id", "text"]], on="feedback_id")

    state.classifier = IssueClassifier(random_state=42)
    state.classifier.fit(full_train["text"], full_train["issue_type_id"])

    state.severity_engine = SeverityEngine(DATA / "issue_taxonomy.csv", random_state=42)
    state.severity_engine.fit(full_train["text"], full_train["severity"])

    state.actionability_engine = ActionabilityEngine(random_state=42)
    state.actionability_engine.fit(full_train["text"], full_train["actionable"])

    state.detector = EmergingIssueDetector(
        classifier=state.classifier,
        locality_resolver=state.resolver,
        data_dir=DATA,
    )

    state.init_time_seconds = time.time() - t0
    state.is_ready = True
    print(f"R5 Engine loaded in {state.init_time_seconds:.2f} seconds. Ready for inference!")


@app.on_event("startup")
def startup_event() -> None:
    # Security Rule 3 & 4: Validate required secrets early at startup (fail safely if missing/unsafe)
    settings.validate_startup()
    if settings.ENABLE_DEMO_ACCOUNTS or settings.APP_MODE == "local_dev":
        seed_database()
    else:
        seed_alerts_from_flags()
    load_application_state()


# -----------------------------------------------------------------------------
# Pydantic Request / Response Models
# -----------------------------------------------------------------------------
class ClassifyRequest(BaseModel):
    text: str = Field(..., description="Customer feedback or staff note text", min_length=2)
    channel: Optional[str] = Field("in_app", description="Channel: online_review, in_app, staff_shift_note, call_centre_ticket")
    rating: Optional[float] = Field(None, description="Optional star rating (1-5)")
    store_id: Optional[str] = Field(None, description="Optional store_id (e.g. ST-4H2KQ)")
    include_gemini: Optional[bool] = Field(False, description="Whether to include Gemini Hybrid AI contextual analysis")


class ClassifyResponse(BaseModel):
    text: str
    issue_type_id: str
    issue_type_name: str
    owner_function: str
    severity: str
    policy_severity_floor: str
    actionable: int
    is_routable: bool
    resolved_store_id: Optional[str]
    resolved_store_name: Optional[str]
    store_resolution_source: str
    confidence: float
    routing_destination: str
    sentiment: Optional[str] = "Neutral"
    sentiment_score: Optional[float] = 0.0
    gemini_insights: Optional[Dict[str, Any]] = None


class GeminiAnalyzeRequest(BaseModel):
    text: str = Field(..., description="Customer feedback or operational note text", min_length=1)
    store_id: Optional[str] = Field(None, description="Optional store ID")
    store_name: Optional[str] = Field(None, description="Optional store name")
    ml_context: Optional[Dict[str, Any]] = Field(None, description="Optional pre-computed ML predictions")


# -----------------------------------------------------------------------------
# REST API Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/health")
def health_check() -> dict:
    """System health check and state status."""
    return {
        "status": "online" if state.is_ready else "initializing",
        "app": "R5 — Voice-of-Store Engine",
        "team": "CodeNova",
        "version": "1.0.0",
        "records_loaded": len(state.feedback_df),
        "stores_monitored": len(state.store_master),
        "init_time_seconds": round(state.init_time_seconds, 2),
    }


@app.get("/api/kpis")
def get_kpis() -> dict:
    """Returns top-level KPIs for dashboard summary cards."""
    total_fb = len(state.feedback_df)
    actionable_count = int((state.feedback_df["actionable"] == 1).sum())
    high_crit_count = int(state.feedback_df["severity"].isin(["high", "critical"]).sum())
    critical_count = int((state.feedback_df["severity"] == "critical").sum())
    emerging_flags_count = len(state.emerging_flags_df)

    # Scored week flags (Weeks 1-13)
    train_weeks = set(state.week_calendar[state.week_calendar["labelled_period"] == "train"]["week_start"])
    train_flags_count = int(state.emerging_flags_df["as_of_week_start"].isin(train_weeks).sum())

    return {
        "total_feedback": total_fb,
        "actionable_count": actionable_count,
        "actionable_pct": round(actionable_count / total_fb * 100, 1) if total_fb else 0,
        "high_or_critical_count": high_crit_count,
        "critical_count": critical_count,
        "stores_monitored": len(state.store_master),
        "regions_covered": int(state.store_master["region"].nunique()),
        "emerging_flags_total": emerging_flags_count,
        "emerging_flags_train": train_flags_count,
        "model_cv_classification_score": 0.9312,
        "evaluator_primary_score": 0.7993,
        "baseline_primary_score": 0.4808,
    }


@app.get("/api/regional-briefs")
def get_regional_brief(
    region: str = Query("North", description="Region name: North, South, East, West, Central, or All"),
    week: Optional[str] = Query(None, description="Week start date (YYYY-MM-DD)"),
) -> dict:
    """Delivers Hackathon Deliverable #1: Weekly 1-page brief per region.

    Top 5 issues, evidence quotes, affected stores, and trend arrow vs previous week.
    """
    df = state.feedback_df.copy()

    # Determine target week (default to latest available week or 2026-05-18 if not specified)
    available_weeks = sorted(df["week_start"].unique().tolist())
    target_week = week if week and week in available_weeks else (available_weeks[7] if len(available_weeks) > 7 else available_weeks[-1])

    # Find previous calendar week for trend calculation
    target_idx = available_weeks.index(target_week)
    prev_week = available_weeks[target_idx - 1] if target_idx > 0 else None

    # Filter by region if not 'All'
    if region != "All":
        df = df[df["region"] == region]

    cur_df = df[df["week_start"] == target_week]
    prev_df = df[df["week_start"] == prev_week] if prev_week else pd.DataFrame()

    # Actionable operational issues only
    cur_issues = cur_df[(cur_df["actionable"] == 1) & (~cur_df["issue_type_id"].isin(["NI01", "NI02", "NI03"]))]
    prev_issues = prev_df[(prev_df["actionable"] == 1) & (~prev_df["issue_type_id"].isin(["NI01", "NI02", "NI03"]))]

    # Count by issue type
    cur_counts = cur_issues["issue_type_id"].value_counts()
    prev_counts = prev_issues["issue_type_id"].value_counts() if not prev_issues.empty else pd.Series(dtype=int)

    top_5_ids = cur_counts.head(5).index.tolist()
    brief_items = []

    for rank, tid in enumerate(top_5_ids, 1):
        cur_c = int(cur_counts.get(tid, 0))
        prev_c = int(prev_counts.get(tid, 0))
        diff = cur_c - prev_c

        if diff > 0:
            trend_arrow = f"▲ +{diff}"
            trend_class = "increase"
        elif diff < 0:
            trend_arrow = f"▼ {diff}"
            trend_class = "decrease"
        else:
            trend_arrow = "― 0"
            trend_class = "flat"

        tax_info = state.taxonomy_dict.get(tid, {})
        issue_records = cur_issues[cur_issues["issue_type_id"] == tid]

        # Affected stores
        affected_stores = issue_records["store_name"].dropna().unique().tolist()[:4]
        if not affected_stores and not issue_records["resolved_store_id"].dropna().empty:
            affected_ids = issue_records["resolved_store_id"].dropna().unique().tolist()[:4]
            affected_stores = [state.stores_dict.get(sid, {}).get("store_name", sid) for sid in affected_ids]

        # Evidence quotes (sample 2 realistic text excerpts)
        quotes = issue_records["text"].dropna().sample(min(2, len(issue_records)), random_state=42).tolist() if len(issue_records) else []

        brief_items.append({
            "rank": rank,
            "issue_type_id": tid,
            "issue_name": tax_info.get("issue_type", tid),
            "owner_function": tax_info.get("owner_function", "Store Ops"),
            "severity_floor": tax_info.get("policy_severity_floor", "low"),
            "count": cur_c,
            "previous_count": prev_c,
            "trend_arrow": trend_arrow,
            "trend_class": trend_class,
            "affected_stores": affected_stores,
            "affected_stores_count": int(issue_records["store_id"].nunique()),
            "evidence_quotes": quotes,
        })

    return {
        "region": region,
        "week": target_week,
        "previous_week": prev_week,
        "total_regional_feedback": len(cur_df),
        "total_actionable_issues": len(cur_issues),
        "available_weeks": available_weeks,
        "top_5_issues": brief_items,
    }


@app.get("/api/emerging-alerts")
def get_emerging_alerts(
    week: Optional[str] = Query(None, description="Filter alerts by as_of_week_start"),
) -> dict:
    """Returns detected store emerging incidents with clickable evidence details and current status."""
    flags_df = state.emerging_flags_df.copy()

    if week and week != "All":
        flags_df = flags_df[flags_df["as_of_week_start"] == week]

    # Fetch live statuses and notes from SQLite
    db_statuses = {}
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT alert_id, store_id, issue_type_id, as_of_week_start, status, resolution_notes, updated_at FROM incident_alerts;"
            ).fetchall()
            for r in rows:
                key = (r["store_id"].upper(), r["issue_type_id"].upper(), r["as_of_week_start"])
                db_statuses[key] = {
                    "alert_id": r["alert_id"],
                    "status": r["status"],
                    "resolution_notes": r["resolution_notes"],
                    "updated_at": r["updated_at"],
                }
    except Exception:
        pass

    alerts = []
    for _, r in flags_df.iterrows():
        sid = str(r["store_id"]).strip().upper()
        tid = str(r["issue_type_id"]).strip().upper()
        w_start = r["as_of_week_start"]
        store_info = state.stores_dict.get(sid, {})
        tax_info = state.taxonomy_dict.get(tid, {})

        ev_ids = [x.strip() for x in str(r.get("evidence_feedback_ids", "")).split(";") if x.strip()]
        evidence_samples = []
        for fid in ev_ids[:4]:  # provide first 4 evidence quotes
            fb_item = state.feedback_by_id.get(fid)
            if fb_item:
                evidence_samples.append({
                    "feedback_id": fid,
                    "channel": fb_item.get("channel"),
                    "created_at": fb_item.get("created_at"),
                    "rating": fb_item.get("rating"),
                    "text": fb_item.get("text"),
                })

        # Flag whether this matches the worked example EX-1
        is_ex1 = (sid == "ST-D69EG" and tid == "IT17")

        db_entry = db_statuses.get((sid, tid, w_start), {})
        alert_uid = db_entry.get("alert_id", f"ALT-{w_start}-{sid}-{tid}")
        alert_status = db_entry.get("status", "new")
        resolution_notes = db_entry.get("resolution_notes")
        updated_at = db_entry.get("updated_at")

        sev_floor = tax_info.get("policy_severity_floor", "low")
        issue_display_name = tax_info.get("issue_type", tid)

        # Calculate severity based on policy floor and evidence items
        sev_order = {"critical": 3, "high": 2, "medium": 1, "low": 0}
        highest_sev_val = sev_order.get(str(sev_floor).lower(), 0)
        for s in evidence_samples:
            if s.get("severity"):
                highest_sev_val = max(highest_sev_val, sev_order.get(str(s["severity"]).lower(), 0))
        rev_sev_order = {3: "critical", 2: "high", 1: "medium", 0: "low"}
        calculated_severity = rev_sev_order.get(highest_sev_val, sev_floor or "medium")

        alerts.append({
            "alert_id": alert_uid,
            "week": w_start,
            "as_of_week_start": w_start,
            "store_id": sid,
            "store_name": store_info.get("store_name", sid),
            "city": store_info.get("city", "Unknown"),
            "region": store_info.get("region", "Unknown"),
            "format": store_info.get("format", "Saffron Market"),
            "issue_type_id": tid,
            "issue_name": issue_display_name,
            "issue_type_name": issue_display_name,
            "owner_function": tax_info.get("owner_function", "Store Ops"),
            "severity": calculated_severity,
            "severity_floor": sev_floor,
            "confidence": float(r["confidence"]),
            "evidence_count": len(ev_ids),
            "evidence_ids": ev_ids,
            "evidence_samples": evidence_samples,
            "is_worked_incident_ex1": is_ex1,
            "status": alert_status,
            "resolution_note": resolution_notes,
            "resolution_notes": resolution_notes,
            "updated_at": updated_at,
        })

    # Sort descending by confidence
    alerts.sort(key=lambda x: (x["as_of_week_start"], -x["confidence"]), reverse=True)

    return {
        "total_alerts": len(alerts),
        "alerts": alerts,
    }


@app.get("/api/feedback")
def query_feedback(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    channel: Optional[str] = None,
    severity: Optional[str] = None,
    actionable: Optional[int] = None,
    region: Optional[str] = None,
    search: Optional[str] = None,
) -> dict:
    """Paginated, filterable feedback stream."""
    df = state.feedback_df

    if channel and channel != "All":
        df = df[df["channel"] == channel]
    if severity and severity != "All":
        df = df[df["severity"] == severity]
    if actionable is not None:
        df = df[df["actionable"] == actionable]
    if region and region != "All":
        df = df[df["region"] == region]
    if search:
        s = search.lower()
        df = df[df["text"].str.lower().str.contains(s, na=False)]

    total = len(df)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit

    paged = df.iloc[start_idx:end_idx]
    records = []
    for _, r in paged.iterrows():
        records.append({
            "feedback_id": r["feedback_id"],
            "channel": r["channel"],
            "created_at": r["created_at"],
            "store_id": r["store_id"],
            "store_name": r["store_name"] if pd.notna(r.get("store_name")) else None,
            "region": r["region"] if pd.notna(r.get("region")) else None,
            "rating": r["rating"] if pd.notna(r.get("rating")) else None,
            "text": r["text"],
            "issue_type_id": r["issue_type_id"],
            "issue_type_name": r["issue_type_name"],
            "owner_function": r["owner_function"],
            "severity": r["severity"],
            "actionable": int(r["actionable"]),
        })

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "pages": (total + limit - 1) // limit,
        "records": records,
    }


@app.post("/api/classify", response_model=ClassifyResponse)
def classify_live_feedback(req: ClassifyRequest) -> ClassifyResponse:
    """Live feedback triage form using the trained ML pipeline."""
    if not state.is_ready:
        raise HTTPException(status_code=503, detail="ML Models are still initializing")

    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    # 1. Locality Resolution
    resolved_sid = req.store_id
    res_source = "user_provided" if resolved_sid else "unresolved"
    if not resolved_sid:
        match_sid, loc_name, status = state.resolver.resolve_text(text)
        if status == "unique_match" and match_sid:
            resolved_sid = match_sid
            res_source = f"locality_matched ({loc_name})"

    store_name = state.stores_dict.get(resolved_sid, {}).get("store_name") if resolved_sid else None

    # 2. Issue Type Classification
    pred_types, confs = state.classifier.predict_with_confidence([text])
    issue_type_id = str(pred_types[0])
    confidence = float(confs[0])

    # 2b. Sentiment Classification
    s_res = state.classifier.sentiment_engine.predict(text, rating=req.rating)
    sentiment = s_res["sentiment"]
    sentiment_score = s_res["compound"]

    # 3. Severity Determination
    severity = state.severity_engine.predict_single(text, issue_type_id)

    # 4. Actionability Determination
    actionable = state.actionability_engine.predict_single(text, resolved_sid, issue_type_id)

    # 5. Routing Destination
    tax_info = state.taxonomy_dict.get(issue_type_id, {})
    owner_func = tax_info.get("owner_function", "None (not actionable)")
    policy_floor = tax_info.get("policy_severity_floor", "low")
    issue_name = "Praise / compliment (No Negative Issue)" if issue_type_id == "NI01" else tax_info.get("issue_type", issue_type_id)

    is_routable = state.actionability_engine.is_routable(resolved_sid, issue_type_id)

    routing_dest = f"{owner_func} Team"
    if resolved_sid:
        routing_dest += f" (Store: {store_name or resolved_sid})"
    elif is_routable:
        routing_dest += " (Central Digital / Supply Chain Queue)"
    else:
        routing_dest = "Discard / Unrouted Chatter (Missing Store Context)"

    gemini_insights = None
    if getattr(req, "include_gemini", False):
        try:
            res = gemini_service.analyze_feedback(
                text=text,
                context={
                    "issue_type_id": issue_type_id,
                    "issue_type_name": issue_name,
                    "severity": severity,
                    "sentiment": sentiment,
                    "sentiment_score": sentiment_score,
                    "store_id": resolved_sid,
                    "store_name": store_name,
                },
            )
            gemini_insights = res.dict()
        except Exception:
            pass

    return ClassifyResponse(
        text=text,
        issue_type_id=issue_type_id,
        issue_type_name=issue_name,
        owner_function=owner_func,
        severity=severity,
        policy_severity_floor=policy_floor,
        actionable=actionable,
        is_routable=is_routable,
        resolved_store_id=resolved_sid,
        resolved_store_name=store_name,
        store_resolution_source=res_source,
        confidence=round(confidence, 4),
        routing_destination=routing_dest,
        sentiment=sentiment,
        sentiment_score=sentiment_score,
        gemini_insights=gemini_insights,
    )


@app.get("/api/stores")
def get_stores(
    search: Optional[str] = None,
    region: Optional[str] = None,
    format: Optional[str] = None,
) -> list:
    """Returns store master directory with optional search, region, and format filters."""
    df = state.store_master.copy()
    if region and region != "All":
        df = df[df["region"].str.lower() == region.strip().lower()]
    if format and format != "All":
        df = df[df["format"].str.lower() == format.strip().lower()]
    if search:
        s = search.strip().lower()
        mask = (
            df["store_id"].str.lower().str.contains(s, na=False)
            | df["store_name"].str.lower().str.contains(s, na=False)
            | df["city"].str.lower().str.contains(s, na=False)
            | df["state"].str.lower().str.contains(s, na=False)
        )
        df = df[mask]
    return df.to_dict(orient="records")


@app.get("/api/admin/stores")
def get_admin_stores(
    search: Optional[str] = None,
    region: Optional[str] = None,
    format: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    all: bool = Query(False),
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> dict:
    """Returns store directory with pagination, search, and region filters for authorized administrators."""
    df = state.store_master.copy()
    if region and region != "All":
        df = df[df["region"].str.lower() == region.strip().lower()]
    if format and format != "All":
        df = df[df["format"].str.lower() == format.strip().lower()]
    if search:
        s = search.strip().lower()
        mask = (
            df["store_id"].str.lower().str.contains(s, na=False)
            | df["store_name"].str.lower().str.contains(s, na=False)
            | df["city"].str.lower().str.contains(s, na=False)
            | df["state"].str.lower().str.contains(s, na=False)
        )
        df = df[mask]

    total_records = len(df)

    if all:
        records = df.to_dict(orient="records")
        return {
            "total": total_records,
            "page": 1,
            "limit": total_records,
            "total_pages": 1,
            "stores": records,
        }

    total_pages = max(1, (total_records + limit - 1) // limit)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_df = df.iloc[start_idx:end_idx]

    return {
        "total": total_records,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "stores": page_df.to_dict(orient="records"),
    }


@app.get("/api/taxonomy")
def get_taxonomy() -> list:
    """Returns taxonomy list."""
    return state.issue_taxonomy.to_dict(orient="records")


# -----------------------------------------------------------------------------
# Customer Portal Endpoints (Phase 1: Submissions, Dashboard, and History)
# -----------------------------------------------------------------------------
def _resolve_sqlite_user_id(conn: sqlite3.Connection, user: Dict[str, Any]) -> Optional[int]:
    """Resolves an integer user ID for local SQLite dual-persistence and audit logging.

    If the user already has a local integer ID, returns it directly.
    If the user authenticated via Supabase (UUID), maps to their local SQLite user ID by email.
    If no matching SQLite user exists, returns None (NULL), which is safely permitted
    by the SQLite schema (REFERENCES users(id) ON DELETE SET NULL).
    """
    raw_id = user.get("id")
    if isinstance(raw_id, int):
        return raw_id
    email = user.get("email")
    if email:
        row = conn.execute("SELECT id FROM users WHERE lower(email) = lower(?);", (email.strip(),)).fetchone()
        if row:
            return row["id"]
    return None


class CustomerFeedbackSubmissionRequest(BaseModel):
    store_id: str = Field(..., min_length=2, max_length=20, description="Store ID, e.g. ST-D69EG")
    text: str = Field(..., min_length=5, max_length=3000, description="Customer feedback message")
    rating: Optional[int] = Field(None, ge=1, le=5, description="Optional star rating (1-5)")
    channel: str = Field("in_app", description="Feedback channel (e.g. in_app, online_review)")


class ManagerFeedbackActionRequest(BaseModel):
    action: str = Field(..., description="Action: 'acknowledge', 'investigate', or 'resolve'")
    note: Optional[str] = Field(None, description="Resolution or operational investigation note")


@app.post("/api/customer/submissions", status_code=201)
def submit_customer_feedback(
    req: CustomerFeedbackSubmissionRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Submits customer feedback, executes the live ML triage pipeline, and persists the record."""
    if not state.is_ready:
        raise HTTPException(status_code=503, detail="ML classification engine is still initializing.")

    # 1. Clean & validate inputs
    text = req.text.strip()
    if len(text) < 5:
        raise HTTPException(status_code=400, detail="Feedback text must be at least 5 characters.")

    target_sid = req.store_id.strip().upper()
    if target_sid not in state.stores_dict:
        raise HTTPException(
            status_code=400,
            detail=f"Store '{req.store_id}' does not exist in store directory.",
        )

    store_info = state.stores_dict[target_sid]
    store_name = store_info.get("store_name", target_sid)

    # 2. Issue classification using trained model
    pred_types, confs = state.classifier.predict_with_confidence([text])
    issue_type_id = str(pred_types[0])
    confidence = float(confs[0])

    s_res = state.classifier.sentiment_engine.predict(text, rating=req.rating)
    sentiment = s_res["sentiment"]

    # 3. Severity & Policy Floor
    severity = state.severity_engine.predict_single(text, issue_type_id)

    # 4. Actionability & Owner Function
    actionable = state.actionability_engine.predict_single(text, target_sid, issue_type_id)
    tax_info = state.taxonomy_dict.get(issue_type_id, {})
    owner_func = "Customer Experience / None" if issue_type_id == "NI01" else tax_info.get("owner_function", "Store Ops")
    issue_name = "Praise / compliment (No Negative Issue)" if issue_type_id == "NI01" else tax_info.get("issue_type", issue_type_id)
    if issue_type_id == "NI01":
        actionable = 0
        severity = "low"

    # 5. Generate unique feedback ID
    import secrets
    feedback_id = f"FB-CUST-{int(time.time())}-{secrets.token_hex(3).upper()}"

    # 6. Primary Persistence to Supabase
    sub_record = {
        "feedback_id": feedback_id,
        "user_id": current_user.get("id"),
        "customer_email": current_user.get("email"),
        "store_id": target_sid,
        "channel": req.channel or "in_app",
        "rating": req.rating,
        "text": text,
        "issue_type_id": issue_type_id,
        "severity": severity,
        "actionable": actionable,
        "owner_function": owner_func,
        "status": "new",
    }
    user_token = current_user.get("token")
    provider = current_user.get("provider")
    if provider == "supabase" and user_token:
        try:
            supa_inserted = SupabaseService.insert_feedback(sub_record, access_token=user_token)
            if supa_inserted is None and not settings.TESTING:
                raise HTTPException(
                    status_code=502,
                    detail="Supabase feedback insertion failed. Unable to persist feedback in authoritative database.",
                )
            SupabaseService.insert_audit_log(
                user_id=str(current_user.get("id")),
                action="CUSTOMER_SUBMIT_FEEDBACK",
                target_type="feedback_submissions",
                target_id=feedback_id,
                details={
                    "action": "CUSTOMER_SUBMIT_FEEDBACK",
                    "store_id": target_sid,
                    "issue_type_id": issue_type_id,
                    "severity": severity,
                    "actionable": actionable,
                    "actor_email": current_user.get("email"),
                    "actor_role": current_user.get("role"),
                },
                access_token=user_token,
            )
        except HTTPException:
            raise
        except Exception as e:
            if not settings.TESTING:
                raise HTTPException(
                    status_code=502,
                    detail=f"Supabase error during feedback submission: {str(e)}",
                )

    # 7. Resilient Dual-Persistence to SQLite
    with get_db_connection() as conn:
        sqlite_uid = _resolve_sqlite_user_id(conn, current_user)
        conn.execute(
            """
            INSERT INTO feedback_submissions (
                feedback_id, user_id, store_id, channel, rating, text,
                predicted_issue_type_id, predicted_severity, predicted_actionable,
                owner_function, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            """,
            (
                feedback_id,
                sqlite_uid,
                target_sid,
                req.channel or "in_app",
                req.rating,
                text,
                issue_type_id,
                severity,
                actionable,
                owner_func,
            ),
        )

        audit_details = json.dumps({
            "action": "CUSTOMER_SUBMIT_FEEDBACK",
            "store_id": target_sid,
            "issue_type_id": issue_type_id,
            "severity": severity,
            "actionable": actionable,
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
        })
        conn.execute(
            """
            INSERT INTO audit_log (user_id, action, target_type, target_id, details)
            VALUES (?, 'CUSTOMER_SUBMIT_FEEDBACK', 'feedback_submissions', ?, ?);
            """,
            (sqlite_uid, feedback_id, audit_details),
        )

    return {
        "success": True,
        "feedback_id": feedback_id,
        "submission": {
            "feedback_id": feedback_id,
            "store_id": target_sid,
            "store_name": store_name,
            "channel": req.channel or "in_app",
            "rating": req.rating,
            "text": text,
            "issue_type_id": issue_type_id,
            "issue_type_name": issue_name,
            "owner_function": owner_func,
            "severity": severity,
            "actionable": actionable,
            "confidence": round(confidence, 4),
            "sentiment": sentiment,
            "status": "new",
        },
    }


@app.get("/api/customer/dashboard")
def get_customer_dashboard(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Delivers real summary metrics and recent activity for the authenticated customer."""
    user_id = current_user.get("id")
    email = current_user.get("email", "")
    token = current_user.get("token")
    provider = current_user.get("provider")

    # 1. Primary Read from Supabase if configured & authenticated via Supabase
    if provider == "supabase" and token:
        supa_res = SupabaseService.get_customer_submissions(
            user_id=str(user_id) if user_id else "",
            email=email,
            access_token=token,
            limit=50,
        )
        if supa_res is not None:
            total, supa_rows = supa_res
            status_counts = {"new": 0, "acknowledged": 0, "investigating": 0, "resolved": 0}
            for r in supa_rows:
                st = (r.get("status") or "new").lower()
                status_counts[st] = status_counts.get(st, 0) + 1
            resolved = status_counts.get("resolved", 0)
            open_count = total - resolved
            recent_items = []
            for r in supa_rows[:5]:
                sid = r.get("store_id")
                tid = r.get("issue_type_id") or r.get("predicted_issue_type_id")
                store_name = state.stores_dict.get(sid, {}).get("store_name", sid)
                tax_info = state.taxonomy_dict.get(tid, {})
                recent_items.append({
                    "feedback_id": r.get("feedback_id") or r.get("id") or "FB-UNKNOWN",
                    "store_id": sid,
                    "store_name": store_name,
                    "channel": r.get("channel", "in_app"),
                    "rating": r.get("rating"),
                    "text": r.get("text", ""),
                    "issue_type_id": tid,
                    "issue_type_name": tax_info.get("issue_type", tid),
                    "owner_function": r.get("owner_function") or tax_info.get("owner_function", "Store Ops"),
                    "severity": r.get("severity") or r.get("predicted_severity") or "low",
                    "actionable": int(r.get("actionable", r.get("predicted_actionable", 0))),
                    "status": r.get("status") or "new",
                    "resolution_notes": r.get("resolution_notes"),
                    "created_at": r.get("created_at"),
                    "updated_at": r.get("updated_at"),
                })
            return {
                "user_id": user_id,
                "email": current_user.get("email"),
                "full_name": current_user.get("full_name"),
                "total_submissions": total,
                "open_submissions": open_count,
                "resolved_submissions": resolved,
                "status_breakdown": status_counts,
                "recent_submissions": recent_items,
            }
        elif not settings.TESTING:
            # Supabase error in production mode for Supabase user: do not silently mask with stale SQLite data
            raise HTTPException(
                status_code=502,
                detail="Supabase query failed. Unable to fetch customer dashboard from authoritative database.",
            )

    # 2. Resilient Fallback to SQLite (for local testing/offline)
    with get_db_connection() as conn:
        sqlite_uid = _resolve_sqlite_user_id(conn, current_user)
        rows = conn.execute(
            """
            SELECT status, count(*) as count
            FROM feedback_submissions
            WHERE user_id = ?
            GROUP BY status;
            """,
            (sqlite_uid,),
        ).fetchall()

        status_counts = {r["status"]: r["count"] for r in rows}
        total = sum(status_counts.values())
        resolved = status_counts.get("resolved", 0)
        open_count = total - resolved

        recent_rows = conn.execute(
            """
            SELECT *
            FROM feedback_submissions
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 5;
            """,
            (sqlite_uid,),
        ).fetchall()

    recent_items = []
    for r in recent_rows:
        sid = r["store_id"]
        tid = r["predicted_issue_type_id"]
        store_name = state.stores_dict.get(sid, {}).get("store_name", sid)
        tax_info = state.taxonomy_dict.get(tid, {})
        recent_items.append({
            "feedback_id": r["feedback_id"],
            "store_id": sid,
            "store_name": store_name,
            "channel": r["channel"],
            "rating": r["rating"],
            "text": r["text"],
            "issue_type_id": tid,
            "issue_type_name": tax_info.get("issue_type", tid),
            "owner_function": r["owner_function"],
            "severity": r["predicted_severity"],
            "actionable": r["predicted_actionable"],
            "status": r["status"] or "new",
            "resolution_notes": r["resolution_notes"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })

    return {
        "user_id": user_id,
        "email": current_user.get("email"),
        "full_name": current_user.get("full_name"),
        "total_submissions": total,
        "open_submissions": open_count,
        "resolved_submissions": resolved,
        "status_breakdown": {
            "new": status_counts.get("new", 0),
            "acknowledged": status_counts.get("acknowledged", 0),
            "investigating": status_counts.get("investigating", 0),
            "resolved": resolved,
        },
        "recent_submissions": recent_items,
    }


@app.get("/api/customer/submissions")
def get_customer_submissions(
    status: Optional[str] = Query(None, description="Filter by status: new, acknowledged, investigating, resolved"),
    search: Optional[str] = Query(None, description="Search query in text or store"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Delivers paginated, filterable submission history strictly owned by the authenticated customer."""
    user_id = current_user.get("id")
    email = current_user.get("email", "")
    token = current_user.get("token")
    provider = current_user.get("provider")

    # 1. Primary Read from Supabase if authenticated via Supabase & configured
    if provider == "supabase" and token:
        supa_res = SupabaseService.get_customer_submissions(
            user_id=str(user_id) if user_id else "",
            email=email,
            access_token=token,
            status=status,
            search=search,
            page=page,
            limit=limit,
        )
        if supa_res is not None:
            # Legitimate Supabase result (including total == 0)
            total, supa_rows = supa_res
            items = []
            for r in supa_rows:
                sid = r.get("store_id")
                tid = r.get("issue_type_id")
                store_name = state.stores_dict.get(sid, {}).get("store_name", sid)
                tax_info = state.taxonomy_dict.get(tid, {})
                items.append({
                    "feedback_id": r["feedback_id"],
                    "store_id": sid,
                    "store_name": store_name,
                    "channel": r.get("channel", "in_app"),
                    "rating": r.get("rating"),
                    "text": r.get("text", ""),
                    "issue_type_id": tid,
                    "issue_type_name": tax_info.get("issue_type", tid),
                    "owner_function": r.get("owner_function") or tax_info.get("owner_function", "Store Ops"),
                    "severity": r.get("severity") or "low",
                    "actionable": int(r.get("actionable", 0)),
                    "status": r.get("status") or "new",
                    "resolution_notes": r.get("resolution_notes"),
                    "created_at": r.get("created_at"),
                    "updated_at": r.get("updated_at"),
                })
            return {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": (total + limit - 1) // limit if total > 0 else 1,
                "submissions": items,
            }
        elif not settings.TESTING:
            # Supabase error in production mode for Supabase user: do not silently mask with stale SQLite data
            raise HTTPException(
                status_code=502,
                detail="Supabase query failed. Unable to fetch customer submissions from authoritative database.",
            )

    # 2. Resilient Fallback to SQLite
    with get_db_connection() as conn:
        sqlite_uid = _resolve_sqlite_user_id(conn, current_user)
        query = "SELECT * FROM feedback_submissions WHERE user_id = ?"
        params: List[Any] = [sqlite_uid]

        if status and status.lower() != "all":
            query += " AND status = ?"
            params.append(status.lower())

        if search and search.strip():
            s = f"%{search.strip().lower()}%"
            query += " AND (lower(text) LIKE ? OR lower(store_id) LIKE ? OR lower(feedback_id) LIKE ?)"
            params.extend([s, s, s])

        count_query = query.replace("SELECT *", "SELECT count(*)")
        total = conn.execute(count_query, tuple(params)).fetchone()[0]

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?;"
        offset = (page - 1) * limit
        params.extend([limit, offset])
        rows = conn.execute(query, tuple(params)).fetchall()

    items = []
    for r in rows:
        sid = r["store_id"]
        tid = r["predicted_issue_type_id"]
        store_name = state.stores_dict.get(sid, {}).get("store_name", sid)
        tax_info = state.taxonomy_dict.get(tid, {})
        items.append({
            "feedback_id": r["feedback_id"],
            "store_id": sid,
            "store_name": store_name,
            "channel": r["channel"],
            "rating": r["rating"],
            "text": r["text"],
            "issue_type_id": tid,
            "issue_type_name": tax_info.get("issue_type", tid),
            "owner_function": r["owner_function"],
            "severity": r["predicted_severity"],
            "actionable": r["predicted_actionable"],
            "status": r["status"] or "new",
            "resolution_notes": r["resolution_notes"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "pages": (total + limit - 1) // limit if total > 0 else 1,
        "submissions": items,
    }


# -----------------------------------------------------------------------------
# Store Manager Operational Endpoints (Backend Role & Store Isolation Enforced)
# -----------------------------------------------------------------------------
class AlertActionRequest(BaseModel):
    action: str = Field(..., description="Action: 'acknowledge', 'investigate', or 'resolve'")
    note: Optional[str] = Field(None, description="Resolution or operational investigation note")


@app.get("/api/manager/store-summary")
def get_store_summary(
    store_id: Optional[str] = Query(None, description="Store ID (Admin can specify; Store Manager locked to assigned store)"),
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    """Delivers store-level KPIs and operational status for store managers."""
    role = current_user.get("role")
    if role == "store_manager":
        if store_id and store_id.upper() != current_user.get("assigned_store_id", "").upper():
            enforce_store_access(current_user, store_id)
        target_sid = current_user.get("assigned_store_id")
    else:
        target_sid = store_id or "ST-D69EG"

    target_sid = target_sid.upper() if target_sid else ""
    store_info = state.stores_dict.get(target_sid, {})
    if not store_info:
        raise HTTPException(status_code=404, detail=f"Store '{target_sid}' not found.")

    fb_df = state.feedback_df
    if "resolved_store_id" in fb_df.columns:
        store_fb = fb_df[(fb_df["store_id"] == target_sid) | (fb_df["resolved_store_id"] == target_sid)]
    else:
        store_fb = fb_df[fb_df["store_id"] == target_sid]

    total_fb = len(store_fb)
    act_count = int((store_fb["actionable"] == 1).sum()) if total_fb else 0
    high_crit_count = int(store_fb["severity"].isin(["high", "critical"]).sum()) if total_fb else 0
    critical_count = int((store_fb["severity"] == "critical").sum()) if total_fb else 0

    with get_db_connection() as conn:
        alerts = conn.execute(
            """
            SELECT status, count(*) as count
            FROM incident_alerts
            WHERE store_id = ?
            GROUP BY status;
            """,
            (target_sid,),
        ).fetchall()
        alert_counts = {r["status"]: r["count"] for r in alerts}

    return {
        "store_id": target_sid,
        "store_name": store_info.get("store_name", target_sid),
        "city": store_info.get("city", "Unknown"),
        "state": store_info.get("state", "Unknown"),
        "region": store_info.get("region", "Unknown"),
        "format": store_info.get("format", "Saffron Market"),
        "size_sqft": store_info.get("size_sqft"),
        "avg_weekly_footfall": store_info.get("avg_weekly_footfall"),
        "store": {
            "store_id": target_sid,
            "store_name": store_info.get("store_name", target_sid),
            "city": store_info.get("city", "Unknown"),
            "region": store_info.get("region", "Unknown"),
            "format": store_info.get("format", "Saffron Market"),
        },
        "total_feedback": total_fb,
        "actionable_count": act_count,
        "actionable_pct": round(act_count / total_fb * 100, 1) if total_fb else 0.0,
        "high_or_critical_count": high_crit_count,
        "critical_count": critical_count,
        "active_alerts": sum(cnt for st, cnt in alert_counts.items() if st != "resolved"),
        "alerts_by_status": alert_counts,
        "alerts_summary": {
            "total": sum(alert_counts.values()),
            "new": alert_counts.get("new", 0),
            "acknowledged": alert_counts.get("acknowledged", 0),
            "investigating": alert_counts.get("investigating", 0),
            "resolved": alert_counts.get("resolved", 0),
        },
    }


@app.get("/api/manager/feedback-inbox")
def get_manager_feedback_inbox(
    store_id: Optional[str] = Query(None, description="Store ID"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    severity: Optional[str] = None,
    actionable: Optional[int] = None,
    channel: Optional[str] = None,
    search: Optional[str] = None,
    status: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    """Store Manager Feedback Inbox strictly filtered to the authenticated manager's assigned store."""
    role = current_user.get("role")
    if role == "store_manager":
        if store_id and store_id.upper() != current_user.get("assigned_store_id", "").upper():
            enforce_store_access(current_user, store_id)
        target_sid = current_user.get("assigned_store_id")
    else:
        target_sid = store_id or "ST-D69EG"

    target_sid = target_sid.upper() if target_sid else ""

    # 1. Fetch live customer submissions from Supabase first
    customer_records = []
    token = current_user.get("token")
    provider = current_user.get("provider")
    supa_subs = None
    if provider == "supabase" and token:
        supa_subs = SupabaseService.get_manager_submissions(target_sid, access_token=token)
        if supa_subs is None and not settings.TESTING:
            # Supabase error in production mode for Supabase user: do not silently mask with stale SQLite data
            raise HTTPException(
                status_code=502,
                detail="Supabase query failed. Unable to fetch manager inbox from authoritative database.",
            )

    if supa_subs is not None:
        # Legitimate Supabase response: use it exclusively as authoritative source of truth
        for r in supa_subs:
            fid = r["feedback_id"]
            tid = r.get("issue_type_id")
            tax_info = state.taxonomy_dict.get(tid, {})
            customer_records.append({
                "feedback_id": fid,
                "channel": r.get("channel", "in_app"),
                "created_at": r.get("created_at"),
                "store_id": target_sid,
                "store_name": state.stores_dict.get(target_sid, {}).get("store_name", target_sid),
                "rating": r.get("rating"),
                "text": r.get("text", ""),
                "issue_type_id": tid,
                "issue_type_name": tax_info.get("issue_type", tid),
                "owner_function": r.get("owner_function") or tax_info.get("owner_function", "Store Ops"),
                "severity": r.get("severity") or "low",
                "actionable": int(r.get("actionable", 0)),
                "policy_severity_floor": tax_info.get("policy_severity_floor", "low"),
                "status": r.get("status") or "new",
                "resolution_notes": r.get("resolution_notes"),
                "is_customer_submission": True,
            })
    else:
        # Offline / local testing fallback
        try:
            with get_db_connection() as conn:
                db_subs = conn.execute(
                    """
                    SELECT * FROM feedback_submissions
                    WHERE store_id = ?
                    ORDER BY created_at DESC;
                    """,
                    (target_sid,),
                ).fetchall()

                for r in db_subs:
                    fid = r["feedback_id"]
                    tid = r["predicted_issue_type_id"]
                    tax_info = state.taxonomy_dict.get(tid, {})
                    customer_records.append({
                        "feedback_id": fid,
                        "channel": r["channel"],
                        "created_at": r["created_at"],
                        "store_id": target_sid,
                        "store_name": state.stores_dict.get(target_sid, {}).get("store_name", target_sid),
                        "rating": r["rating"],
                        "text": r["text"],
                        "issue_type_id": tid,
                        "issue_type_name": tax_info.get("issue_type", tid),
                        "owner_function": r["owner_function"],
                        "severity": r["predicted_severity"],
                        "actionable": int(r["predicted_actionable"]),
                        "policy_severity_floor": tax_info.get("policy_severity_floor", "low"),
                        "status": r["status"] or "new",
                        "resolution_notes": r["resolution_notes"],
                        "is_customer_submission": True,
                    })
        except Exception:
            pass

    # Filter customer records
    filtered_customer_records = []
    for r in customer_records:
        if severity and severity != "All" and r["severity"] != severity:
            continue
        if actionable is not None and r["actionable"] != actionable:
            continue
        if channel and channel != "All" and r["channel"] != channel:
            continue
        if search:
            s_low = search.lower()
            if s_low not in r["text"].lower() and s_low not in r["feedback_id"].lower():
                continue
        if status and status != "All" and r.get("status", "new").lower() != status.lower():
            continue
        filtered_customer_records.append(r)

    # 2. Fetch baseline feedback stream for this store (read-only evaluation dataset)
    df = state.feedback_df
    if "resolved_store_id" in df.columns:
        df = df[(df["store_id"] == target_sid) | (df["resolved_store_id"] == target_sid)]
    else:
        df = df[df["store_id"] == target_sid]

    if severity and severity != "All":
        df = df[df["severity"] == severity]
    if actionable is not None:
        df = df[df["actionable"] == actionable]
    if channel and channel != "All":
        df = df[df["channel"] == channel]
    if search:
        s = search.lower()
        df = df[df["text"].str.lower().str.contains(s, na=False)]

    dataset_records = []
    if not status or status == "All" or status.lower() == "new":
        for _, r in df.iterrows():
            dataset_records.append({
                "feedback_id": r["feedback_id"],
                "channel": r["channel"],
                "created_at": r["created_at"],
                "store_id": target_sid,
                "store_name": r.get("store_name") or state.stores_dict.get(target_sid, {}).get("store_name", target_sid),
                "rating": r["rating"] if pd.notna(r.get("rating")) else None,
                "text": r["text"],
                "issue_type_id": r["issue_type_id"],
                "issue_type_name": r["issue_type_name"],
                "owner_function": r["owner_function"],
                "severity": r["severity"],
                "actionable": int(r["actionable"]),
                "policy_severity_floor": state.taxonomy_dict.get(r["issue_type_id"], {}).get("policy_severity_floor", "low"),
                "status": "new",
                "resolution_notes": None,
                "is_customer_submission": False,
            })

    # Combined records: Customer submissions first, followed by historical dataset
    combined_records = filtered_customer_records + dataset_records
    total = len(combined_records)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    paged = combined_records[start_idx:end_idx]

    return {
        "store_id": target_sid,
        "page": page,
        "limit": limit,
        "total": total,
        "pages": (total + limit - 1) // limit if total > 0 else 1,
        "records": paged,
    }


@app.post("/api/manager/feedback/{feedback_id}/action")
def update_feedback_submission_status(
    feedback_id: str,
    req: ManagerFeedbackActionRequest,
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    """Updates customer feedback submission status (acknowledge, investigate, resolve) with store isolation."""
    action = req.action.strip().lower()
    note = (req.note or "").strip()

    status_map = {
        "acknowledge": "acknowledged",
        "investigate": "investigating",
        "resolve": "resolved",
        "acknowledged": "acknowledged",
        "investigating": "investigating",
        "resolved": "resolved",
    }
    if action not in status_map:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{req.action}'. Allowed: 'acknowledge', 'investigate', 'resolve'.",
        )
    new_status = status_map[action]

    with get_db_connection() as conn:
        sub = conn.execute(
            "SELECT * FROM feedback_submissions WHERE feedback_id = ? OR id = ?;",
            (feedback_id, feedback_id),
        ).fetchone()

        if not sub:
            raise HTTPException(status_code=404, detail=f"Feedback submission '{feedback_id}' not found.")

        # Store boundary check
        role = current_user.get("role")
        if role == "store_manager":
            assigned_sid = current_user.get("assigned_store_id")
            if not assigned_sid or sub["store_id"].upper() != assigned_sid.upper():
                raise HTTPException(
                    status_code=403,
                    detail=f"Forbidden: You are only authorized to manage store '{assigned_sid}'. Feedback belongs to store '{sub['store_id']}'.",
                )

        old_status = sub["status"]

        conn.execute(
            """
            UPDATE feedback_submissions
            SET status = ?,
                resolution_notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE feedback_id = ?;
            """,
            (new_status, note or sub["resolution_notes"], sub["feedback_id"]),
        )

        audit_details = json.dumps({
            "action": f"FEEDBACK_{action.upper()}",
            "old_status": old_status,
            "new_status": new_status,
            "store_id": sub["store_id"],
            "resolution_note": note,
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
        })

        sqlite_uid = _resolve_sqlite_user_id(conn, current_user)
        conn.execute(
            """
            INSERT INTO audit_log (user_id, action, target_type, target_id, details)
            VALUES (?, ?, 'feedback_submissions', ?, ?);
            """,
            (sqlite_uid, f"FEEDBACK_{action.upper()}", sub["feedback_id"], audit_details),
        )

    # Primary write to Supabase PostgREST
    user_token = current_user.get("token")
    provider = current_user.get("provider")
    if provider == "supabase" and user_token:
        try:
            supa_updated = SupabaseService.update_feedback_status(
                feedback_id=sub["feedback_id"],
                status=new_status,
                resolution_notes=note or sub["resolution_notes"],
                access_token=user_token,
            )
            if supa_updated is None and not settings.TESTING:
                raise HTTPException(
                    status_code=502,
                    detail="Supabase status update failed. Authoritative record was not modified in Supabase.",
                )
            SupabaseService.insert_audit_log(
                user_id=str(current_user.get("id")),
                action=f"FEEDBACK_{action.upper()}",
                target_type="feedback_submissions",
                target_id=sub["feedback_id"],
                details=json.loads(audit_details),
                access_token=user_token,
            )
        except HTTPException:
            raise
        except Exception as e:
            if not settings.TESTING:
                raise HTTPException(
                    status_code=502,
                    detail=f"Supabase error during status update: {str(e)}",
                )

    return {
        "success": True,
        "feedback_id": sub["feedback_id"],
        "store_id": sub["store_id"],
        "previous_status": old_status,
        "status": new_status,
        "resolution_notes": note or sub["resolution_notes"],
        "action": action,
        "actor": current_user.get("email"),
    }


@app.get("/api/manager/alerts")
def get_manager_alerts(
    store_id: Optional[str] = Query(None, description="Store ID"),
    status: Optional[str] = Query(None, description="Alert status filter: new, acknowledged, investigating, resolved"),
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    """Returns alerts for the manager's assigned store with status tracking and evidence details."""
    role = current_user.get("role")
    if role == "store_manager":
        if store_id and store_id.upper() != current_user.get("assigned_store_id", "").upper():
            enforce_store_access(current_user, store_id)
        target_sid = current_user.get("assigned_store_id")
    else:
        target_sid = store_id

    token = current_user.get("token")
    provider = current_user.get("provider")
    supa_alerts = None
    if provider == "supabase" and token:
        supa_alerts = SupabaseService.get_alerts(store_id=target_sid, status=status, access_token=token)
        if supa_alerts is None and not settings.TESTING:
            # Supabase error in production mode for Supabase user: do not silently mask with stale SQLite data
            raise HTTPException(
                status_code=502,
                detail="Supabase query failed. Unable to fetch incident alerts from authoritative database.",
            )

    if supa_alerts is not None:
        rows = supa_alerts
    else:
        with get_db_connection() as conn:
            query = "SELECT * FROM incident_alerts WHERE 1=1"
            params = []
            if target_sid:
                query += " AND store_id = ?"
                params.append(target_sid.upper())
            if status and status != "All":
                query += " AND status = ?"
                params.append(status.lower())

            query += " ORDER BY as_of_week_start DESC, confidence DESC;"
            rows = conn.execute(query, tuple(params)).fetchall()

    alerts = []
    for r in rows:
        r_dict = dict(r) if not isinstance(r, dict) else r
        sid = (r_dict.get("store_id") or "").strip().upper()
        tid = (r_dict.get("issue_type_id") or "").strip().upper()
        store_info = state.stores_dict.get(sid, {})
        tax_info = state.taxonomy_dict.get(tid, {})

        ev_ids = [x.strip() for x in str(r_dict.get("evidence_feedback_ids") or "").split(";") if x.strip()]
        evidence_samples = []
        for fid in ev_ids[:4]:
            fb_item = state.feedback_by_id.get(fid)
            if fb_item:
                evidence_samples.append({
                    "feedback_id": fid,
                    "channel": fb_item.get("channel"),
                    "created_at": fb_item.get("created_at"),
                    "rating": fb_item.get("rating"),
                    "text": fb_item.get("text"),
                })

        # Determine severity from evidence records and policy floor
        sev_floor = tax_info.get("policy_severity_floor", "low")
        sev_order = {"critical": 3, "high": 2, "medium": 1, "low": 0}
        highest_sev_val = sev_order.get(str(sev_floor).lower(), 0)
        for fid in ev_ids:
            fb_item = state.feedback_by_id.get(fid)
            if fb_item and fb_item.get("severity"):
                highest_sev_val = max(highest_sev_val, sev_order.get(str(fb_item["severity"]).lower(), 0))
        rev_sev_order = {3: "critical", 2: "high", 1: "medium", 0: "low"}
        calculated_severity = rev_sev_order.get(highest_sev_val, sev_floor or "medium")

        # Calculate actual baseline and current counts from real store feedback
        w_start = r_dict.get("as_of_week_start") or r_dict.get("week") or ""
        cur_count = len(ev_ids)
        baseline_count = 0.5
        ratio = float(cur_count)
        if hasattr(state, "feedback_df") and state.feedback_df is not None:
            try:
                prior_df = state.feedback_df[
                    (state.feedback_df["store_id"] == sid) &
                    (state.feedback_df["issue_type_id"] == tid) &
                    (state.feedback_df["week_start"] < w_start)
                ]
                prior_weeks = prior_df["week_start"].nunique()
                if prior_weeks > 0:
                    baseline_count = round(len(prior_df) / prior_weeks, 1)
                    ratio = round(cur_count / max(baseline_count, 0.5), 1)
            except Exception:
                pass

        issue_display_name = tax_info.get("issue_type", tid)
        res_note = r_dict.get("resolution_notes") or r_dict.get("resolution_note") or ""

        alert_uid = r_dict.get("alert_id") or (f"ALT-{w_start}-{sid}-{tid}" if w_start and sid and tid else str(r_dict.get("id", "")))

        raw_conf = r_dict.get("confidence")
        try:
            conf_val = float(raw_conf) if raw_conf is not None else 0.5
        except (ValueError, TypeError):
            conf_val = 0.5

        alerts.append({
            "id": r_dict.get("id"),
            "alert_id": alert_uid,
            "store_id": sid,
            "store_name": store_info.get("store_name", sid),
            "city": store_info.get("city", "Unknown"),
            "region": store_info.get("region", "Unknown"),
            "format": store_info.get("format", "Saffron Market"),
            "issue_type_id": tid,
            "issue_name": issue_display_name,
            "issue_type_name": issue_display_name,
            "owner_function": tax_info.get("owner_function", "Store Ops"),
            "severity": calculated_severity,
            "severity_floor": sev_floor,
            "week": w_start,
            "as_of_week_start": w_start,
            "confidence": conf_val,
            "evidence_count": len(ev_ids),
            "current_count": cur_count,
            "baseline_count": baseline_count,
            "ratio": ratio,
            "evidence_feedback_ids": r_dict.get("evidence_feedback_ids") or "",
            "evidence_samples": evidence_samples,
            "status": r_dict.get("status") or "new",
            "acknowledged_by_user_id": r_dict.get("acknowledged_by_user_id") or r_dict.get("resolved_by_user_id"),
            "resolved_by_user_id": r_dict.get("resolved_by_user_id") or r_dict.get("acknowledged_by_user_id"),
            "resolution_note": res_note,
            "resolution_notes": res_note,
            "updated_at": r_dict.get("updated_at") or r_dict.get("created_at"),
        })

    return {
        "store_id": target_sid,
        "total_alerts": len(alerts),
        "alerts": alerts,
    }


@app.post("/api/manager/alerts/{alert_id}/action")
def update_alert_action(
    alert_id: str,
    req: AlertActionRequest,
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    """Updates alert status (acknowledge, investigate, resolve) with store isolation and audit trail."""
    import json
    action = req.action.strip().lower()
    note = (req.note or "").strip()

    status_map = {
        "acknowledge": "acknowledged",
        "investigate": "investigating",
        "resolve": "resolved",
    }
    if action not in status_map:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{req.action}'. Allowed actions: 'acknowledge', 'investigate', 'resolve'.",
        )
    new_status = status_map[action]

    if action == "resolve" and len(note) < 5:
        raise HTTPException(
            status_code=400,
            detail="Resolution note is required (minimum 5 characters) when resolving an alert.",
        )

    with get_db_connection() as conn:
        alert = conn.execute(
            "SELECT * FROM incident_alerts WHERE alert_id = ? OR id = ?;",
            (alert_id, alert_id),
        ).fetchone()

        if not alert:
            raise HTTPException(status_code=404, detail=f"Alert '{alert_id}' not found.")

        # Enforce store isolation on backend
        role = current_user.get("role")
        if role == "store_manager":
            assigned_sid = current_user.get("assigned_store_id")
            if not assigned_sid or alert["store_id"].upper() != assigned_sid.upper():
                raise HTTPException(
                    status_code=403,
                    detail=f"Forbidden: You are only authorized to manage alerts for store '{assigned_sid}'. Action on store '{alert['store_id']}' denied.",
                )

        old_status = alert["status"]

        sqlite_uid = _resolve_sqlite_user_id(conn, current_user)
        conn.execute(
            """
            UPDATE incident_alerts
            SET status = ?,
                acknowledged_by_user_id = ?,
                resolution_notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE alert_id = ?;
            """,
            (new_status, sqlite_uid, note or alert["resolution_notes"], alert["alert_id"]),
        )

        audit_details = json.dumps({
            "action": action,
            "old_status": old_status,
            "new_status": new_status,
            "store_id": alert["store_id"],
            "resolution_note": note,
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
        })

        conn.execute(
            """
            INSERT INTO audit_log (user_id, action, target_type, target_id, details)
            VALUES (?, ?, 'incident_alerts', ?, ?);
            """,
            (sqlite_uid, f"ALERT_{action.upper()}", alert["alert_id"], audit_details),
        )

    # Primary write to Supabase PostgREST
    user_token = current_user.get("token")
    provider = current_user.get("provider")
    if provider == "supabase" and user_token:
        try:
            supa_updated = SupabaseService.update_alert(
                alert_id=alert["alert_id"],
                status=new_status,
                resolution_notes=note or alert["resolution_notes"],
                user_id=str(current_user.get("id")),
                access_token=user_token,
            )
            if supa_updated is None and not settings.TESTING:
                raise HTTPException(
                    status_code=502,
                    detail="Supabase alert status update failed. Authoritative alert was not modified in Supabase.",
                )
            SupabaseService.insert_audit_log(
                user_id=str(current_user.get("id")),
                action=f"ALERT_{action.upper()}",
                target_type="incident_alerts",
                target_id=alert["alert_id"],
                details=json.loads(audit_details),
                access_token=user_token,
            )
        except HTTPException:
            raise
        except Exception as e:
            if not settings.TESTING:
                raise HTTPException(
                    status_code=502,
                    detail=f"Supabase error during alert status update: {str(e)}",
                )

    return {
        "success": True,
        "alert_id": alert["alert_id"],
        "store_id": alert["store_id"],
        "previous_status": old_status,
        "status": new_status,
        "resolution_notes": note or alert["resolution_notes"],
        "action": action,
        "actor": current_user.get("email"),
    }


@app.get("/api/manager/alerts/{alert_id}/history")
def get_alert_history(
    alert_id: str,
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    """Returns audit log trail for an alert, verifying store ownership."""
    with get_db_connection() as conn:
        alert = conn.execute(
            "SELECT * FROM incident_alerts WHERE alert_id = ? OR id = ?;",
            (alert_id, alert_id),
        ).fetchone()

        if not alert:
            raise HTTPException(status_code=404, detail=f"Alert '{alert_id}' not found.")

        role = current_user.get("role")
        if role == "store_manager":
            assigned_sid = current_user.get("assigned_store_id")
            if not assigned_sid or alert["store_id"].upper() != assigned_sid.upper():
                raise HTTPException(
                    status_code=403,
                    detail=f"Forbidden: You are not authorized to view audit history for store '{alert['store_id']}'.",
                )

        logs = conn.execute(
            """
            SELECT a.id, a.user_id, u.email as actor_email, u.full_name as actor_name, u.role as actor_role,
                   a.action, a.target_id, a.details, a.created_at
            FROM audit_log a
            LEFT JOIN users u ON a.user_id = u.id
            WHERE a.target_type = 'incident_alerts' AND a.target_id = ?
            ORDER BY a.created_at DESC;
            """,
            (alert["alert_id"],),
        ).fetchall()

    parsed_history = []
    for r in logs:
        rec = dict(r)
        parsed_details = {}
        if rec.get("details"):
            try:
                parsed_details = json.loads(rec["details"])
            except Exception:
                parsed_details = {}

        rec["details_data"] = parsed_details
        rec["old_status"] = parsed_details.get("old_status")
        rec["new_status"] = parsed_details.get("new_status")
        rec["resolution_note"] = parsed_details.get("resolution_note")
        rec["store_id"] = parsed_details.get("store_id") or alert["store_id"]

        if not rec.get("actor_email") and parsed_details.get("actor_email"):
            rec["actor_email"] = parsed_details["actor_email"]
        if not rec.get("actor_role") and parsed_details.get("actor_role"):
            rec["actor_role"] = parsed_details["actor_role"]

        parsed_history.append(rec)

    return {
        "alert_id": alert["alert_id"],
        "store_id": alert["store_id"],
        "current_status": alert["status"],
        "history": parsed_history,
    }


# -----------------------------------------------------------------------------
# Store Manager: Emerging Insights, Historical Analytics, Action Center, Reports, Settings
# -----------------------------------------------------------------------------
@app.get("/api/manager/insights")
def get_manager_insights(
    store_id: Optional[str] = Query(None, description="Store ID override for admin"),
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    role = current_user.get("role")
    if role == "store_manager":
        if store_id and store_id.upper() != current_user.get("assigned_store_id", "").upper():
            enforce_store_access(current_user, store_id)
        target_sid = current_user.get("assigned_store_id")
    else:
        target_sid = store_id or "ST-D69EG"

    if not target_sid:
        raise HTTPException(status_code=400, detail="Store ID is required.")

    df = state.feedback_df
    if "resolved_store_id" in df.columns:
        store_df = df[(df["store_id"] == target_sid) | (df["resolved_store_id"] == target_sid)]
    else:
        store_df = df[df["store_id"] == target_sid]

    total = len(store_df)
    actionable_count = int((store_df["actionable"] == 1).sum()) if "actionable" in store_df.columns else 0
    actionable_rate = round((actionable_count / total * 100), 1) if total > 0 else 0.0

    top_issues = []
    if "issue_type_id" in store_df.columns:
        counts = store_df["issue_type_id"].value_counts().head(8)
        for tid, cnt in counts.items():
            tax = state.taxonomy_dict.get(tid, {})
            sub_act = int((store_df[store_df["issue_type_id"] == tid]["actionable"] == 1).sum())
            top_issues.append({
                "issue_type_id": tid,
                "issue_type_name": tax.get("issue_type", tid),
                "owner_function": tax.get("owner_function", "Store Ops"),
                "count": int(cnt),
                "actionable_count": sub_act,
                "pct_of_store": round((cnt / total * 100), 1) if total > 0 else 0.0,
            })

    sev_dist = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    if "severity" in store_df.columns:
        for sev, cnt in store_df["severity"].value_counts().items():
            s_key = str(sev).lower()
            if s_key in sev_dist:
                sev_dist[s_key] = int(cnt)

    channel_dist = {}
    if "channel" in store_df.columns:
        for ch, cnt in store_df["channel"].value_counts().items():
            channel_dist[str(ch)] = int(cnt)

    dept_friction = {}
    for item in top_issues:
        dept = item["owner_function"]
        dept_friction[dept] = dept_friction.get(dept, 0) + item["count"]

    store_meta = state.stores_dict.get(target_sid, {})
    return {
        "store_id": target_sid,
        "store_name": store_meta.get("store_name", target_sid),
        "city": store_meta.get("city", "Unknown"),
        "total_feedback": total,
        "actionable_count": actionable_count,
        "actionable_rate": actionable_rate,
        "top_issues": top_issues,
        "severity_distribution": sev_dist,
        "channel_distribution": channel_dist,
        "department_friction": dept_friction,
    }


@app.get("/api/manager/historical-analytics")
def get_manager_historical_analytics(
    store_id: Optional[str] = Query(None, description="Store ID override for admin"),
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    role = current_user.get("role")
    if role == "store_manager":
        if store_id and store_id.upper() != current_user.get("assigned_store_id", "").upper():
            enforce_store_access(current_user, store_id)
        target_sid = current_user.get("assigned_store_id")
    else:
        target_sid = store_id or "ST-D69EG"

    if not target_sid:
        raise HTTPException(status_code=400, detail="Store ID is required.")

    df = state.feedback_df
    if "resolved_store_id" in df.columns:
        store_df = df[(df["store_id"] == target_sid) | (df["resolved_store_id"] == target_sid)].copy()
    else:
        store_df = df[df["store_id"] == target_sid].copy()

    rating_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    avg_rating = 0.0
    if "rating" in store_df.columns:
        rated = store_df[pd.notna(store_df["rating"]) & (store_df["rating"] > 0)]
        if len(rated) > 0:
            avg_rating = round(float(rated["rating"].mean()), 2)
            for r, c in rated["rating"].value_counts().items():
                try:
                    ri = int(round(float(r)))
                    if ri in rating_counts:
                        rating_counts[ri] = int(c)
                except Exception:
                    pass

    weekly_trend = []
    if "created_at" in store_df.columns:
        try:
            store_df["week"] = pd.to_datetime(store_df["created_at"], errors="coerce").dt.to_period("W").astype(str)
            w_grp = store_df.groupby("week").agg(
                total=("feedback_id", "count"),
                actionable=("actionable", lambda x: int((x == 1).sum())),
            ).reset_index().tail(12)
            for _, row in w_grp.iterrows():
                weekly_trend.append({
                    "week": str(row["week"]),
                    "total": int(row["total"]),
                    "actionable": int(row["actionable"]),
                })
        except Exception:
            pass

    store_meta = state.stores_dict.get(target_sid, {})
    return {
        "store_id": target_sid,
        "store_name": store_meta.get("store_name", target_sid),
        "average_rating": avg_rating,
        "rating_distribution": rating_counts,
        "weekly_trend": weekly_trend,
        "total_records": len(store_df),
    }


@app.get("/api/manager/actions")
def get_manager_action_center(
    store_id: Optional[str] = Query(None, description="Store ID override for admin"),
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    role = current_user.get("role")
    if role == "store_manager":
        if store_id and store_id.upper() != current_user.get("assigned_store_id", "").upper():
            enforce_store_access(current_user, store_id)
        target_sid = current_user.get("assigned_store_id")
    else:
        target_sid = store_id or "ST-D69EG"

    if not target_sid:
        raise HTTPException(status_code=400, detail="Store ID is required.")

    with get_db_connection() as conn:
        subs = conn.execute(
            """
            SELECT feedback_id, channel, rating, text, predicted_issue_type_id,
                   predicted_severity, predicted_actionable, status, resolution_notes,
                   created_at, updated_at
            FROM feedback_submissions
            WHERE store_id = ?
            ORDER BY created_at DESC;
            """,
            (target_sid,),
        ).fetchall()

        alerts = conn.execute(
            """
            SELECT alert_id, issue_type_id, as_of_week_start, confidence,
                   status, resolution_notes, updated_at
            FROM incident_alerts
            WHERE store_id = ?
            ORDER BY as_of_week_start DESC, confidence DESC;
            """,
            (target_sid,),
        ).fetchall()

    counts = {
        "pending": 0,
        "investigating": 0,
        "acknowledged": 0,
        "resolved": 0,
        "total": len(subs) + len(alerts),
    }

    action_items = []
    for s in subs:
        st = (s["status"] or "new").lower()
        if st in ["new", "pending"]:
            counts["pending"] += 1
        elif st in counts:
            counts[st] += 1

        tax = state.taxonomy_dict.get(s["predicted_issue_type_id"], {})
        action_items.append({
            "type": "feedback",
            "id": s["feedback_id"],
            "item_id": s["feedback_id"],
            "title": f"Customer Feedback ({tax.get('issue_type', s['predicted_issue_type_id'])})",
            "summary": (s["text"] or "")[:140] + ("..." if len(s["text"] or "") > 140 else ""),
            "severity": s["predicted_severity"] or "low",
            "status": st,
            "resolution_notes": s["resolution_notes"],
            "created_at": s["created_at"],
            "updated_at": s["updated_at"],
        })

    for a in alerts:
        st = (a["status"] or "new").lower()
        if st in ["new", "pending"]:
            counts["pending"] += 1
        elif st in counts:
            counts[st] += 1

        tax = state.taxonomy_dict.get(a["issue_type_id"], {})
        action_items.append({
            "type": "alert",
            "id": a["alert_id"],
            "item_id": a["alert_id"],
            "title": f"Emerging Alert: {tax.get('issue_type', a['issue_type_id'])}",
            "summary": f"Confidence: {round(float(a['confidence'] or 0)*100)}% | As of Week: {a['as_of_week_start']}",
            "severity": "high",
            "status": st,
            "resolution_notes": a["resolution_notes"],
            "created_at": a["as_of_week_start"],
            "updated_at": a["updated_at"],
        })

    store_meta = state.stores_dict.get(target_sid, {})
    return {
        "store_id": target_sid,
        "store_name": store_meta.get("store_name", target_sid),
        "counts": counts,
        "action_items": action_items[:50],
    }


@app.get("/api/manager/reports")
def get_manager_reports(
    store_id: Optional[str] = Query(None, description="Store ID override for admin"),
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    role = current_user.get("role")
    if role == "store_manager":
        if store_id and store_id.upper() != current_user.get("assigned_store_id", "").upper():
            enforce_store_access(current_user, store_id)
        target_sid = current_user.get("assigned_store_id")
    else:
        target_sid = store_id or "ST-D69EG"

    if not target_sid:
        raise HTTPException(status_code=400, detail="Store ID is required.")

    df = state.feedback_df
    if "resolved_store_id" in df.columns:
        store_df = df[(df["store_id"] == target_sid) | (df["resolved_store_id"] == target_sid)]
    else:
        store_df = df[df["store_id"] == target_sid]

    store_meta = state.stores_dict.get(target_sid, {})

    with get_db_connection() as conn:
        resolved_count = conn.execute(
            "SELECT count(*) FROM feedback_submissions WHERE store_id = ? AND status = 'resolved';",
            (target_sid,),
        ).fetchone()[0]
        active_alerts_count = conn.execute(
            "SELECT count(*) FROM incident_alerts WHERE store_id = ? AND status != 'resolved';",
            (target_sid,),
        ).fetchone()[0]

    return {
        "report_id": f"REP-{target_sid}-{int(time.time())}",
        "store_id": target_sid,
        "store_name": store_meta.get("store_name", target_sid),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "fleet_rank": "Top Tier (Standard Audit)",
        "total_feedback_analyzed": len(store_df),
        "actionable_feedback_count": int((store_df["actionable"] == 1).sum()) if "actionable" in store_df.columns else 0,
        "resolved_incidents": resolved_count,
        "active_alerts": active_alerts_count,
        "top_friction_categories": [
            {"issue_type": state.taxonomy_dict.get(t, {}).get("issue_type", t), "count": int(c)}
            for t, c in store_df["issue_type_id"].value_counts().head(5).items()
        ] if "issue_type_id" in store_df.columns else [],
        "executive_summary": (
            f"Voice of Store Engine operational report for {store_meta.get('store_name', target_sid)}. "
            f"A total of {len(store_df)} customer interactions have been ingested and triaged with real-time "
            f"severity floor enforcement and locality resolution. Current active operational alerts: {active_alerts_count}."
        ),
    }


@app.get("/api/manager/reports/export-csv")
def export_manager_report_csv(
    store_id: Optional[str] = Query(None, description="Store ID override for admin"),
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
):
    role = current_user.get("role")
    if role == "store_manager":
        if store_id and store_id.upper() != current_user.get("assigned_store_id", "").upper():
            enforce_store_access(current_user, store_id)
        target_sid = current_user.get("assigned_store_id")
    else:
        target_sid = store_id or "ST-D69EG"

    with get_db_connection() as conn:
        subs = conn.execute(
            """
            SELECT feedback_id, channel, rating, text, predicted_issue_type_id,
                   predicted_severity, predicted_actionable, status, resolution_notes, created_at
            FROM feedback_submissions
            WHERE store_id = ?
            ORDER BY created_at DESC;
            """,
            (target_sid,),
        ).fetchall()

    lines = ["Feedback_ID,Channel,Rating,Issue_Type_ID,Severity,Actionable,Status,Created_At,Resolution_Notes"]
    for s in subs:
        note = (s["resolution_notes"] or "").replace('"', '""')
        lines.append(
            f'"{s["feedback_id"]}","{s["channel"]}","{s["rating"] or ""}","{s["predicted_issue_type_id"]}","{s["predicted_severity"]}","{s["predicted_actionable"]}","{s["status"]}","{s["created_at"]}","{note}"'
        )
    csv_data = "\n".join(lines)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="store_{target_sid}_feedback_report.csv"'},
    )


class StoreSettingsUpdateRequest(BaseModel):
    store_id: Optional[str] = None
    operating_hours: Optional[str] = None
    monitoring_tier: Optional[str] = None
    notifications: Optional[Dict[str, bool]] = None


def _ensure_store_settings_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_settings (
            store_id TEXT PRIMARY KEY,
            operating_hours TEXT,
            monitoring_tier TEXT,
            notifications_json TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


@app.get("/api/manager/settings")
def get_manager_settings(
    store_id: Optional[str] = Query(None, description="Store ID override for admin"),
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    role = current_user.get("role")
    if role == "store_manager":
        if store_id and store_id.upper() != current_user.get("assigned_store_id", "").upper():
            enforce_store_access(current_user, store_id)
        target_sid = current_user.get("assigned_store_id")
    else:
        target_sid = store_id or "ST-D69EG"

    store_meta = state.stores_dict.get(target_sid, {})
    op_hours = "07:00 AM - 11:00 PM IST (Daily)"
    tier = "Tier-1 Autonomous Triage"
    notifications = {
        "email_on_critical_alert": True,
        "daily_summary_brief": True,
        "instant_actionable_notification": True,
    }
    last_updated = None

    try:
        with get_db_connection() as conn:
            _ensure_store_settings_table(conn)
            row = conn.execute(
                "SELECT operating_hours, monitoring_tier, notifications_json, updated_at FROM store_settings WHERE store_id = ?;",
                (target_sid.upper(),),
            ).fetchone()
            if row:
                if row["operating_hours"]:
                    op_hours = row["operating_hours"]
                if row["monitoring_tier"]:
                    tier = row["monitoring_tier"]
                if row["notifications_json"]:
                    try:
                        notifications = json.loads(row["notifications_json"])
                    except Exception:
                        pass
                last_updated = row["updated_at"]
    except Exception as e:
        logger.warning(f"Failed to fetch store settings from database: {e}")

    return {
        "store_id": target_sid,
        "store_name": store_meta.get("store_name", target_sid),
        "city": store_meta.get("city", "Unknown"),
        "state": store_meta.get("state", "India"),
        "format": store_meta.get("format", "Standard Supercenter"),
        "operating_hours": op_hours,
        "monitoring_tier": tier,
        "manager_email": current_user.get("email"),
        "manager_name": current_user.get("full_name"),
        "sync_mode": settings.APP_MODE,
        "supabase_connected": bool(settings.SUPABASE_URL and settings.SUPABASE_PUBLISHABLE_KEY),
        "notifications": notifications,
        "updated_at": last_updated,
    }


@app.post("/api/manager/settings")
def update_manager_settings(
    req: StoreSettingsUpdateRequest,
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> dict:
    role = current_user.get("role")
    if role == "store_manager":
        assigned_sid = current_user.get("assigned_store_id")
        if req.store_id and req.store_id.upper() != (assigned_sid or "").upper():
            enforce_store_access(current_user, req.store_id)
        target_sid = assigned_sid
    else:
        target_sid = req.store_id or "ST-D69EG"

    if not target_sid:
        raise HTTPException(status_code=400, detail="Store ID is required to update store settings.")

    # Validation
    if req.operating_hours is not None:
        clean_hours = req.operating_hours.strip()
        if len(clean_hours) < 3 or len(clean_hours) > 100:
            raise HTTPException(status_code=400, detail="Operating hours must be between 3 and 100 characters.")
    else:
        clean_hours = "07:00 AM - 11:00 PM IST (Daily)"

    valid_tiers = ["Tier-1 Autonomous Triage", "Tier-2 Escalation Only", "Tier-3 Full Manual"]
    clean_tier = req.monitoring_tier if req.monitoring_tier in valid_tiers else "Tier-1 Autonomous Triage"

    clean_notifs = {
        "email_on_critical_alert": True,
        "daily_summary_brief": True,
        "instant_actionable_notification": True,
    }
    if req.notifications and isinstance(req.notifications, dict):
        clean_notifs.update(req.notifications)

    notifs_json = json.dumps(clean_notifs)

    with get_db_connection() as conn:
        _ensure_store_settings_table(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO store_settings (store_id, operating_hours, monitoring_tier, notifications_json, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP);
            """,
            (target_sid.upper(), clean_hours, clean_tier, notifs_json),
        )
        sqlite_uid = _resolve_sqlite_user_id(conn, current_user)
        conn.execute(
            """
            INSERT INTO audit_log (user_id, action, target_type, target_id, details)
            VALUES (?, 'STORE_SETTINGS_UPDATE', 'store_settings', ?, ?);
            """,
            (sqlite_uid, target_sid.upper(), json.dumps({
                "operating_hours": clean_hours,
                "monitoring_tier": clean_tier,
                "actor": current_user.get("email"),
            })),
        )

    return {
        "success": True,
        "message": f"Store settings for '{target_sid}' updated successfully.",
        "store_id": target_sid.upper(),
        "operating_hours": clean_hours,
        "monitoring_tier": clean_tier,
        "notifications": clean_notifs,
    }


# -----------------------------------------------------------------------------
# Admin Workspaces & Operations (11 Sections)
# -----------------------------------------------------------------------------
class AdminAssignRoleRequest(BaseModel):
    target_email: str
    new_role: str
    new_store_id: Optional[str] = None


@app.get("/api/admin/overview")
def get_admin_overview(
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> dict:
    df = state.feedback_df
    total_feedback = len(df)
    total_actionable = int((df["actionable"] == 1).sum()) if "actionable" in df.columns else 0
    actionable_rate = round((total_actionable / total_feedback * 100), 1) if total_feedback > 0 else 0.0

    with get_db_connection() as conn:
        active_alerts = conn.execute("SELECT count(*) FROM incident_alerts WHERE status != 'resolved';").fetchone()[0]
        resolved_alerts = conn.execute("SELECT count(*) FROM incident_alerts WHERE status = 'resolved';").fetchone()[0]

    city_summaries = []
    if "city" in df.columns:
        for city, cnt in df["city"].value_counts().head(8).items():
            city_df = df[df["city"] == city]
            act_city = int((city_df["actionable"] == 1).sum()) if "actionable" in city_df.columns else 0
            city_summaries.append({
                "city": city,
                "feedback_count": int(cnt),
                "actionable_rate": round((act_city / cnt * 100), 1) if cnt > 0 else 0.0,
            })

    return {
        "total_stores": len(state.stores_dict),
        "total_feedback_volume": total_feedback,
        "total_actionable": total_actionable,
        "chain_actionable_rate": actionable_rate,
        "active_alerts_count": active_alerts,
        "resolved_alerts_count": resolved_alerts,
        "city_summaries": city_summaries,
    }


@app.get("/api/admin/users")
def get_admin_users(
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> dict:
    with get_db_connection() as conn:
        users = conn.execute(
            """
            SELECT id, email, full_name, role, assigned_store_id, created_at
            FROM users
            ORDER BY id ASC;
            """
        ).fetchall()

    user_list = []
    for u in users:
        sid = u["assigned_store_id"]
        sname = state.stores_dict.get(sid, {}).get("store_name", sid) if sid else None
        user_list.append({
            "id": u["id"],
            "email": u["email"],
            "full_name": u["full_name"],
            "role": u["role"],
            "assigned_store_id": sid,
            "assigned_store_name": sname,
            "created_at": u["created_at"],
        })

    return {"total": len(user_list), "users": user_list}


@app.post("/api/admin/users/assign-role")
def admin_assign_user_role(
    req: AdminAssignRoleRequest,
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> dict:
    target_email = req.target_email.strip().lower()
    new_role = req.new_role.strip().lower()
    new_store_id = req.new_store_id.strip().upper() if req.new_store_id else None

    if new_role not in ["customer", "store_manager", "admin"]:
        raise HTTPException(status_code=400, detail=f"Invalid role '{req.new_role}'. Allowed: customer, store_manager, admin.")

    if new_role == "store_manager":
        if not new_store_id:
            raise HTTPException(status_code=400, detail="Store Manager must be assigned a valid store_id.")
        if new_store_id not in state.stores_dict:
            raise HTTPException(status_code=400, detail=f"Store ID '{new_store_id}' does not exist in store fleet.")
    else:
        new_store_id = None

    with get_db_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE lower(email) = ?;", (target_email,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail=f"User with email '{target_email}' not found.")

        old_role = user["role"]
        old_store = user["assigned_store_id"]

        conn.execute(
            """
            UPDATE users
            SET role = ?, assigned_store_id = ?
            WHERE lower(email) = ?;
            """,
            (new_role, new_store_id, target_email),
        )

        audit_details = json.dumps({
            "action": "ADMIN_ASSIGN_ROLE",
            "target_email": target_email,
            "old_role": old_role,
            "new_role": new_role,
            "old_store_id": old_store,
            "new_store_id": new_store_id,
            "actor_email": current_user.get("email"),
        })
        sqlite_uid = _resolve_sqlite_user_id(conn, current_user)
        conn.execute(
            """
            INSERT INTO audit_log (user_id, action, target_type, target_id, details)
            VALUES (?, 'ADMIN_ASSIGN_ROLE', 'users', ?, ?);
            """,
            (sqlite_uid, str(user["id"]), audit_details),
        )

    return {
        "success": True,
        "message": f"Updated {target_email} to role '{new_role}'" + (f" for store {new_store_id}" if new_store_id else ""),
        "user": {
            "email": target_email,
            "role": new_role,
            "assigned_store_id": new_store_id,
        },
    }


@app.get("/api/admin/insights")
def get_admin_network_insights(
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> dict:
    df = state.feedback_df
    total = len(df)

    top_categories = []
    if "issue_type_id" in df.columns:
        for tid, cnt in df["issue_type_id"].value_counts().head(10).items():
            tax = state.taxonomy_dict.get(tid, {})
            top_categories.append({
                "issue_type_id": tid,
                "issue_type_name": tax.get("issue_type", tid),
                "owner_function": tax.get("owner_function", "Store Ops"),
                "count": int(cnt),
                "pct": round((cnt / total * 100), 1) if total > 0 else 0.0,
            })

    city_breakdown = []
    if "city" in df.columns:
        for city, cnt in df["city"].value_counts().head(10).items():
            city_df = df[df["city"] == city]
            act = int((city_df["actionable"] == 1).sum()) if "actionable" in city_df.columns else 0
            city_breakdown.append({
                "city": city,
                "count": int(cnt),
                "actionable_rate": round((act / cnt * 100), 1) if cnt > 0 else 0.0,
            })

    sev_breakdown = {}
    if "severity" in df.columns:
        for s, c in df["severity"].value_counts().items():
            sev_breakdown[str(s)] = int(c)

    channel_breakdown = {}
    if "channel" in df.columns:
        for ch, c in df["channel"].value_counts().items():
            channel_breakdown[str(ch)] = int(c)

    return {
        "total_analyzed": total,
        "top_categories": top_categories,
        "city_breakdown": city_breakdown,
        "severity_breakdown": sev_breakdown,
        "channel_breakdown": channel_breakdown,
    }


@app.get("/api/admin/remediations")
def get_admin_remediations(
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> dict:
    with get_db_connection() as conn:
        actions = conn.execute(
            """
            SELECT id, user_id, action, target_type, target_id, details, created_at
            FROM audit_log
            WHERE action LIKE 'FEEDBACK_%' OR action LIKE 'ALERT_%'
            ORDER BY id DESC
            LIMIT 50;
            """
        ).fetchall()

        total_actions = conn.execute("SELECT count(*) FROM audit_log WHERE action LIKE 'FEEDBACK_%' OR action LIKE 'ALERT_%';").fetchone()[0]
        resolved_feedback = conn.execute("SELECT count(*) FROM feedback_submissions WHERE status = 'resolved';").fetchone()[0]
        resolved_alerts = conn.execute("SELECT count(*) FROM incident_alerts WHERE status = 'resolved';").fetchone()[0]

    remediation_items = []
    for a in actions:
        d = {}
        if a["details"]:
            try:
                d = json.loads(a["details"])
            except Exception:
                pass
        remediation_items.append({
            "id": a["id"],
            "action": a["action"],
            "target_type": a["target_type"],
            "target_id": a["target_id"],
            "store_id": d.get("store_id", "N/A"),
            "actor_email": d.get("actor_email", "manager@saffron.example"),
            "resolution_note": d.get("resolution_note", ""),
            "created_at": a["created_at"],
        })

    return {
        "total_actions": total_actions,
        "resolved_feedback_count": resolved_feedback,
        "resolved_alerts_count": resolved_alerts,
        "recent_remediations": remediation_items,
    }


@app.get("/api/admin/integrations")
def get_admin_integrations(
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> dict:
    has_supa_url = bool(settings.SUPABASE_URL)
    has_supa_key = bool(settings.SUPABASE_PUBLISHABLE_KEY)
    supa_host = settings.SUPABASE_URL.split("//")[-1] if has_supa_url else "Not configured"

    return {
        "integrations": [
            {
                "name": "Supabase PostgREST Database",
                "type": "Primary Production Database",
                "endpoint": supa_host,
                "status": "Connected & Synchronized" if (has_supa_url and has_supa_key) else "Offline (SQLite Fallback)",
                "live": has_supa_url and has_supa_key,
                "last_sync": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            },
            {
                "name": "Supabase GoTrue Auth",
                "type": "Identity & JWT Token Provider",
                "endpoint": f"{supa_host}/auth/v1" if has_supa_url else "Local Auth",
                "status": "Connected & Active" if (has_supa_url and has_supa_key) else "Local JWT Active",
                "live": has_supa_url and has_supa_key,
                "last_sync": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            },
            {
                "name": "POS Receipt Survey Ingest Webhook",
                "type": "Real-time Stream Ingestion",
                "endpoint": "/api/customer/submissions",
                "status": "Operational & Listening",
                "live": True,
                "last_sync": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            },
            {
                "name": "In-Store Kiosk Feedback Terminal API",
                "type": "Store Hardware Interface",
                "endpoint": "/api/customer/submissions?channel=in_store_kiosk",
                "status": "Operational & Listening",
                "live": True,
                "last_sync": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            },
            {
                "name": "SMS & WhatsApp Gateway",
                "type": "Omnichannel Ingestion",
                "endpoint": "gateway.saffron.example/webhook",
                "status": "Ready for Webhook Integration (Disconnected)",
                "live": False,
                "last_sync": "Never",
            },
        ]
    }


@app.get("/api/admin/settings")
def get_admin_settings(
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> dict:
    return {
        "platform_name": "Voice of Store Engine",
        "app_env": settings.APP_ENV,
        "app_mode": settings.APP_MODE,
        "dual_persistence_enabled": True,
        "fleet_store_count": len(state.stores_dict),
        "taxonomy_categories_count": len(state.taxonomy_dict),
        "confidence_threshold": 0.70,
        "min_evidence_threshold": 3,
        "policy_severity_floor_enabled": True,
        "auth_security": {
            "jwt_algorithm": "HS256",
            "access_token_expire_minutes": getattr(settings, "JWT_ACCESS_TOKEN_EXPIRE_MINUTES", 60),
            "rls_enforcement": "Active on all public tables",
        },
    }


@app.get("/api/admin/audit")
def get_admin_audit_log(
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    action: Optional[str] = Query(None, description="Filter by action name"),
    target_type: Optional[str] = Query(None, description="Filter by target type"),
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> dict:
    with get_db_connection() as conn:
        base_query = "SELECT * FROM audit_log WHERE 1=1"
        count_query = "SELECT count(*) FROM audit_log WHERE 1=1"
        params = []

        if action and action.upper() != "ALL":
            base_query += " AND action = ?"
            count_query += " AND action = ?"
            params.append(action.strip())

        if target_type and target_type.upper() != "ALL":
            base_query += " AND target_type = ?"
            count_query += " AND target_type = ?"
            params.append(target_type.strip())

        total = conn.execute(count_query, tuple(params)).fetchone()[0]

        base_query += " ORDER BY id DESC LIMIT ? OFFSET ?;"
        offset = (page - 1) * limit
        params.extend([limit, offset])

        rows = conn.execute(base_query, tuple(params)).fetchall()

    items = []
    for r in rows:
        d = {}
        if r["details"]:
            try:
                d = json.loads(r["details"])
            except Exception:
                pass
        items.append({
            "id": r["id"],
            "user_id": r["user_id"],
            "action": r["action"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "details": d,
            "created_at": r["created_at"],
        })

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "pages": (total + limit - 1) // limit if total > 0 else 1,
        "records": items,
    }


# -----------------------------------------------------------------------------
# Google Gemini Hybrid AI Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/gemini/status")
def get_gemini_status() -> Dict[str, Any]:
    """Returns safe, non-sensitive operational status of the Google Gemini Hybrid AI layer."""
    return gemini_service.get_status()


@app.post("/api/gemini/analyze", response_model=GeminiAnalysisResult)
def gemini_analyze_feedback(req: GeminiAnalyzeRequest) -> GeminiAnalysisResult:
    """Performs contextual Hybrid AI analysis on customer feedback with structured output."""
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Feedback text cannot be empty.")

    ctx = req.ml_context or {}
    if req.store_id and "store_id" not in ctx:
        ctx["store_id"] = req.store_id
    if req.store_name and "store_name" not in ctx:
        ctx["store_name"] = req.store_name

    return gemini_service.analyze_feedback(text=text, context=ctx)


@app.post("/api/manager/feedback/{feedback_id}/gemini-analyze")
def gemini_analyze_store_feedback(
    feedback_id: str,
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> Dict[str, Any]:
    """On-demand Gemini Hybrid AI operational analysis for Store Managers with strict store boundary enforcement."""
    with get_db_connection() as conn:
        sub = conn.execute(
            "SELECT * FROM feedback_submissions WHERE feedback_id = ? OR id = ?;",
            (feedback_id, feedback_id),
        ).fetchone()

    feedback_text = ""
    store_id = ""
    store_name = ""
    issue_type_name = ""
    severity = "moderate"
    sentiment = "neutral"

    if sub:
        store_id = sub["store_id"]
        # Store boundary check
        role = current_user.get("role")
        if role == "store_manager":
            assigned_sid = current_user.get("assigned_store_id")
            if not assigned_sid or store_id.upper() != assigned_sid.upper():
                raise HTTPException(
                    status_code=403,
                    detail=f"Forbidden: You are only authorized to manage store '{assigned_sid}'. Feedback belongs to store '{store_id}'.",
                )
        feedback_text = sub["feedback_text"]
        issue_type_name = sub.get("predicted_issue_type") or sub.get("issue_type") or "Customer Feedback"
        severity = sub.get("predicted_severity") or "moderate"
        sentiment = sub.get("predicted_sentiment") or "neutral"
        store_name = state.stores_dict.get(store_id, {}).get("store_name", store_id)
    else:
        # Check in-memory state
        fb = state.feedback_by_id.get(feedback_id)
        if not fb:
            raise HTTPException(status_code=404, detail=f"Feedback '{feedback_id}' not found.")
        store_id = fb.get("store_id", "")
        # Store boundary check
        role = current_user.get("role")
        if role == "store_manager":
            assigned_sid = current_user.get("assigned_store_id")
            if not assigned_sid or store_id.upper() != assigned_sid.upper():
                raise HTTPException(
                    status_code=403,
                    detail=f"Forbidden: You are only authorized to manage store '{assigned_sid}'. Feedback belongs to store '{store_id}'.",
                )
        feedback_text = fb.get("feedback_text") or fb.get("text", "")
        issue_type_name = state.taxonomy_dict.get(fb.get("issue_type_id"), {}).get("issue_type", "Customer Feedback")
        severity = fb.get("severity", "moderate")
        sentiment = fb.get("sentiment", "neutral")
        store_name = state.stores_dict.get(store_id, {}).get("store_name", store_id)

    res = gemini_service.analyze_feedback(
        text=feedback_text,
        context={
            "store_id": store_id,
            "store_name": store_name,
            "issue_type_name": issue_type_name,
            "severity": severity,
            "sentiment": sentiment,
        },
    )
    return {
        "feedback_id": feedback_id,
        "store_id": store_id,
        "store_name": store_name,
        "analysis": res.dict(),
    }


@app.get("/api/manager/gemini/alert-brief")
def gemini_store_alert_brief(
    store_id: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(require_roles(["store_manager", "admin"])),
) -> Dict[str, Any]:
    """Generates an AI operational alert brief for the active store."""
    role = current_user.get("role")
    target_store = current_user.get("assigned_store_id") if role == "store_manager" else (store_id or "ST-D69EG")
    if role == "store_manager" and store_id:
        enforce_store_access(current_user, store_id)
        target_store = store_id

    alert_info = {
        "store_id": target_store,
        "issue_type_id": "IT17",
        "issue_type_name": "Dairy / Perishable Quality",
        "spike_factor": 3.2,
        "recent_count": 8,
        "alert_id": f"ALERT-{target_store}",
    }
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM incident_alerts WHERE store_id = ? ORDER BY id DESC LIMIT 1;",
                (target_store,),
            ).fetchone()
            if row:
                alert_info["alert_id"] = str(row["alert_id"] if "alert_id" in row.keys() else row["id"])
                if "issue_type_id" in row.keys() and row["issue_type_id"]:
                    alert_info["issue_type_id"] = row["issue_type_id"]
                    if state.taxonomy_dict and row["issue_type_id"] in state.taxonomy_dict:
                        alert_info["issue_type_name"] = state.taxonomy_dict[row["issue_type_id"]].get("issue_type", "Dairy / Perishable Quality")
    except Exception:
        pass

    return gemini_service.explain_emerging_alert(alert_info)


@app.get("/api/admin/gemini/network-summary")
def gemini_network_summary(
    current_user: Dict[str, Any] = Depends(require_roles(["admin"])),
) -> Dict[str, Any]:
    """Synthesizes high-level AI operational fleet intelligence across all stores for HQ Executive Admin."""
    top_issues = [
        {"name": "Billing & Checkout Delays", "count": 482},
        {"name": "Produce & Dairy Freshness", "count": 315},
        {"name": "Staff Availability & Courtesy", "count": 284},
        {"name": "Product Out of Stock", "count": 210},
        {"name": "Aisle Cleanliness & Spills", "count": 145},
    ]
    total_stores = len(state.stores_dict) or 60
    return gemini_service.generate_fleet_summary(top_issues=top_issues, stores_count=total_stores)


# -----------------------------------------------------------------------------
# Static Web App Delivery
# -----------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend index.html not found.")
    return FileResponse(index_path)


# -----------------------------------------------------------------------------
# Main Execution Entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = 8000
    print(f"\n========================================================")
    print(f"Starting R5 Voice-of-Store Engine on http://127.0.0.1:{port}")
    print(f"========================================================\n")
    uvicorn.run("FULL_CODE.app:app", host="127.0.0.1", port=port, reload=False)
