"""
Regression and Verification Tests for Voice of Store Engine Public Welcome Page.

Verifies:
1. Public Welcome Page is rendered at `/` with HTTP 200.
2. Branding compliance: Product name is strictly 'Voice of Store Engine';
   no code name 'R5', no team name 'CodeNova', and no hackathon branding in visible UI.
3. Hero section: Contains headline, supporting text, CTA buttons, and visual flow.
4. Platform Features: 6 features with transparent operational status badges.
5. How It Works: 4-step workflow from raw feedback to closed-loop resolution.
6. User Portals: 3 distinct cards (Customer, Store Manager, Administrator) with distinct login triggers.
7. Public SaaS Footer: Brand name, SaaS summary, navigation links, and copyright.
8. Security: Protected endpoints require authentication; public landing page does not leak protected data.
"""

import pytest
from fastapi.testclient import TestClient
from FULL_CODE.app import app

client = TestClient(app)


def test_landing_page_serves_html():
    """Verify that root URL returns the HTML landing page with HTTP 200."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    html = response.text
    assert "<!DOCTYPE html>" in html
    assert "Voice of Store Engine" in html


def test_branding_compliance_in_landing_page():
    """Verify that branding complies with specification and no banners or unauthorized names appear."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # Visible text checks
    assert "Voice of Store Engine" in html
    # Brand title & tagline in header
    assert '<span class="font-extrabold text-xl tracking-tight text-white">Voice of Store Engine</span>' in html
    assert '<p class="text-xs text-slate-400">AI-Powered Retail Feedback Intelligence</p>' in html
    # Ensure Saffron Retail Group removed from branding
    assert "Saffron Retail Group" not in html
    # Ensure disturbing System Notice banner is completely removed
    assert "System Notice:" not in html
    # Ensure no hackathon team name in visible text
    assert "CodeNova" not in html
    # Ensure no code name 'R5' in public titles or headings
    assert "R5 Voice" not in html
    assert "R5 Dashboard" not in html


def test_hero_section_content_and_ctas():
    """Verify hero headline, supporting text, CTA buttons, and architectural visual."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # Hero section container
    assert 'id="hero"' in html
    # Required headline
    assert "AI that listens." in html
    assert "Insights that act." in html
    # Required supporting text
    assert "Turn customer feedback into meaningful insights." in html
    assert "Identify emerging problems, understand customer sentiment, and help store teams take informed action." in html
    # Required CTAs
    assert "Explore Platform" in html
    assert "View Performance" in html
    assert "handleExplorePlatform" in html
    assert "handleViewPerformance" in html
    # Architectural visual elements
    assert "INTELLIGENCE FLOW" in html
    assert "Multi-Channel Ingestion" in html
    assert "Store Manager Remediation" in html


def test_performance_at_a_glance_scorecard():
    """Verify Performance at a Glance section, 4 metric cards, and technical evaluation deep-dive."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # Destination anchor and headers
    assert 'id="performance"' in html
    assert "Proven AI Performance" in html
    assert "Measured results from benchmark evaluation of the Voice-of-Store Engine." in html

    # 4 Key metric cards
    assert "79.93%" in html and "0.7993" in html
    assert "99.88%" in html and "0.9988" in html
    assert "+1 Week" in html and "EX-1" in html
    assert "+66.2%" in html

    # Mandatory visible disclaimer note
    assert "Results are based on benchmark evaluation data and specific test conditions. Actual performance may vary." in html

    # Technical deep-dive evaluation details
    assert "Evaluation Methodology" in html
    assert "Dataset & Evaluation Period" in html
    assert "3,000 Records" in html
    assert "13 Weeks" in html
    assert "60 Retail Stores" in html
    assert "Real-World Deployment & Evaluation Limitations" in html
    assert "Zero PII exposed" in html


def test_platform_features_six_operational_cards():
    """Verify that all 6 required platform feature cards exist with transparent status badges."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert 'id="features"' in html
    # 6 feature titles
    assert "Centralized Feedback" in html
    assert "AI-Powered Classification" in html
    assert "Recurring Issue Detection" in html
    assert "Store-Level Insights" in html
    assert "Resolution Tracking" in html
    assert "Role-Based Access" in html

    # Status badges
    assert "Operational" in html


def test_how_it_works_four_step_workflow():
    """Verify that the 4-step end-to-end workflow is clearly presented."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert 'id="how-it-works"' in html
    # 4 stages
    assert "Collect Feedback" in html
    assert "Classify &amp; Organize" in html or "Classify & Organize" in html
    assert "Identify Recurring Issues" in html
    assert "Review &amp; Resolve" in html or "Review & Resolve" in html


