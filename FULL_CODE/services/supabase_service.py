"""Supabase Integration & Database Service (Team CodeNova).

Connects FastAPI backend securely to Supabase Auth and PostgREST.
Follows:
- Rule 3: Keep secrets out of code. Never prints or logs secrets.
- Rule 4: Validate on server and enforce store/role boundaries.
- Resilient Dual-Persistence: Acts as primary source of truth when configured,
  with transparent fallback to local SQLite for offline/CI execution.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    from FULL_CODE.config import settings
except ImportError:
    from config import settings


class SupabaseService:
    """Service interacting with Supabase Auth and PostgREST over HTTPS."""

    @classmethod
    def is_configured(cls) -> bool:
        """Returns True if Supabase URL and Publishable/Anon key are configured."""
        return bool(settings.SUPABASE_URL and settings.SUPABASE_PUBLISHABLE_KEY)

    @classmethod
    def get_headers(cls, access_token: Optional[str] = None, prefer_return: bool = False) -> Dict[str, str]:
        """Constructs headers for Supabase requests without exposing backend secrets."""
        # Prefer service-role key if available for server-side operations, otherwise publishable key
        server_key = settings.SUPABASE_SERVICE_ROLE_KEY or settings.SUPABASE_PUBLISHABLE_KEY
        headers = {
            "apikey": server_key,
            "Content-Type": "application/json",
        }
        # If user provides their authenticated bearer token, pass it to trigger their RLS context
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            headers["Authorization"] = f"Bearer {server_key}"

        if prefer_return:
            headers["Prefer"] = "return=representation"
        return headers

    @classmethod
    def verify_token(cls, token: str) -> Dict[str, Any]:
        """Validates a Supabase user access token with Supabase GoTrue Auth API.

        Returns verified user profile dictionary with role and assigned_store_id.
        Raises ValueError if token is invalid, expired, or rejected.
        """
        if not cls.is_configured():
            raise ValueError("Supabase is not configured on the backend server.")

        url = f"{settings.SUPABASE_URL}/auth/v1/user"
        headers = {
            "apikey": settings.SUPABASE_PUBLISHABLE_KEY,
            "Authorization": f"Bearer {token}",
        }

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.get(url, headers=headers)
                if res.status_code != 200:
                    raise ValueError(f"Supabase token validation rejected (Status {res.status_code})")

                user_data = res.json()
                user_id = user_data.get("id")
                email = user_data.get("email", "")
                metadata = user_data.get("user_metadata", {})

                role = metadata.get("role", "customer")
                assigned_store_id = metadata.get("assigned_store_id")
                full_name = metadata.get("full_name", email.split("@")[0] if "@" in email else "User")

                # Try querying public.profiles table using the token
                try:
                    profile_url = f"{settings.SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}&select=id,email,full_name,role,assigned_store_id"
                    p_res = client.get(profile_url, headers=headers)
                    if p_res.status_code == 200:
                        profiles = p_res.json()
                        if profiles and isinstance(profiles, list) and len(profiles) > 0:
                            p = profiles[0]
                            role = p.get("role", role)
                            assigned_store_id = p.get("assigned_store_id", assigned_store_id)
                            full_name = p.get("full_name", full_name)
                except Exception:
                    pass

                # Fallback to local SQLite profile if full_name is generic ("Customer", "User") or missing
                if not full_name or full_name.strip().lower() in ("customer", "user", email.split("@")[0].lower()):
                    try:
                        from FULL_CODE.db.connection import get_db_connection
                        with get_db_connection() as conn:
                            db_user = conn.execute(
                                "SELECT full_name, role, assigned_store_id FROM users WHERE LOWER(email) = LOWER(?)",
                                (email,)
                            ).fetchone()
                            if db_user:
                                if db_user["full_name"]:
                                    full_name = db_user["full_name"]
                                if not role or role == "customer":
                                    role = db_user["role"] or role
                                if not assigned_store_id and db_user["assigned_store_id"]:
                                    assigned_store_id = db_user["assigned_store_id"]
                    except Exception:
                        pass

                return {
                    "id": user_id,
                    "email": email,
                    "full_name": full_name,
                    "role": role,
                    "assigned_store_id": assigned_store_id,
                    "provider": "supabase",
                }
        except httpx.RequestError as exc:
            raise ValueError(f"Unable to reach Supabase Auth server: {exc}")

    @classmethod
    def insert_feedback(cls, record: Dict[str, Any], access_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Inserts customer feedback into Supabase public.feedback_submissions."""
        if not cls.is_configured():
            return None

        url = f"{settings.SUPABASE_URL}/rest/v1/feedback_submissions"
        headers = cls.get_headers(access_token=access_token, prefer_return=True)

        payload = {
            "feedback_id": record["feedback_id"],
            "text": record["text"],
            "channel": record.get("channel", "in_app"),
            "rating": record.get("rating"),
            "store_id": record.get("store_id"),
            "issue_type_id": record.get("issue_type_id"),
            "severity": record.get("severity"),
            "actionable": int(record.get("actionable", 0)),
            "is_routable": 1,
            "owner_function": record.get("owner_function"),
            "status": record.get("status", "new"),
        }
        # If user id is a valid UUID, pass customer_user_id
        uid = record.get("user_id")
        if uid and isinstance(uid, str) and len(uid) == 36 and "-" in uid:
            payload["customer_user_id"] = uid
        if record.get("customer_email"):
            payload["customer_email"] = record["customer_email"]

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.post(url, headers=headers, json=payload)
                if res.status_code in (200, 201):
                    data = res.json()
                    return data[0] if isinstance(data, list) and data else record
        except Exception:
            pass
        return None

    @classmethod
    def get_customer_submissions(
        cls,
        user_id: str,
        email: str,
        access_token: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        limit: int = 10,
    ) -> Optional[Tuple[int, List[Dict[str, Any]]]]:
        """Retrieves paginated feedback submissions for customer from Supabase."""
        if not cls.is_configured():
            return None

        url = f"{settings.SUPABASE_URL}/rest/v1/feedback_submissions"
        headers = cls.get_headers(access_token=access_token)
        headers["Prefer"] = "count=exact"

        params = {
            "select": "*",
            "order": "created_at.desc",
            "offset": str((page - 1) * limit),
            "limit": str(limit),
        }
        if status and status.lower() != "all":
            params["status"] = f"eq.{status.lower()}"
        if search and search.strip():
            s = search.strip()
            params["or"] = f"(text.ilike.*{s}*,store_id.ilike.*{s}*,feedback_id.ilike.*{s}*)"

        # Filter by customer user id if UUID, or rely on RLS with access token
        if user_id and isinstance(user_id, str) and len(user_id) == 36 and "-" in user_id:
            params["customer_user_id"] = f"eq.{user_id}"

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.get(url, headers=headers, params=params)
                if res.status_code in (200, 206):
                    items = res.json()
                    # Parse total count from Content-Range header if present
                    cr = res.headers.get("content-range", "")
                    total = len(items)
                    if "/" in cr:
                        try:
                            total = int(cr.split("/")[1])
                        except Exception:
                            pass
                    return total, items
        except Exception:
            pass
        return None

    @classmethod
    def get_manager_submissions(
        cls,
        store_id: str,
        access_token: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Retrieves customer submissions for a specific store from Supabase."""
        if not cls.is_configured():
            return None

        url = f"{settings.SUPABASE_URL}/rest/v1/feedback_submissions"
        headers = cls.get_headers(access_token=access_token)
        params = {
            "select": "*",
            "store_id": f"eq.{store_id.upper()}",
            "order": "created_at.desc",
        }

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.get(url, headers=headers, params=params)
                if res.status_code in (200, 206):
                    return res.json()
        except Exception:
            pass
        return None

    @classmethod
    def update_feedback_status(
        cls,
        feedback_id: str,
        status: str,
        resolution_notes: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Updates feedback status and resolution notes in Supabase."""
        if not cls.is_configured():
            return None

        url = f"{settings.SUPABASE_URL}/rest/v1/feedback_submissions?feedback_id=eq.{feedback_id}"
        headers = cls.get_headers(access_token=access_token, prefer_return=True)

        payload = {
            "status": status,
            "resolution_notes": resolution_notes,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.patch(url, headers=headers, json=payload)
                if res.status_code in (200, 204):
                    if res.status_code == 200 and res.text:
                        data = res.json()
                        if isinstance(data, list):
                            if len(data) == 0:
                                # 0 rows updated in Supabase (e.g. RLS policy rejected or feedback_id not found)
                                return None
                            return data[0]
                    return payload
                else:
                    print(f"[SupabaseService] PATCH feedback failed: HTTP {res.status_code} - {res.text}")
        except Exception as e:
            print(f"[SupabaseService] PATCH feedback exception: {type(e).__name__} - {e}")
        return None

    @classmethod
    def get_alerts(
        cls,
        store_id: Optional[str] = None,
        status: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Retrieves incident alerts from Supabase."""
        if not cls.is_configured():
            return None

        url = f"{settings.SUPABASE_URL}/rest/v1/incident_alerts"
        headers = cls.get_headers(access_token=access_token)
        params = {
            "select": "*",
            "order": "as_of_week_start.desc,confidence.desc",
        }
        if store_id:
            params["store_id"] = f"eq.{store_id.upper()}"
        if status and status.lower() != "all":
            params["status"] = f"eq.{status.lower()}"

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.get(url, headers=headers, params=params)
                if res.status_code in (200, 206):
                    return res.json()
                print(f"[SupabaseService] GET incident_alerts returned HTTP {res.status_code}: {res.text}")
        except Exception as e:
            print(f"[SupabaseService] GET incident_alerts exception: {e}")
        return None

    @classmethod
    def update_alert(
        cls,
        alert_id: str,
        status: str,
        resolution_notes: Optional[str] = None,
        user_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Updates incident alert status and resolution notes in Supabase."""
        if not cls.is_configured():
            return None

        url = f"{settings.SUPABASE_URL}/rest/v1/incident_alerts?alert_id=eq.{alert_id}"
        headers = cls.get_headers(access_token=access_token, prefer_return=True)

        payload = {
            "status": status,
            "resolution_note": resolution_notes,
            "resolution_notes": resolution_notes,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if user_id and isinstance(user_id, str) and len(user_id) == 36 and "-" in user_id:
            payload["resolved_by_user_id"] = user_id

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.patch(url, headers=headers, json=payload)
                if res.status_code in (200, 204):
                    if res.status_code == 200 and res.text:
                        data = res.json()
                        if isinstance(data, list):
                            if len(data) == 0:
                                # 0 rows updated in Supabase (e.g. RLS policy rejected or feedback_id not found)
                                return None
                            return data[0]
                    return payload
                else:
                    print(f"[SupabaseService] PATCH alert failed: HTTP {res.status_code} - {res.text}")
        except Exception as e:
            print(f"[SupabaseService] PATCH alert exception: {type(e).__name__} - {e}")
        return None

    @classmethod
    def insert_audit_log(
        cls,
        user_id: Optional[str],
        action: str,
        target_type: str,
        target_id: str,
        details: Dict[str, Any],
        access_token: Optional[str] = None,
    ) -> bool:
        """Appends an event to public.audit_log in Supabase."""
        if not cls.is_configured():
            return False

        url = f"{settings.SUPABASE_URL}/rest/v1/audit_log"
        headers = cls.get_headers(access_token=access_token)

        payload = {
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id),
            "details": details,
        }
        if user_id and isinstance(user_id, str) and len(user_id) == 36 and "-" in user_id:
            payload["user_id"] = user_id

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.post(url, headers=headers, json=payload)
                return res.status_code in (200, 201)
        except Exception:
            pass
        return False