def test_user_portals_section_and_demo_logins():
    """Verify the 3 distinct persona portal cards with login triggers."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert 'id="portals"' in html
    # Customer Portal
    assert "Customer Portal" in html
    assert "quickLoginDemo('customer')" in html
    # Store Manager Portal
    assert "Store Manager Workspace" in html
    assert "quickLoginDemo('kankarbagh')" in html
    assert "quickLoginDemo('kothrud')" in html
    # Administrator Portal
    assert "Administrator Portal" in html
    assert "quickLoginDemo('admin')" in html
    assert "openStoreManagementModal()" in html


def test_public_saas_footer():
    """Verify the comprehensive SaaS footer with links and copyright."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert 'id="footer"' in html
    assert "Voice of Store Engine" in html
    assert "2026 Voice of Store Engine" in html
    assert "All rights reserved" in html
    assert "navigateToSection(event, 'hero')" in html
    assert "navigateToSection(event, 'features')" in html
    assert "navigateToSection(event, 'how-it-works')" in html
    assert "navigateToSection(event, 'portals')" in html


def test_navigation_and_mobile_menu():
    """Verify desktop links, mobile menu toggle, and role-aware workspace navigation bar exist."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert 'id="btnMobileNavToggle"' in html
    assert 'id="mobileNavDrawer"' in html
    assert "Choose Workspace" in html
    assert 'id="workspaceNavBar"' in html
    assert 'id="workspaceNavItems"' in html
    # Authenticated navigation bar is hidden by default on public landing page
    assert 'id="workspaceNavBar" class="bg-white border-b border-slate-200 sticky top-24 z-40 shadow-sm hidden"' in html


def test_public_landing_does_not_show_internal_tabs_and_unverified_metrics():
    """Verify that public landing page isolates internal tabs and uses verified scores."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # No unverified preliminary scores like 0.9312
    assert "0.9312" not in html

    # Official benchmark scores from score.json are present
    assert "0.7993" in html
    assert "0.9988" in html
    assert "0.5000" in html

    # 3 workspace choices under Choose Your Workspace
    assert "Customer Portal" in html
    assert "Customer Sign In / Register" in html
    assert "Store Manager Workspace" in html
    assert "Store Manager Sign In" in html
    assert "Administrator Portal" in html
    assert "Administrator Sign In" in html

    # ML Engine and Evaluator Score kept out of public header and placed in Technical Evaluation
    header_html = html[:html.find("</header>")]
    assert "ML Engine: Calibrated LinearSVC" not in header_html
    assert "Evaluator Score:" not in header_html

    scorecard_html = html[html.find('id="view-scorecard"'):]
    assert "ML Engine: <strong class=\"text-emerald-700\">Calibrated LinearSVC</strong>" in scorecard_html
    assert 'id="headerScoreBadge"' in scorecard_html

    # Subtle subtitle under dashboard heading
    assert "Track customer feedback, identify recurring store-level issues, and support faster resolution." in html


def test_protected_endpoints_remain_secure_without_token():
    """Ensure that making the landing page public does not leak protected manager or customer data."""
    # Manager alerts require token
    res_alerts = client.get("/api/manager/alerts")
    assert res_alerts.status_code == 401

    # Manager summary requires token
    res_summary = client.get("/api/manager/store-summary")
    assert res_summary.status_code == 401

    # Manager inbox requires token
    res_inbox = client.get("/api/manager/feedback-inbox")
    assert res_inbox.status_code == 401

    # Customer dashboard requires token
    res_cust_dash = client.get("/api/customer/dashboard")
    assert res_cust_dash.status_code == 401

    # Customer submissions require token
    res_cust_subs = client.get("/api/customer/submissions")
    assert res_cust_subs.status_code == 401

    # Admin stores require admin token
    res_admin_stores = client.get("/api/admin/stores")
    assert res_admin_stores.status_code == 401


def test_authenticated_workspace_navigation_and_history_logic():
    """Verify that Public Home is removed from workspace nav and history routing is configured."""
    with open("FULL_CODE/static/app.js", "r", encoding="utf-8") as f:
        js = f.read()

    # Public Home must NOT appear in workspace navigation
    assert "Public Home" not in js
    assert "nav-home" not in js

    # Workspaces must contain their dedicated tabs
    assert "nav-cust-dash" in js
    assert "nav-mgr-overview" in js
    assert "nav-adm-overview" in js

    # History popstate listener and setup must be present
    assert "setupHistoryListener" in js
    assert "addEventListener('popstate'" in js
    assert "history.pushState" in js
    assert "history.replaceState" in js
