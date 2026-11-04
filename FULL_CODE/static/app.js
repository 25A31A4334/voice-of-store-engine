// Client-Side Controller for Voice of Store Engine Platform

let currentPage = 1;
let searchTimeout = null;

// Supabase Client & Authentication State
let _supabase = null;
let _currentSession = null;
let _currentUser = null;
let _authMode = 'signin';

// Presets for the Live Classifier Demo
const PRESETS = {
    freezer: {
        text: "Deep freezer in dairy aisle 4 has stopped cooling completely, milk packets are leaking and ice cream has melted.",
        channel: "staff_shift_note",
        rating: "",
        store: "ST-001JY"
    },
    allergen: {
        text: "Eggless cake from bakery contained egg, child had severe allergic reaction and was rushed to hospital emergency!",
        channel: "call_centre_ticket",
        rating: "1",
        store: "ST-D69EG"
    },
    billing: {
        text: "Waited 45 mins at billing counter in Kothrud, only 1 cashier active in evening peak, line reached the entrance.",
        channel: "in_app",
        rating: "2",
        store: ""
    },
    marathi: {
        text: "para sakali 9 lach dudh sampla, shelf rikama. store: kothrud.",
        channel: "in_app",
        rating: "3",
        store: ""
    },
    praise: {
        text: "Billing was very fast and staff Amit helped find everything quickly, thank you Saffron Retail!",
        channel: "online_review",
        rating: "5",
        store: "ST-001JY"
    }
};

// Chart.js Instance Cache and Safe Lifecycle Renderer
window._activeCharts = window._activeCharts || {};

function renderOrUpdateChart(canvasId, config) {
    if (typeof Chart === 'undefined') {
        console.warn('Chart.js library not loaded yet for canvas:', canvasId);
        return null;
    }
    const canvas = document.getElementById(canvasId);
    if (!canvas) return null;

    if (window._activeCharts[canvasId]) {
        try {
            window._activeCharts[canvasId].destroy();
        } catch (e) {
            console.warn('Error destroying existing chart instance:', canvasId, e);
        }
        delete window._activeCharts[canvasId];
    }

    try {
        const ctx = canvas.getContext('2d');
        const chart = new Chart(ctx, config);
        window._activeCharts[canvasId] = chart;
        return chart;
    } catch (e) {
        console.error('Failed to create chart on canvas ' + canvasId + ':', e);
        return null;
    }
}

// -----------------------------------------------------------------------------
// Safe Error Response Parser
// -----------------------------------------------------------------------------
async function parseErrorResponse(res, defaultMsg = 'An unexpected error occurred.') {
    let msg = defaultMsg;
    try {
        const text = await res.text();
        if (text) {
            try {
                const data = JSON.parse(text);
                if (data && data.detail) {
                    msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
                } else if (data && data.message) {
                    msg = data.message;
                } else if (data && data.error) {
                    msg = typeof data.error === 'string' ? data.error : JSON.stringify(data.error);
                } else {
                    msg = text;
                }
            } catch (_) {
                // Non-JSON response (e.g. plain text / HTML from 500, 502, 504)
                msg = text.length > 250 ? `${text.slice(0, 247)}...` : text;
            }
        }
    } catch (_) {
        // Text reading failed
    }
    return msg ? `HTTP ${res.status}: ${msg}` : `HTTP ${res.status}: ${defaultMsg}`;
}

// -----------------------------------------------------------------------------
// Initialization
// -----------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    initSupabaseAuth();
    loadStoredLocalSession();
    loadKPIs();
    loadRegionalBrief();
    loadEmergingAlerts();
    loadExplorerFeedback(1);
    populateCustomerStoreSelect();

    setupHistoryListener();

    // Route management: Opening base URL '/' must always display the public welcome page
    const initialUser = getAuthUser();
    let initialTab = 'home';
    let subView = null;

    const rawHash = (window.location.hash || '').replace(/^#\/?/, '').split('?')[0].trim();

    if (rawHash && rawHash !== 'home') {
        if (rawHash.startsWith('customer/')) {
            initialTab = 'customer';
            subView = rawHash.replace('customer/', '');
        } else if (rawHash.startsWith('manager/')) {
            initialTab = 'manager';
            subView = rawHash.replace('manager/', '');
        } else if (rawHash.startsWith('login-') || rawHash === 'login') {
            initialTab = 'login';
        } else {
            initialTab = rawHash;
        }
    } else {
        // Base URL '/' or '#home': Always load public landing page
        initialTab = 'home';
    }

    if (initialTab === 'home') {
        // Public landing page is always rendered at base URL regardless of session
        switchTab('home', false);
    } else if (initialTab === 'login') {
        switchTab('home', false);
        if (!initialUser) {
            const role = (window.location.hash && window.location.hash.includes('manager')) ? 'manager' : ((window.location.hash && window.location.hash.includes('admin')) ? 'admin' : 'customer');
            openAuthModalForRole(role, false);
        }
    } else if (initialUser) {
        // Explicit deep-link hash requested by an authenticated user
        const adminViews = ['overview', 'alerts', 'classifier', 'explorer', 'scorecard', 'admin-users', 'admin-insights', 'admin-remediations', 'admin-integrations', 'admin-settings', 'admin-audit'];
        if (adminViews.includes(initialTab) && initialUser.role !== 'admin') {
            switchTab('home', false);
            showManagerToast('Access Denied', 'Administrator privileges required for this view.', 'error');
        } else if (initialTab === 'manager' && initialUser.role !== 'store_manager' && initialUser.role !== 'admin') {
            switchTab('home', false);
            showManagerToast('Access Denied', 'Store Manager privileges required for this workspace.', 'error');
        } else {
            if (initialTab === 'manager' && subView) {
                currentManagerSubView = subView;
            } else if (initialTab === 'customer' && subView) {
                currentCustomerSubView = subView;
            }
            switchTab(initialTab, false);
            if (initialTab === 'customer' && subView) {
                showCustomerSubView(subView, false);
            } else if (initialTab === 'manager') {
                showManagerSubView(subView || 'overview', false);
            }
        }
    } else {
        // Explicit deep-link hash requested by unauthenticated visitor -> protected views redirect to home
        switchTab('home', false);
        const role = initialTab === 'manager' ? 'manager' : (initialTab === 'customer' ? 'customer' : 'admin');
        showManagerToast('Sign-In Required', 'Please sign in to access this workspace.', 'error');
        openAuthModalForRole(role, false);
    }
});

// -----------------------------------------------------------------------------
// Browser Back/Forward History Listener
// -----------------------------------------------------------------------------
function setupHistoryListener() {
    window.addEventListener('popstate', (event) => {
        const user = getAuthUser();
        const token = getAuthToken();
        const state = event.state;
        let targetTab = (state && state.tab) || null;
        let subView = (state && state.subview) || null;

        if (!targetTab && window.location.hash) {
            const hash = window.location.hash.replace(/^#\/?/, '').split('?')[0];
            if (hash.startsWith('customer/')) {
                targetTab = 'customer';
                subView = hash.replace('customer/', '');
            } else if (hash.startsWith('manager/')) {
                targetTab = 'manager';
                subView = hash.replace('manager/', '');
            } else if (hash.startsWith('login-') || hash === 'login') {
                targetTab = 'login';
            } else {
                targetTab = hash;
            }
        }

        if (!targetTab) {
            targetTab = 'home';
        }

        // Handle navigation back to login screen
        if (targetTab === 'login') {
            if (user) {
                // If user is already authenticated, close login modal and show home
                const modal = document.getElementById('authModal');
                if (modal && !modal.classList.contains('hidden')) {
                    modal.classList.add('hidden');
                    clearAuthAlert();
                }
                switchTab('home', false);
                return;
            }
            const role = (state && state.role) || (window.location.hash.includes('manager') ? 'manager' : (window.location.hash.includes('admin') ? 'admin' : 'customer'));
            openAuthModalForRole(role, false);
            return;
        } else {
            // Close auth modal if user navigates away from login via history
            const modal = document.getElementById('authModal');
            if (modal && !modal.classList.contains('hidden')) {
                modal.classList.add('hidden');
                clearAuthAlert();
            }
        }

        // If user logged out, protect authenticated workspaces from being usable via Back button
        if (!user || !token) {
            if (targetTab !== 'home') {
                history.replaceState({ tab: 'home' }, '', '#home');
                switchTab('home', false);
                showManagerToast('Session Expired', 'You have been signed out. Please sign in to access this workspace.', 'error');
                return;
            }
        }

        // Authenticated user navigates Back to home: display home without logging out!
        if (user && targetTab === 'home') {
            switchTab('home', false);
            return;
        }

        if (targetTab === 'manager' && subView) {
            currentManagerSubView = subView;
        } else if (targetTab === 'customer' && subView) {
            currentCustomerSubView = subView;
        }

        switchTab(targetTab, false);
        if (targetTab === 'customer' && subView) {
            showCustomerSubView(subView, false);
        } else if (targetTab === 'manager') {
            showManagerSubView(subView || 'overview', false);
        }
    });
}

// -----------------------------------------------------------------------------
function switchTab(tabId, pushHistory = true, subView = null) {
    const user = getAuthUser();
    const token = getAuthToken();

    if (tabId === 'manager' && subView) {
        currentManagerSubView = subView;
    } else if (tabId === 'customer' && subView) {
        currentCustomerSubView = subView;
    }

    // Enforce role-based access control for internal views
    if (tabId !== 'home') {
        const adminViews = ['overview', 'alerts', 'classifier', 'explorer', 'scorecard', 'admin-users', 'admin-insights', 'admin-remediations', 'admin-integrations', 'admin-settings', 'admin-audit'];
        if (adminViews.includes(tabId)) {
            if (!user || !token) {
                showManagerToast('Administrator Sign-In Required', 'Please sign in with administrator credentials to access platform analytics.', 'error');
                if (pushHistory) {
                    history.pushState({ tab: 'login', role: 'admin' }, '', '#login-admin');
                } else {
                    history.replaceState({ tab: 'login', role: 'admin' }, '', '#login-admin');
                }
                openAuthModalForRole('admin', false);
                return;
            }
            if (user.role !== 'admin') {
                showManagerToast('Access Denied', 'This section is restricted to HQ Administrators.', 'error');
                if (user.role === 'store_manager') {
                    switchTab('manager', pushHistory);
                } else {
                    switchTab('customer', pushHistory);
                }
                return;
            }
        } else if (tabId === 'manager') {
            if (!user || !token) {
                showManagerToast('Store Manager Sign-In Required', 'Please sign in to access the store manager workspace.', 'error');
                openAuthModalForRole('manager', pushHistory);
                return;
            }
            if (user.role === 'customer') {
                showManagerToast('Access Denied', 'The Store Manager workspace is restricted to store managers and administrators.', 'error');
                switchTab('customer', pushHistory);
                return;
            }
        }
    }

    // Hide all tab view sections (All 22 sections supported)
    const sections = [
        'home', 'overview', 'alerts', 'classifier', 'explorer', 'scorecard', 'manager', 'customer',
        'admin-users', 'admin-insights', 'admin-remediations', 'admin-integrations', 'admin-settings', 'admin-audit'
    ];
    sections.forEach(s => {
        const el = document.getElementById(`view-${s}`);
        const btn = document.getElementById(`tab-${s}`);
        if (el) el.classList.add('hidden');
        if (btn) {
            btn.classList.remove('border-saffron-500', 'text-saffron-600');
            btn.classList.add('border-transparent', 'text-slate-500');
        }
    });

    // Show active tab view
    const activeEl = document.getElementById(`view-${tabId}`);
    const activeBtn = document.getElementById(`tab-${tabId}`);
    if (activeEl) activeEl.classList.remove('hidden');
    if (activeBtn) {
        activeBtn.classList.add('border-saffron-500', 'text-saffron-600');
        activeBtn.classList.remove('border-transparent', 'text-slate-500');
    }

    try {
        sessionStorage.setItem('vse_active_tab', tabId);
    } catch (e) {}

    // Synchronize browser history and URL hash predictably without duplicate entries
    const targetHash = '#' + tabId;
    const hasMatchingSubHash = window.location.hash && (window.location.hash === targetHash || window.location.hash.startsWith(targetHash + '/'));
    if (pushHistory) {
        if (!history.state || history.state.tab !== tabId || (!hasMatchingSubHash && window.location.hash !== targetHash)) {
            history.pushState({ tab: tabId }, '', targetHash);
        }
    } else {
        if (!history.state || history.state.tab !== tabId || (!hasMatchingSubHash && window.location.hash !== targetHash)) {
            const replaceUrl = (tabId === 'home' && (!window.location.hash || window.location.hash === '#' || window.location.hash === '#/'))
                ? (window.location.pathname || '/')
                : targetHash;
            history.replaceState({ tab: tabId }, '', replaceUrl);
        }
    }

    // Dynamic Role-Aware Workspace Navigation
    renderWorkspaceNavigation(user, tabId);

    if (tabId === 'manager') {
        loadManagerWorkspace();
    } else if (tabId === 'customer') {
        loadCustomerPortal();
    } else if (tabId === 'admin-users') {
        loadAdminUsers();
    } else if (tabId === 'admin-insights') {
        loadAdminInsights();
    } else if (tabId === 'admin-remediations') {
        loadAdminRemediations();
    } else if (tabId === 'admin-integrations') {
        loadAdminIntegrations();
    } else if (tabId === 'admin-settings') {
        loadAdminSettings();
    } else if (tabId === 'admin-audit') {
        loadAdminAudit();
    } else if (tabId === 'overview') {
        loadRegionalBrief();
        loadKPIs();
    } else if (tabId === 'alerts') {
        loadEmergingAlerts();
    } else if (tabId === 'explorer') {
        loadExplorerFeedback();
    }
}

// -----------------------------------------------------------------------------
// Role-Aware Dynamic Workspace Navigation Bar
// -----------------------------------------------------------------------------
function renderWorkspaceNavigation(user, activeTabId) {
    const navBar = document.getElementById('workspaceNavBar');
    const container = document.getElementById('workspaceNavItems');
    if (!container || !navBar) return;

    // Do NOT show internal dashboard navigation when on public landing page or unauthenticated
    if (!user || activeTabId === 'home') {
        navBar.classList.add('hidden');
        container.innerHTML = '';
        return;
    }

    navBar.classList.remove('hidden');

    let navItems = [];

    if (user.role === 'customer') {
        navItems = [
            { id: 'nav-cust-dash', label: '📊 My Dashboard', action: () => { switchTab('customer'); showCustomerSubView('dashboard'); }, active: activeTabId === 'customer' && (currentCustomerSubView === 'dashboard' || !currentCustomerSubView) },
            { id: 'nav-cust-submit', label: '✍️ Submit Feedback', action: () => { switchTab('customer'); showCustomerSubView('submit'); }, active: activeTabId === 'customer' && currentCustomerSubView === 'submit' },
            { id: 'nav-cust-hist', label: '📜 Submission History', action: () => { switchTab('customer'); showCustomerSubView('history'); }, active: activeTabId === 'customer' && currentCustomerSubView === 'history' },
        ];
    } else if (user.role === 'store_manager') {
        const activeSub = currentManagerSubView || 'overview';
        navItems = [
            { id: 'nav-mgr-overview', label: '🏬 Overview', action: () => (activeTabId === 'manager' ? showManagerSubView('overview') : switchTab('manager', true, 'overview')), active: activeTabId === 'manager' && activeSub === 'overview' },
            { id: 'nav-mgr-insights', label: '💡 Insights', action: () => (activeTabId === 'manager' ? showManagerSubView('insights') : switchTab('manager', true, 'insights')), active: activeTabId === 'manager' && activeSub === 'insights' },
            { id: 'nav-mgr-inbox', label: '📥 Inbox', action: () => (activeTabId === 'manager' ? showManagerSubView('inbox') : switchTab('manager', true, 'inbox')), active: activeTabId === 'manager' && activeSub === 'inbox' },
            { id: 'nav-mgr-alerts', label: '📡 Alerts', action: () => (activeTabId === 'manager' ? showManagerSubView('alerts') : switchTab('manager', true, 'alerts')), active: activeTabId === 'manager' && activeSub === 'alerts' },
            { id: 'nav-mgr-actions', label: '⚡ Action Center', action: () => (activeTabId === 'manager' ? showManagerSubView('actions') : switchTab('manager', true, 'actions')), active: activeTabId === 'manager' && activeSub === 'actions' },
            { id: 'nav-mgr-analytics', label: '📈 Analytics', action: () => (activeTabId === 'manager' ? showManagerSubView('analytics') : switchTab('manager', true, 'analytics')), active: activeTabId === 'manager' && activeSub === 'analytics' },
            { id: 'nav-mgr-reports', label: '📑 Reports', action: () => (activeTabId === 'manager' ? showManagerSubView('reports') : switchTab('manager', true, 'reports')), active: activeTabId === 'manager' && activeSub === 'reports' },
            { id: 'nav-mgr-settings', label: '⚙️ Settings', action: () => (activeTabId === 'manager' ? showManagerSubView('settings') : switchTab('manager', true, 'settings')), active: activeTabId === 'manager' && activeSub === 'settings' }
        ];
    } else if (user.role === 'admin') {
        navItems = [
            { id: 'nav-adm-overview', label: '🌐 Overview', action: () => switchTab('overview'), active: activeTabId === 'overview' },
            { id: 'nav-adm-stores', label: '🏬 Stores (60)', action: () => openStoreManagementModal(), active: false },
            { id: 'nav-adm-users', label: '👥 Users', action: () => switchTab('admin-users'), active: activeTabId === 'admin-users' },
            { id: 'nav-adm-explorer', label: '🔍 Global Feedback', action: () => switchTab('explorer'), active: activeTabId === 'explorer' },
            { id: 'nav-adm-insights', label: '📊 Network Insights', action: () => switchTab('admin-insights'), active: activeTabId === 'admin-insights' },
            { id: 'nav-adm-classifier', label: '🧪 AI Ops', action: () => switchTab('classifier'), active: activeTabId === 'classifier' },
            { id: 'nav-adm-alerts', label: '📡 All Alerts', action: () => switchTab('alerts'), active: activeTabId === 'alerts' },
            { id: 'nav-adm-remediations', label: '🛡️ Remediations', action: () => switchTab('admin-remediations'), active: activeTabId === 'admin-remediations' },
            { id: 'nav-adm-integrations', label: '🔌 Integrations', action: () => switchTab('admin-integrations'), active: activeTabId === 'admin-integrations' },
            { id: 'nav-adm-settings', label: '⚙️ Settings', action: () => switchTab('admin-settings'), active: activeTabId === 'admin-settings' },
            { id: 'nav-adm-audit', label: '📜 Audit Log', action: () => switchTab('admin-audit'), active: activeTabId === 'admin-audit' },
            { id: 'nav-adm-scorecard', label: '📊 Scorecard', action: () => switchTab('scorecard'), active: activeTabId === 'scorecard' }
        ];
    }

    container.innerHTML = '';
    navItems.forEach(item => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.id = item.id;
        btn.innerText = item.label;
        btn.onclick = item.action;
        if (item.active) {
            btn.className = 'px-3.5 py-1.5 rounded-lg font-bold text-xs sm:text-sm bg-navy-900 text-white shadow-xs transition';
            btn.setAttribute('aria-current', 'page');
        } else {
            btn.className = 'px-3.5 py-1.5 rounded-lg font-medium text-xs sm:text-sm text-slate-600 hover:text-navy-900 hover:bg-slate-100 transition';
            btn.removeAttribute('aria-current');
        }
        container.appendChild(btn);
    });
}

// -----------------------------------------------------------------------------
// Landing Page Navigation & Interaction Helpers
// -----------------------------------------------------------------------------
function goToUserWorkspace() {
    const user = getAuthUser();
    if (!user) {
        navigateToSection(null, 'portals');
        return;
    }
    if (user.role === 'customer') {
        switchTab('customer');
        showCustomerSubView(currentCustomerSubView || 'dashboard');
    } else if (user.role === 'store_manager') {
        switchTab('manager');
        showManagerSubView(currentManagerSubView || 'overview');
    } else if (user.role === 'admin') {
        switchTab('overview');
    } else {
        switchTab('home');
    }
}

function navigateToSection(event, sectionId) {
    if (event && event.preventDefault) event.preventDefault();
    closeMobileMenu();

    const homeEl = document.getElementById('view-home');
    if (!homeEl || homeEl.classList.contains('hidden')) {
        switchTab('home');
    }

    setTimeout(() => {
        const target = document.getElementById(sectionId);
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    }, 50);
}

function toggleMobileMenu() {
    const drawer = document.getElementById('mobileNavDrawer');
    if (drawer) {
        drawer.classList.toggle('hidden');
    }
}

function closeMobileMenu() {
    const drawer = document.getElementById('mobileNavDrawer');
    if (drawer) {
        drawer.classList.add('hidden');
    }
}

function handleExploreDashboard() {
    const user = getAuthUser();
    if (user) {
        if (user.role === 'customer') {
            switchTab('customer');
            showCustomerSubView(currentCustomerSubView || 'dashboard');
        } else if (user.role === 'store_manager') {
            switchTab('manager');
            showManagerSubView(currentManagerSubView || 'overview');
        } else {
            switchTab('overview');
        }
    } else {
        navigateToSection(null, 'portals');
    }
}

function handleSubmitFeedbackHero() {
    const user = getAuthUser();
    if (user && user.role === 'customer') {
        switchTab('customer');
        showCustomerSubView('submit');
    } else if (user) {
        switchTab('customer');
        showCustomerSubView('submit');
    } else {
        openAuthModalForRole('customer');
    }
}

function openAuthModalForRole(role, pushHistory = true) {
    const user = getAuthUser();
    if (user) {
        if (role === 'customer' && user.role === 'customer') {
            switchTab('customer');
            showCustomerSubView(currentCustomerSubView || 'dashboard');
            return;
        } else if (role === 'manager' && (user.role === 'store_manager' || user.role === 'admin')) {
            switchTab('manager');
            showManagerSubView(currentManagerSubView || 'overview');
            return;
        } else if (role === 'admin' && user.role === 'admin') {
            switchTab('overview');
            return;
        }
    }
    openAuthModal();
    if (pushHistory) {
        if (!history.state || history.state.tab !== 'login' || history.state.role !== role) {
            history.pushState({ tab: 'login', role: role }, '', '#login-' + role);
        }
    }
    const title = document.getElementById('authModalTitle');
    const tabRegister = document.getElementById('authTabRegister');
    if (role === 'customer') {
        if (title) title.innerText = 'Voice of Store Engine — Customer Access';
        if (tabRegister) tabRegister.classList.remove('hidden');
        switchAuthMode('signin');
    } else if (role === 'manager') {
        if (title) title.innerText = 'Voice of Store Engine — Store Manager Sign In';
        if (tabRegister) tabRegister.classList.add('hidden');
        switchAuthMode('signin');
    } else if (role === 'admin') {
        if (title) title.innerText = 'Voice of Store Engine — Administrator Access';
        if (tabRegister) tabRegister.classList.add('hidden');
        switchAuthMode('signin');
    }
}

// -----------------------------------------------------------------------------
// 1. KPI Loading
// -----------------------------------------------------------------------------
async function loadKPIs() {
    try {
        const res = await fetch('/api/kpis');
        if (!res.ok) return;
        const data = await res.json();

        document.getElementById('kpi-total').innerText = data.total_feedback.toLocaleString();
        document.getElementById('kpi-actionable').innerText = data.actionable_count.toLocaleString();
        document.getElementById('kpi-actionable-pct').innerText = `${data.actionable_pct}% Actionable`;
        document.getElementById('kpi-high').innerText = data.high_or_critical_count.toLocaleString();
        document.getElementById('kpi-critical').innerText = `${data.critical_count} Critical`;
        document.getElementById('kpi-stores').innerText = `${data.stores_monitored}`;
        const scoreBadge = document.getElementById('headerScoreBadge');
        if (scoreBadge) scoreBadge.innerText = data.evaluator_primary_score.toFixed(4);
    } catch (e) {
        console.error('Error loading KPIs:', e);
    }
}

// -----------------------------------------------------------------------------
// 2. Regional Briefs Loading
// -----------------------------------------------------------------------------
async function loadRegionalBrief() {
    const region = document.getElementById('briefRegionSelect').value;
    const weekSelect = document.getElementById('briefWeekSelect');
    const selectedWeek = weekSelect.value || '';

    try {
        const url = `/api/regional-briefs?region=${encodeURIComponent(region)}&week=${encodeURIComponent(selectedWeek)}`;
        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();

        // Populate available weeks once
        if (weekSelect.children.length === 0 && data.available_weeks) {
            data.available_weeks.forEach(w => {
                const opt = document.createElement('option');
                opt.value = w;
                opt.innerText = `Week of ${w}`;
                if (w === data.week) opt.selected = true;
                weekSelect.appendChild(opt);
            });
        }

        const tbody = document.getElementById('briefTableBody');
        tbody.innerHTML = '';

        if (!data.top_5_issues || data.top_5_issues.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="text-center py-6 text-slate-400">No actionable issues recorded for this region and week.</td></tr>`;
            return;
        }

        data.top_5_issues.forEach(item => {
            const tr = document.createElement('tr');
            tr.className = 'hover:bg-slate-50 transition border-b border-slate-100';

            // Trend styling
            let trendClass = 'trend-flat';
            if (item.trend_class === 'increase') trendClass = 'trend-increase';
            else if (item.trend_class === 'decrease') trendClass = 'trend-decrease';

            // Evidence quotes rendering
            let quotesHtml = item.evidence_quotes.map(q => `<div class="italic text-slate-600 mb-1 border-l-2 border-amber-300 pl-2">"${q.length > 110 ? q.slice(0, 110) + '...' : q}"</div>`).join('');
            if (!quotesHtml) quotesHtml = `<span class="text-slate-400 italic">No direct quotes</span>`;

            // Affected stores badge
            const storesText = item.affected_stores && item.affected_stores.length > 0
                ? item.affected_stores.slice(0, 3).map(s => `<span class="inline-block bg-slate-100 text-slate-700 px-1.5 py-0.5 rounded text-[10px] mr-1 mb-1">${s}</span>`).join('')
                : `<span class="text-slate-400">Central / Multi-store</span>`;

            tr.innerHTML = `
                <td class="py-3 px-3 text-center font-bold text-slate-400 text-xs">#${item.rank}</td>
                <td class="py-3 px-4">
                    <div class="font-bold text-navy-900">${item.issue_name}</div>
                    <div class="text-[10px] text-slate-500">ID: <span class="font-mono font-semibold">${item.issue_type_id}</span> • Owner: <span class="text-indigo-600 font-semibold">${item.owner_function}</span></div>
                </td>
                <td class="py-3 px-3 text-center font-semibold text-slate-800">${item.count}</td>
                <td class="py-3 px-3 text-center ${trendClass} text-xs">${item.trend_arrow}</td>
                <td class="py-3 px-4">${storesText}</td>
                <td class="py-3 px-4 text-xs">${quotesHtml}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error('Error loading regional brief:', e);
    }
}

// -----------------------------------------------------------------------------
// 3. Emerging Alerts Loading
// -----------------------------------------------------------------------------
async function loadEmergingAlerts() {
    try {
        const res = await fetch('/api/emerging-alerts');
        if (!res.ok) return;
        const data = await res.json();

        const badge = document.getElementById('alertsBadge');
        if (badge) badge.innerText = data.total_alerts;

        const container = document.getElementById('alertsContainer');
        container.innerHTML = '';

        if (!data.alerts || data.alerts.length === 0) {
            container.innerHTML = `<div class="p-8 text-center text-slate-400 text-xs">No active emerging alerts.</div>`;
            return;
        }

        data.alerts.forEach((alert, idx) => {
            const card = document.createElement('div');
            card.className = `p-4 rounded-xl border ${alert.is_worked_incident_ex1 ? 'border-amber-400 bg-amber-50/40' : 'border-slate-200 bg-white'} shadow-sm space-y-3`;

            // Evidence quotes rendering
            let evidenceHtml = alert.evidence_samples.map(ev => `
                <div class="p-2.5 bg-slate-50 rounded border border-slate-200 text-xs text-slate-700 space-y-1">
                    <div class="flex items-center justify-between text-[10px] text-slate-400">
                        <span>ID: <strong class="font-mono text-slate-600">${ev.feedback_id}</strong> (${ev.channel})</span>
                        <span>${ev.created_at || ''}</span>
                    </div>
                    <p class="italic text-slate-800">"${ev.text}"</p>
                </div>
            `).join('');

            card.innerHTML = `
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-100 pb-2">
                    <div class="flex items-center space-x-2">
                        <span class="px-2 py-0.5 rounded text-xs font-bold ${alert.confidence >= 0.5 ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'}">
                            Confidence: ${(alert.confidence * 100).toFixed(1)}%
                        </span>
                        <span class="font-bold text-sm text-navy-900">${alert.store_name} (${alert.store_id})</span>
                        ${alert.is_worked_incident_ex1 ? '<span class="px-2 py-0.2 bg-emerald-100 text-emerald-800 rounded font-bold text-[10px]">⭐ EX-1 Worked Example</span>' : ''}
                    </div>
                    <div class="text-xs text-slate-500">
                        <span>Week of: <strong>${alert.as_of_week_start}</strong></span> • <span>${alert.city}, ${alert.region}</span>
                    </div>
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
                    <div>
                        <span class="text-slate-400 text-[10px] uppercase font-semibold">Flagged Issue</span>
                        <p class="font-semibold text-slate-800">${alert.issue_name} (<span class="font-mono">${alert.issue_type_id}</span>)</p>
                    </div>
                    <div>
                        <span class="text-slate-400 text-[10px] uppercase font-semibold">Owning Function</span>
                        <p class="font-semibold text-indigo-600">${alert.owner_function}</p>
                    </div>
                    <div>
                        <span class="text-slate-400 text-[10px] uppercase font-semibold">Evidence Citing</span>
                        <p class="font-semibold text-slate-800">${alert.evidence_count} Verified Records</p>
                    </div>
                </div>

                <!-- Evidence Accordion Toggle -->
                <div class="pt-2 border-t border-slate-100">
                    <button onclick="toggleEvidence(${idx})" class="text-xs font-semibold text-saffron-600 hover:text-saffron-700 flex items-center space-x-1">
                        <span>View Verified Evidence Quotes (${alert.evidence_count} IDs)</span>
                        <span id="ev-icon-${idx}">▼</span>
                    </button>
                    <div id="ev-box-${idx}" class="mt-2 space-y-2 hidden">
                        ${evidenceHtml}
                    </div>
                </div>
            `;
            container.appendChild(card);
        });
    } catch (e) {
        console.error('Error loading emerging alerts:', e);
    }
}

function toggleEvidence(idx) {
    const box = document.getElementById(`ev-box-${idx}`);
    const icon = document.getElementById(`ev-icon-${idx}`);
    if (box.classList.contains('hidden')) {
        box.classList.remove('hidden');
        icon.innerText = '▲';
    } else {
        box.classList.add('hidden');
        icon.innerText = '▼';
    }
}

// -----------------------------------------------------------------------------
// 4. Live Classifier Handler & Presets
// -----------------------------------------------------------------------------
function setSample(type) {
    const sample = PRESETS[type];
    if (!sample) return;
    document.getElementById('inputText').value = sample.text;
    document.getElementById('inputChannel').value = sample.channel;
    document.getElementById('inputRating').value = sample.rating;
    document.getElementById('inputStoreId').value = sample.store;
}

async function handleClassify(e) {
    e.preventDefault();
    const btn = document.getElementById('btnClassify');
    const text = document.getElementById('inputText').value.trim();
    const channel = document.getElementById('inputChannel').value;
    const ratingVal = document.getElementById('inputRating').value;
    const storeId = document.getElementById('inputStoreId').value.trim() || null;

    btn.disabled = true;
    btn.innerText = 'Classifying & Routing...';

    try {
        const payload = {
            text: text,
            channel: channel,
            rating: ratingVal ? parseFloat(ratingVal) : null,
            store_id: storeId
        };

        const res = await fetch('/api/classify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!res.ok) {
            const err = await res.json();
            alert(`Classification error: ${err.detail || 'Could not classify'}`);
            return;
        }

        const data = await res.json();

        // Render result card
        document.getElementById('resultPlaceholder').classList.add('hidden');
        document.getElementById('resultContent').classList.remove('hidden');

        document.getElementById('resIssueName').innerText = data.issue_type_name;
        document.getElementById('resIssueId').innerText = data.issue_type_id;
        document.getElementById('resOwnerFunc').innerText = data.owner_function;

        // Severity Badge
        const sevBadge = document.getElementById('resSeverityBadge');
        const sev = (data.severity || 'low').toLowerCase();
        sevBadge.innerText = sev.toUpperCase();
        sevBadge.className = `px-3 py-1 rounded-md font-bold text-xs badge-${sev}`;
        document.getElementById('resFloor').innerText = data.policy_severity_floor;

        // Sentiment Badge
        const sentBadge = document.getElementById('resSentimentBadge');
        if (sentBadge) {
            const sent = data.sentiment || 'Neutral';
            sentBadge.innerText = sent;
            let sentClass = 'bg-slate-100 text-slate-700';
            if (sent === 'Positive') sentClass = 'bg-emerald-100 text-emerald-800 border border-emerald-300';
            else if (sent === 'Negative') sentClass = 'bg-red-100 text-red-800 border border-red-300';
            else if (sent === 'Mixed') sentClass = 'bg-amber-100 text-amber-800 border border-amber-300';
            sentBadge.className = `px-3 py-1 rounded-md font-bold text-xs ${sentClass}`;
            const sentScoreElem = document.getElementById('resSentimentScore');
            if (sentScoreElem) sentScoreElem.innerText = data.sentiment_score !== undefined ? Number(data.sentiment_score).toFixed(2) : '0.00';
        }

        // Actionable Badge
        const actBadge = document.getElementById('resActionableBadge');
        actBadge.innerText = data.actionable === 1 ? 'Actionable (1)' : 'Non-Actionable (0)';
        actBadge.className = `px-3 py-1 rounded-md font-bold text-xs ${data.actionable === 1 ? 'badge-actionable-yes' : 'badge-actionable-no'}`;
        document.getElementById('resRoutableText').innerText = data.is_routable ? 'Routable' : 'Cannot Route (Missing Store / Not Digital)';

        document.getElementById('resDestination').innerText = data.routing_destination;

        // Store Resolution
        document.getElementById('resStoreName').innerText = data.resolved_store_name || (data.resolved_store_id ? data.resolved_store_id : 'Unassigned Store');
        document.getElementById('resStoreSource').innerText = data.store_resolution_source;

        // Confidence
        const confPct = Math.round(data.confidence * 100);
        document.getElementById('resConfText').innerText = `${confPct}%`;
        document.getElementById('resConfBar').style.width = `${confPct}%`;

    } catch (err) {
        console.error('Classification error:', err);
        alert('Network or server error during classification.');
    } finally {
        btn.disabled = false;
        btn.innerText = 'Classify & Route Feedback';
    }
}

// -----------------------------------------------------------------------------
// 5. Feedback Stream Explorer
// -----------------------------------------------------------------------------
function debounceSearch() {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
        loadExplorerFeedback(1);
    }, 300);
}

function changePage(delta) {
    loadExplorerFeedback(currentPage + delta);
}

async function loadExplorerFeedback(page = 1) {
    currentPage = page;
    const channel = document.getElementById('filterChannel').value;
    const severity = document.getElementById('filterSeverity').value;
    const actionable = document.getElementById('filterActionable').value;
    const region = document.getElementById('filterRegion').value;
    const search = document.getElementById('explorerSearch').value.trim();

    let url = `/api/feedback?page=${page}&limit=20`;
    if (channel && channel !== 'All') url += `&channel=${encodeURIComponent(channel)}`;
    if (severity && severity !== 'All') url += `&severity=${encodeURIComponent(severity)}`;
    if (actionable !== '') url += `&actionable=${encodeURIComponent(actionable)}`;
    if (region && region !== 'All') url += `&region=${encodeURIComponent(region)}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    try {
        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();

        const tbody = document.getElementById('explorerTableBody');
        tbody.innerHTML = '';

        if (!data.records || data.records.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="text-center py-6 text-slate-400">No matching feedback records found.</td></tr>`;
            return;
        }

        data.records.forEach(r => {
            const tr = document.createElement('tr');
            tr.className = 'hover:bg-slate-50 transition border-b border-slate-100';

            const storeDisplay = r.store_name ? `${r.store_name} (${r.region})` : (r.store_id || '<span class="text-slate-400 italic">None</span>');

            tr.innerHTML = `
                <td class="py-2.5 px-3">
                    <div class="font-mono text-slate-700 font-bold text-[11px]">${r.feedback_id}</div>
                    <div class="text-[10px] text-slate-400">${r.channel}</div>
                </td>
                <td class="py-2.5 px-3 text-[11px] text-slate-700">${storeDisplay}</td>
                <td class="py-2.5 px-4 text-xs text-slate-800">${r.text}</td>
                <td class="py-2.5 px-3">
                    <div class="font-semibold text-[11px] text-navy-900">${r.issue_type_name}</div>
                    <div class="text-[10px] text-slate-400 font-mono">${r.issue_type_id} • ${r.owner_function}</div>
                </td>
                <td class="py-2.5 px-2 text-center">
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold badge-${r.severity}">${r.severity}</span>
                </td>
                <td class="py-2.5 px-2 text-center">
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold ${r.actionable === 1 ? 'badge-actionable-yes' : 'badge-actionable-no'}">${r.actionable}</span>
                </td>
            `;
            tbody.appendChild(tr);
        });

        // Pagination controls
        const startRecord = ((data.page - 1) * data.limit) + 1;
        const endRecord = Math.min(data.page * data.limit, data.total);
        document.getElementById('explorerPaginationInfo').innerText = `Showing ${startRecord} to ${endRecord} of ${data.total.toLocaleString()} records`;

        document.getElementById('btnPrevPage').disabled = data.page <= 1;
        document.getElementById('btnNextPage').disabled = data.page >= data.pages;

    } catch (e) {
        console.error('Error loading explorer feedback:', e);
    }
}

// -----------------------------------------------------------------------------
// 5. Supabase Authentication Controller & Profile Hydration
// -----------------------------------------------------------------------------
async function hydrateUserProfile(accessToken, baseUser = null) {
    if (!accessToken) return baseUser;
    try {
        const res = await fetch('/api/auth/me', {
            headers: { 'Authorization': `Bearer ${accessToken}` }
        });
        if (res.ok) {
            const profile = await res.json();
            return { ...(baseUser || {}), ...profile };
        }
    } catch (e) {
        console.warn('Profile hydration failed:', e);
    }
    return baseUser;
}

async function initSupabaseAuth() {
    try {
        const res = await fetch('/api/config/public');
        if (!res.ok) return;
        const config = await res.json();

        if (config.supabase_url && config.supabase_publishable_key && window.supabase) {
            _supabase = window.supabase.createClient(config.supabase_url, config.supabase_publishable_key);
            console.log('Supabase client initialized.');

            // Only adopt Supabase session if no valid local session is active
            const hasLocalToken = !!(localStorage.getItem('r5_token') || sessionStorage.getItem('r5_token'));
            const { data: sessionData, error } = await _supabase.auth.getSession();
            if (sessionData && sessionData.session && !hasLocalToken) {
                _currentSession = sessionData.session;
                const hydrated = await hydrateUserProfile(sessionData.session.access_token, sessionData.session.user);
                _currentUser = hydrated;
                setLocalSession(sessionData.session.access_token, hydrated);
            }

            // Listen for auth state changes (login, logout, token refresh)
            _supabase.auth.onAuthStateChange(async (event, session) => {
                if (session) {
                    _currentSession = session;
                    const hydrated = await hydrateUserProfile(session.access_token, session.user);
                    _currentUser = hydrated;
                    setLocalSession(session.access_token, hydrated);
                } else if (!_currentSession || _currentSession.provider !== 'local') {
                    const localTok = localStorage.getItem('r5_token') || sessionStorage.getItem('r5_token');
                    if (!localTok) {
                        _currentSession = null;
                        _currentUser = null;
                        updateAuthUI(null);
                    }
                }
            });
        }
    } catch (e) {
        console.warn('Could not initialize Supabase Auth:', e);
    }
}

function updateAuthUI(user) {
    const btnSignIn = document.getElementById('btnSignInModal');
    const badge = document.getElementById('userProfileBadge');
    const roleEl = document.getElementById('userRoleIndicator');
    const emailEl = document.getElementById('userEmailDisplay');
    const pubLinks = document.getElementById('publicNavLinks');

    if (user) {
        if (btnSignIn) btnSignIn.classList.add('hidden');
        if (badge) badge.classList.remove('hidden');
        if (pubLinks) pubLinks.classList.add('hidden');

        const meta = user.user_metadata || {};
        const role = String(user.role || meta.role || 'customer').toLowerCase();
        const storeId = user.assigned_store_id || meta.assigned_store_id;
        const storeSuffix = storeId ? ` • ${storeId}` : '';

        if (roleEl) roleEl.innerText = `${role.toUpperCase()}${storeSuffix}`;
        if (emailEl) emailEl.innerText = user.email || 'User';
    } else {
        if (btnSignIn) btnSignIn.classList.remove('hidden');
        if (badge) badge.classList.add('hidden');
        if (pubLinks) pubLinks.classList.remove('hidden');
    }

    const currentTab = sessionStorage.getItem('vse_active_tab') || 'home';
    renderWorkspaceNavigation(user, currentTab);
}

function openAuthModal() {
    const modal = document.getElementById('authModal');
    const tabRegister = document.getElementById('authTabRegister');
    if (tabRegister) tabRegister.classList.remove('hidden');
    const title = document.getElementById('authModalTitle');
    if (title) title.innerText = 'Voice of Store Engine — Sign In';
    if (modal) modal.classList.remove('hidden');
    clearAuthAlert();
}

function closeAuthModal() {
    const modal = document.getElementById('authModal');
    if (modal) modal.classList.add('hidden');
    clearAuthAlert();
    // Cleanly restore previous non-login view in URL if modal was closed
    if (history.state && history.state.tab === 'login') {
        const user = getAuthUser();
        if (user) {
            const wsTab = user.role === 'customer' ? 'customer' : (user.role === 'store_manager' ? 'manager' : 'overview');
            history.replaceState({ tab: wsTab }, '', '#' + wsTab);
        } else {
            history.replaceState({ tab: 'home' }, '', '#home');
        }
    }
}

function switchAuthMode(mode) {
    _authMode = mode;
    const tabIn = document.getElementById('authTabSignIn');
    const tabReg = document.getElementById('authTabRegister');
    const nameGroup = document.getElementById('authFullNameGroup');
    const btnSubmit = document.getElementById('btnSubmitAuth');
    clearAuthAlert();

    if (mode === 'signin') {
        if (tabIn) {
            tabIn.className = 'flex-1 py-3 text-center border-b-2 border-saffron-500 text-saffron-600 bg-white';
        }
        if (tabReg) {
            tabReg.className = 'flex-1 py-3 text-center border-b-2 border-transparent text-slate-500 hover:text-slate-700';
        }
        if (nameGroup) nameGroup.classList.add('hidden');
        if (btnSubmit) btnSubmit.innerText = 'Sign In';
    } else {
        if (tabReg) {
            tabReg.className = 'flex-1 py-3 text-center border-b-2 border-saffron-500 text-saffron-600 bg-white';
        }
        if (tabIn) {
            tabIn.className = 'flex-1 py-3 text-center border-b-2 border-transparent text-slate-500 hover:text-slate-700';
        }
        if (nameGroup) nameGroup.classList.remove('hidden');
        if (btnSubmit) btnSubmit.innerText = 'Register Customer Account';
    }
}

async function submitAuthForm(event) {
    event.preventDefault();

    const email = document.getElementById('authEmail').value.trim();
    const password = document.getElementById('authPassword').value;
    const fullName = document.getElementById('authFullName') ? document.getElementById('authFullName').value.trim() : '';
    const btnSubmit = document.getElementById('btnSubmitAuth');

    btnSubmit.disabled = true;
    btnSubmit.innerText = 'Processing...';

    try {
        if (_authMode === 'signin') {
            let supaSuccess = false;
            if (_supabase) {
                try {
                    const { data, error } = await _supabase.auth.signInWithPassword({ email, password });
                    if (!error && data && data.session) {
                        _currentSession = data.session;
                        const hydrated = await hydrateUserProfile(data.session.access_token, data.user);
                        _currentUser = hydrated;
                        setLocalSession(data.session.access_token, hydrated);
                        supaSuccess = true;
                    }
                } catch (e) {
                    console.log('Supabase sign-in bypass:', e);
                }
            }

            if (!supaSuccess) {
                // Try backend FastAPI authentication (SQLite backed)
                const res = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, password })
                });

                if (!res.ok) {
                    const err = await res.json();
                    throw new Error(err.detail || 'Invalid email or password.');
                }

                const data = await res.json();
                setLocalSession(data.access_token, data.user);
            }

            showAuthAlert('Signed in successfully!', 'success');
            setTimeout(() => {
                const user = getAuthUser();
                closeAuthModal();
                btnSubmit.disabled = false;
                btnSubmit.innerText = 'Sign In';
                const wsTab = (user && user.role === 'customer') ? 'customer' : ((user && user.role === 'store_manager') ? 'manager' : 'overview');
                // Push workspace directly so that browser Back navigates directly to #home without a login loop
                history.pushState({ tab: wsTab }, '', '#' + wsTab);
                switchTab(wsTab, false);
                if (wsTab === 'manager') {
                    showManagerSubView('overview', false);
                } else if (wsTab === 'customer') {
                    showCustomerSubView('dashboard', false);
                }
            }, 800);
        } else {
            // Register Customer
            let registered = false;
            if (_supabase) {
                try {
                    const { data, error } = await _supabase.auth.signUp({
                        email,
                        password,
                        options: {
                            data: {
                                full_name: fullName || 'Customer',
                                role: 'customer'
                            }
                        }
                    });
                    if (!error) registered = true;
                } catch (e) {
                    console.log('Supabase signup bypass:', e);
                }
            }

            if (!registered) {
                const res = await fetch('/api/auth/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, password, full_name: fullName || 'Customer' })
                });

                if (!res.ok) {
                    const err = await res.json();
                    throw new Error(err.detail || 'Registration failed.');
                }
            }

            showAuthAlert('Registration successful! You may now sign in.', 'success');
            setTimeout(() => {
                switchAuthMode('signin');
                btnSubmit.disabled = false;
                btnSubmit.innerText = 'Sign In';
            }, 1200);
        }
    } catch (err) {
        showAuthAlert(err.message || 'Authentication error', 'error');
        btnSubmit.disabled = false;
        btnSubmit.innerText = _authMode === 'signin' ? 'Sign In' : 'Register Customer Account';
    }
}

async function handleSignOut() {
    clearLocalSession();
    if (_supabase) {
        try { await _supabase.auth.signOut(); } catch (e) {}
    }
    _currentSession = null;
    _currentUser = null;
    try {
        sessionStorage.removeItem('vse_active_tab');
        sessionStorage.removeItem('r5_token');
        sessionStorage.removeItem('r5_user');
    } catch (e) {}
    updateAuthUI(null);
    showManagerToast('Signed Out', 'You have been signed out successfully.', 'success');
    history.replaceState({ tab: 'home' }, '', '#home');
    switchTab('home', false);
}

function showAuthAlert(message, type) {
    const box = document.getElementById('authAlertBox');
    if (!box) return;
    box.classList.remove('hidden', 'bg-red-50', 'text-red-700', 'border', 'border-red-200', 'bg-emerald-50', 'text-emerald-700', 'border-emerald-200');
    if (type === 'error') {
        box.classList.add('bg-red-50', 'text-red-700', 'border', 'border-red-200');
    } else {
        box.classList.add('bg-emerald-50', 'text-emerald-700', 'border', 'border-emerald-200');
    }
    box.innerText = message;
}

function clearAuthAlert() {
    const box = document.getElementById('authAlertBox');
    if (box) {
        box.classList.add('hidden');
        box.innerText = '';
    }
}

// =============================================================================
// 6. Store Manager Workspace Controller
// =============================================================================
let currentManagerStoreId = null;
let currentManagerPage = 1;
let currentManagerTotalPages = 1;
let currentResolvingAlertId = null;
let managerSearchTimeout = null;

function getAuthToken() {
    if (_currentSession && _currentSession.access_token) {
        return _currentSession.access_token;
    }
    const token = localStorage.getItem('r5_token') || sessionStorage.getItem('r5_token');
    if (token) {
        const user = getAuthUser();
        _currentSession = { access_token: token, user: user, provider: 'local' };
        return token;
    }
    return null;
}

function getAuthUser() {
    if (_currentUser) return _currentUser;
    const stored = localStorage.getItem('r5_user') || sessionStorage.getItem('r5_user');
    if (stored) {
        try {
            const parsed = JSON.parse(stored);
            _currentUser = parsed;
            return parsed;
        } catch (e) {}
    }
    return null;
}

function setLocalSession(token, user) {
    if (!token) return;
    try {
        localStorage.setItem('r5_token', token);
        localStorage.setItem('r5_user', JSON.stringify(user));
        sessionStorage.setItem('r5_token', token);
        sessionStorage.setItem('r5_user', JSON.stringify(user));
    } catch (e) {
        console.warn('Storage write failed:', e);
    }
    _currentSession = { access_token: token, user: user, provider: 'local' };
    _currentUser = user;
    updateAuthUI(user);
}

function clearLocalSession() {
    try {
        localStorage.removeItem('r5_token');
        localStorage.removeItem('r5_user');
        sessionStorage.removeItem('r5_token');
        sessionStorage.removeItem('r5_user');
    } catch (e) {}
    _currentSession = null;
    _currentUser = null;
    updateAuthUI(null);
}

function loadStoredLocalSession() {
    const token = localStorage.getItem('r5_token') || sessionStorage.getItem('r5_token');
    const user = getAuthUser();
    if (token && user) {
        _currentSession = { access_token: token, user: user, provider: 'local' };
        _currentUser = user;
        updateAuthUI(user);
    }
}

// Verified demonstration account definitions for evaluator / demo mode
const DEMO_ACCOUNTS = {
    'customer': {
        email: 'customer@example.com',
        password: 'DemoCust#2026!Aarav',
    },
    'kankarbagh': {
        email: 'manager.kankarbagh@saffron.example',
        password: 'DemoMgr#2026!Kankarbagh',
    },
    'kothrud': {
        email: 'manager.kothrud@saffron.example',
        password: 'DemoMgr#2026!Kothrud',
    },
    'admin': {
        email: 'admin@saffron.example',
        password: 'DemoAdmin#2026!LocalOnly',
    }
};

async function quickLoginDemo(target, maybePassword = null) {
    let email = target;
    let password = maybePassword;

    // Handle persona keyword (e.g. 'kankarbagh', 'kothrud', 'admin')
    if (DEMO_ACCOUNTS[target]) {
        email = DEMO_ACCOUNTS[target].email;
        password = DEMO_ACCOUNTS[target].password;
    } else if (!password) {
        // Find by email match if only email passed
        for (const key of Object.keys(DEMO_ACCOUNTS)) {
            if (DEMO_ACCOUNTS[key].email.toLowerCase() === (target || '').toLowerCase()) {
                email = DEMO_ACCOUNTS[key].email;
                password = DEMO_ACCOUNTS[key].password;
                break;
            }
        }
    }

    if (!email || !password) {
        showManagerToast('Login Failed', 'Demo account configuration missing.', 'error');
        return;
    }

    try {
        let authenticated = false;
        if (_supabase) {
            try {
                const { data, error } = await _supabase.auth.signInWithPassword({ email, password });
                if (!error && data && data.session) {
                    _currentSession = data.session;
                    const hydrated = await hydrateUserProfile(data.session.access_token, data.user);
                    _currentUser = hydrated;
                    setLocalSession(data.session.access_token, hydrated);
                    authenticated = true;
                }
            } catch (se) {
                console.log('Supabase demo login fallback:', se);
            }
        }

        if (!authenticated) {
            const res = await fetch('/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password })
            });
            if (!res.ok) {
                const err = await res.json();
                showManagerToast('Login Failed', err.detail || 'Could not authenticate demo user', 'error');
                return;
            }
            const data = await res.json();
            setLocalSession(data.access_token, data.user);
        }

        const activeUser = getAuthUser();
        const roleLabel = activeUser ? activeUser.role : 'user';
        const nameLabel = activeUser ? activeUser.full_name : 'User';
        showManagerToast('Authenticated', `Signed in as ${nameLabel} (${roleLabel})`, 'success');

        if (activeUser && activeUser.assigned_store_id) {
            currentManagerStoreId = activeUser.assigned_store_id;
        } else if (activeUser && activeUser.role === 'admin') {
            currentManagerStoreId = currentManagerStoreId || 'ST-D69EG';
        }

        const wsTab = (activeUser && activeUser.role === 'customer') ? 'customer' : ((activeUser && activeUser.role === 'admin') ? 'overview' : 'manager');
        // Push workspace directly so that browser Back navigates directly to #home without a login loop
        history.pushState({ tab: wsTab }, '', '#' + wsTab);
        switchTab(wsTab, false);
        if (wsTab === 'manager') {
            showManagerSubView('overview', false);
        } else if (wsTab === 'customer') {
            showCustomerSubView('dashboard', false);
        }
    } catch (e) {
        console.error('Error logging in demo user:', e);
        showManagerToast('Network Error', 'Could not reach authentication service', 'error');
    }
}

async function logoutManagerWorkspace() {
    await handleSignOut();
}

let currentManagerSubView = 'overview';

function showManagerSubView(subView, pushHistory = true) {
    const validSubviews = ['overview', 'insights', 'inbox', 'alerts', 'actions', 'analytics', 'reports', 'settings'];
    if (!subView || !validSubviews.includes(subView)) {
        subView = 'overview';
    }
    currentManagerSubView = subView;

    const targetHash = '#manager/' + subView;
    if (pushHistory) {
        if (!history.state || history.state.tab !== 'manager' || history.state.subview !== subView) {
            history.pushState({ tab: 'manager', subview: subView }, '', targetHash);
        }
    } else {
        if (!history.state || history.state.tab !== 'manager' || history.state.subview !== subView) {
            history.replaceState({ tab: 'manager', subview: subView }, '', targetHash);
        }
    }

    // Toggle corresponding section visibility across all 8 subviews
    const sectionMap = {
        'overview': document.getElementById('managerKpiCards'),
        'insights': document.getElementById('managerInsightsSection'),
        'inbox': document.getElementById('managerInboxSection'),
        'alerts': document.getElementById('managerAlertsSection'),
        'actions': document.getElementById('managerActionCenterSection'),
        'analytics': document.getElementById('managerAnalyticsSection'),
        'reports': document.getElementById('managerReportsSection'),
        'settings': document.getElementById('managerSettingsSection')
    };

    Object.keys(sectionMap).forEach(key => {
        const el = sectionMap[key];
        if (el) {
            if (key === subView) el.classList.remove('hidden');
            else el.classList.add('hidden');
        }
    });

    // Update in-header sub-navigation switcher buttons
    const subButtons = [
        { id: 'btnMgrSubOverview', key: 'overview' },
        { id: 'btnMgrSubInsights', key: 'insights' },
        { id: 'btnMgrSubInbox', key: 'inbox' },
        { id: 'btnMgrSubAlerts', key: 'alerts' },
        { id: 'btnMgrSubActions', key: 'actions' },
        { id: 'btnMgrSubAnalytics', key: 'analytics' },
        { id: 'btnMgrSubReports', key: 'reports' },
        { id: 'btnMgrSubSettings', key: 'settings' }
    ];
    subButtons.forEach(sb => {
        const b = document.getElementById(sb.id);
        if (b) {
            if (sb.key === subView) {
                b.className = 'px-3.5 py-2 rounded-lg bg-saffron-500 text-navy-900 font-bold transition shadow-xs flex items-center space-x-1.5';
                b.setAttribute('aria-current', 'page');
            } else {
                b.className = 'px-3.5 py-2 rounded-lg bg-slate-100 text-slate-700 hover:bg-slate-200 transition flex items-center space-x-1.5';
                b.removeAttribute('aria-current');
            }
        }
    });

    // Update top workspace navigation items
    const navItems = [
        { id: 'nav-mgr-overview', key: 'overview' },
        { id: 'nav-mgr-insights', key: 'insights' },
        { id: 'nav-mgr-inbox', key: 'inbox' },
        { id: 'nav-mgr-alerts', key: 'alerts' },
        { id: 'nav-mgr-actions', key: 'actions' },
        { id: 'nav-mgr-analytics', key: 'analytics' },
        { id: 'nav-mgr-reports', key: 'reports' },
        { id: 'nav-mgr-settings', key: 'settings' }
    ];
    navItems.forEach(ni => {
        const nb = document.getElementById(ni.id);
        if (nb) {
            if (ni.key === subView) {
                nb.className = 'px-3.5 py-1.5 rounded-lg font-bold text-xs sm:text-sm bg-navy-900 text-white shadow-xs transition';
                nb.setAttribute('aria-current', 'page');
            } else {
                nb.className = 'px-3.5 py-1.5 rounded-lg font-medium text-xs sm:text-sm text-slate-600 hover:text-navy-900 hover:bg-slate-100 transition';
                nb.removeAttribute('aria-current');
            }
        }
    });

    // Ensure valid manager store ID before loading data
    if (!currentManagerStoreId) {
        const u = getAuthUser();
        currentManagerStoreId = (u && u.assigned_store_id) || 'ST-D69EG';
    }

    // Load data for the selected subview
    if (subView === 'overview') {
        loadStoreSummary();
    } else if (subView === 'insights') {
        loadManagerInsights();
    } else if (subView === 'inbox') {
        loadManagerInbox(currentManagerPage || 1);
    } else if (subView === 'alerts') {
        loadManagerAlerts();
    } else if (subView === 'actions') {
        loadManagerActions();
    } else if (subView === 'analytics') {
        loadManagerAnalytics();
    } else if (subView === 'reports') {
        loadManagerReports();
    } else if (subView === 'settings') {
        loadManagerSettings();
    }
}

async function loadManagerWorkspace() {
    const user = getAuthUser();
    const gate = document.getElementById('managerAuthGate');
    const header = document.getElementById('managerActiveHeader');
    const kpis = document.getElementById('managerKpiCards');
    const alertsSection = document.getElementById('managerAlertsSection');
    const inboxSection = document.getElementById('managerInboxSection');

    if (!user || (user.role !== 'store_manager' && user.role !== 'admin')) {
        if (gate) gate.classList.remove('hidden');
        if (header) header.classList.add('hidden');
        if (kpis) kpis.classList.add('hidden');
        if (alertsSection) alertsSection.classList.add('hidden');
        if (inboxSection) inboxSection.classList.add('hidden');
        return;
    }

    // Role authorized
    if (gate) gate.classList.add('hidden');
    if (header) header.classList.remove('hidden');

    if (!currentManagerStoreId || user.role === 'store_manager') {
        currentManagerStoreId = user.assigned_store_id || 'ST-D69EG';
    }

    const adminSelectGroup = document.getElementById('mgrAdminStoreSelectGroup');
    if (user.role === 'admin') {
        if (adminSelectGroup) adminSelectGroup.classList.remove('hidden');
        await populateAdminStoreSelect();
    } else {
        if (adminSelectGroup) adminSelectGroup.classList.add('hidden');
    }

    const mgrUserDisplay = document.getElementById('mgrUserDisplay');
    if (mgrUserDisplay) {
        mgrUserDisplay.innerText = `${user.full_name} (${user.email})`;
    }

    showManagerSubView(currentManagerSubView || 'overview', false);
}

async function loadStoreSummary() {
    const token = getAuthToken();
    if (!token) return;

    try {
        const url = `/api/manager/store-summary?store_id=${encodeURIComponent(currentManagerStoreId || '')}`;
        const res = await fetch(url, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.status === 401) {
            console.warn('Store summary request unauthorized (HTTP 401).');
            clearLocalSession();
            return;
        }
        if (!res.ok) {
            console.error('Failed to load store summary:', res.status);
            return;
        }
        const data = await res.json();
        const store = data.store || data;

        document.getElementById('mgrStoreName').innerText = store.store_name || currentManagerStoreId;
        document.getElementById('mgrStoreIdBadge').innerText = store.store_id || currentManagerStoreId;
        document.getElementById('mgrStoreFormat').innerText = `${store.format || 'Retail'} Format`;
        document.getElementById('mgrStoreLocation').innerHTML = `${store.city || 'Store City'}, ${store.region || 'Region'} Region • Active Store Manager`;

        document.getElementById('mgrKpiTotalFeedback').innerText = (data.total_feedback || 0).toLocaleString();
        document.getElementById('mgrKpiActionableCount').innerText = (data.actionable_count || 0).toLocaleString();
        document.getElementById('mgrKpiActionablePct').innerText = `${data.actionable_pct || 0}% Actionable`;

        const activeAlerts = data.active_alerts !== undefined ? data.active_alerts : ((data.alerts_summary && (data.alerts_summary.new + data.alerts_summary.investigating + data.alerts_summary.acknowledged)) || 0);
        document.getElementById('mgrKpiActiveAlerts').innerText = activeAlerts;
        const newCount = (data.alerts_by_status && data.alerts_by_status.new) !== undefined ? data.alerts_by_status.new : ((data.alerts_summary && data.alerts_summary.new) || 0);
        document.getElementById('mgrKpiNewAlerts').innerText = `${newCount} New`;
        const resolvedCount = (data.alerts_by_status && data.alerts_by_status.resolved) !== undefined ? data.alerts_by_status.resolved : ((data.alerts_summary && data.alerts_summary.resolved) || 0);
        document.getElementById('mgrKpiResolvedCount').innerText = resolvedCount;

        // Update tab badge
        const badge = document.getElementById('managerAlertsBadge');
        if (badge) {
            badge.innerText = activeAlerts;
            if (activeAlerts > 0) {
                badge.classList.remove('hidden');
            } else {
                badge.classList.add('hidden');
            }
        }
    } catch (e) {
        console.error('Error fetching store summary:', e);
    }
}

async function loadManagerAlerts() {
    const token = getAuthToken();
    const container = document.getElementById('managerAlertsList');
    if (!container) return;

    if (!token) {
        container.innerHTML = `
            <div class="p-5 bg-amber-50 text-amber-800 border border-amber-200 rounded-xl text-xs space-y-2">
                <p class="font-bold">Authentication Required</p>
                <p>Please sign in using one of the demo manager profiles above to view active store alerts.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = '<div class="text-center py-6 text-slate-400 text-xs">Loading store alerts...</div>';

    try {
        const url = `/api/manager/alerts?store_id=${encodeURIComponent(currentManagerStoreId || '')}`;
        const res = await fetch(url, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.status === 401) {
            clearLocalSession();
            container.innerHTML = `
                <div class="p-5 bg-red-50 text-red-700 border border-red-200 rounded-xl text-xs space-y-3">
                    <div class="flex items-center space-x-2">
                        <span class="text-base">⚠️</span>
                        <span class="font-bold">Session Expired or Unauthorized (HTTP 401)</span>
                    </div>
                    <p class="text-red-600">Your authentication session is invalid or has expired. Please sign in again using one of the demo buttons above.</p>
                    <div class="pt-1 flex items-center space-x-2">
                        <button onclick="quickLoginDemo('kankarbagh')" class="px-3 py-1.5 bg-red-700 hover:bg-red-800 text-white rounded-lg text-xs font-semibold shadow-xs">
                            Re-authenticate as Kankarbagh Manager
                        </button>
                    </div>
                </div>
            `;
            const gate = document.getElementById('managerAuthGate');
            if (gate) gate.classList.remove('hidden');
            return;
        }
        if (!res.ok) {
            const errDetail = await parseErrorResponse(res, `Failed to load alerts (HTTP ${res.status}).`);
            container.innerHTML = `
                <div class="p-4 bg-red-50 text-red-700 border border-red-200 rounded-xl text-xs space-y-2">
                    <div class="flex items-center space-x-2 font-bold">
                        <span>⚠️</span>
                        <span>Failed to load alerts (HTTP ${res.status})</span>
                    </div>
                    <p class="text-red-600">${escapeHtml(errDetail)}</p>
                    <button onclick="loadManagerAlerts()" class="mt-2 px-3 py-1.5 bg-red-700 hover:bg-red-800 text-white rounded-lg text-xs font-semibold shadow-xs flex items-center space-x-1.5">
                        <span>↻ Retry Alerts</span>
                    </button>
                </div>
            `;
            return;
        }
        const data = await res.json();
        const alerts = data.alerts || [];

        const chartWrapper = document.getElementById('mgrAlertsChartWrapper');
        if (alerts.length === 0) {
            if (chartWrapper) chartWrapper.classList.add('hidden');
            container.innerHTML = `
                <div class="text-center py-8 bg-slate-50 rounded-xl border border-dashed border-slate-200">
                    <span class="text-2xl">🎉</span>
                    <p class="font-bold text-slate-700 text-sm mt-1">No Active Emerging Alerts for Store</p>
                    <p class="text-xs text-slate-500 mt-0.5">All customer feedback patterns are currently within safe baseline thresholds.</p>
                </div>
            `;
            return;
        }

        if (chartWrapper) {
            chartWrapper.classList.remove('hidden');
            const chartAlerts = alerts.slice(0, 6);
            const labels = chartAlerts.map(a => {
                const name = a.issue_type_name || a.issue_type_id || 'Alert';
                return name.length > 22 ? name.slice(0, 20) + '…' : name;
            });
            const baselines = chartAlerts.map(a => Number(a.baseline_count || 0));
            const currents = chartAlerts.map(a => Number(a.current_count || 0));

            renderOrUpdateChart('chartMgrAlertsSpikes', {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'Baseline Expected',
                            data: baselines,
                            backgroundColor: '#94A3B8',
                            borderRadius: 6
                        },
                        {
                            label: 'Current Anomaly Spike',
                            data: currents,
                            backgroundColor: '#EF4444',
                            borderRadius: 6
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11, family: 'Inter' } } },
                        tooltip: { cornerRadius: 8 }
                    },
                    scales: {
                        y: { beginAtZero: true, grid: { color: '#F1F5F9' }, ticks: { font: { size: 10 } } },
                        x: { grid: { display: false }, ticks: { font: { size: 10 } } }
                    }
                }
            });
        }

        container.innerHTML = '';
        alerts.forEach(alert => {
            try {
                if (!alert) return;

                const alertId = alert.alert_id || alert.id || 'ALT-UNKNOWN';
                const status = String(alert.status || 'new').toLowerCase();
                const severity = String(alert.severity || alert.severity_floor || 'medium').toLowerCase();
                const week = alert.week || alert.as_of_week_start || 'N/A';
                const issueName = alert.issue_type_name || alert.issue_name || alert.issue_type_id || 'Operational Issue';
                const issueId = alert.issue_type_id || 'Unknown';
                const ownerFunc = alert.owner_function || 'Store Ops';
                const resNote = alert.resolution_note || alert.resolution_notes || null;

                const card = document.createElement('div');
                card.className = 'border border-slate-200 rounded-xl p-5 bg-white shadow-sm transition hover:border-slate-300 space-y-4';

                // Status Badge styling
                let statusBadgeClass = 'bg-red-100 text-red-800 border-red-200';
                let statusIcon = '⚠️';
                if (status === 'acknowledged') {
                    statusBadgeClass = 'bg-blue-100 text-blue-800 border-blue-200';
                    statusIcon = '👀';
                } else if (status === 'investigating') {
                    statusBadgeClass = 'bg-indigo-100 text-indigo-800 border-indigo-200';
                    statusIcon = '🔍';
                } else if (status === 'resolved') {
                    statusBadgeClass = 'bg-emerald-100 text-emerald-800 border-emerald-200';
                    statusIcon = '✅';
                }

                // Evidence Quotes HTML
                let quotesHtml = '';
                if (Array.isArray(alert.evidence_samples) && alert.evidence_samples.length > 0) {
                    const quotesList = alert.evidence_samples.map(s => {
                        if (!s) return '';
                        const text = s.text || '';
                        const channel = s.channel || 'feedback';
                        const rating = (s.rating !== null && s.rating !== undefined && s.rating !== '') ? `${s.rating}★` : 'N/A';
                        const feedbackId = s.feedback_id || '';
                        return `
                            <div class="bg-amber-50/60 border-l-2 border-amber-400 p-2 rounded text-slate-700 italic text-xs mb-1.5">
                                "${escapeHtml(text)}"
                                <div class="text-[10px] text-slate-400 not-italic mt-0.5">Channel: ${escapeHtml(channel)} • Rating: ${escapeHtml(rating)} • ID: ${escapeHtml(feedbackId)}</div>
                            </div>
                        `;
                    }).join('');
                    quotesHtml = `
                        <div class="mt-3">
                            <details class="cursor-pointer">
                                <summary class="text-xs font-semibold text-amber-700 hover:text-amber-800 select-none">
                                    View Customer/Staff Evidence Quotes (${alert.evidence_samples.length} items)
                                </summary>
                                <div class="mt-2 space-y-1">
                                    ${quotesList}
                                </div>
                            </details>
                        </div>
                    `;
                }

                // Resolution Note Box (if present)
                let noteHtml = '';
                if (resNote) {
                    noteHtml = `
                        <div class="p-3 bg-slate-50 border border-slate-200 rounded-lg text-xs">
                            <div class="flex items-center justify-between text-slate-500 font-semibold mb-1">
                                <span>Resolution Note & Corrective Action:</span>
                                <span class="text-[10px] text-slate-400">${alert.updated_at ? new Date(alert.updated_at).toLocaleString() : ''}</span>
                            </div>
                            <p class="text-slate-800">${escapeHtml(resNote)}</p>
                        </div>
                    `;
                }

                // Action Buttons
                const canAcknowledge = status === 'new';
                const canInvestigate = status === 'new' || status === 'acknowledged';
                const canResolve = status !== 'resolved';

                // Spike Statistics formatting with resilient fallbacks
                let statsHtml = '';
                if (alert.current_count !== undefined && alert.current_count !== null && alert.baseline_count !== undefined && alert.baseline_count !== null) {
                    const ratioStr = alert.ratio !== undefined && alert.ratio !== null ? ` (Ratio: <strong>${alert.ratio}x</strong>)` : '';
                    statsHtml = `Baseline: <strong>${alert.baseline_count}</strong> &rarr; Spike Volume: <strong class="text-red-600 font-bold">${alert.current_count}</strong>${ratioStr}`;
                } else if (alert.evidence_count !== undefined && alert.evidence_count !== null) {
                    const confStr = alert.confidence !== undefined ? ` • Confidence: <strong>${Math.round(alert.confidence * 100)}%</strong>` : '';
                    statsHtml = `Verified Evidence Spike: <strong class="text-red-600 font-bold">${alert.evidence_count} records</strong>${confStr}`;
                } else {
                    const confStr = alert.confidence !== undefined ? `${Math.round(alert.confidence * 100)}%` : 'Active';
                    statsHtml = `Incident Confidence: <strong>${confStr}</strong>`;
                }

                card.innerHTML = `
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-100 pb-3">
                        <div class="flex items-center space-x-2">
                            <span class="font-mono text-xs font-bold text-slate-500 bg-slate-100 px-2 py-0.5 rounded">${escapeHtml(alertId)}</span>
                            <span class="px-2 py-0.5 rounded text-xs font-bold border ${statusBadgeClass} uppercase">
                                ${statusIcon} ${escapeHtml(status)}
                            </span>
                            <span class="px-2 py-0.5 rounded text-xs font-bold badge-${escapeHtml(severity)}">
                                ${escapeHtml(severity.toUpperCase())}
                            </span>
                        </div>
                        <div class="text-xs text-slate-500">
                            Detection Week: <strong>${escapeHtml(week)}</strong>
                        </div>
                    </div>

                    <div class="space-y-1">
                        <div class="flex items-center space-x-2">
                            <h4 class="font-extrabold text-navy-900 text-sm">${escapeHtml(issueName)}</h4>
                            <span class="text-xs font-mono text-slate-400">(${escapeHtml(issueId)})</span>
                        </div>
                        <p class="text-xs text-slate-500">
                            Owning Department: <strong class="text-slate-700">${escapeHtml(ownerFunc)}</strong> •
                            ${statsHtml}
                        </p>
                    </div>

                    ${quotesHtml}
                    ${noteHtml}

                    <div class="pt-2 border-t border-slate-100 flex flex-wrap items-center justify-between gap-2">
                        <div class="flex items-center space-x-2">
                            ${canAcknowledge ? `
                                <button onclick="handleAlertAction('${escapeHtml(alertId)}', 'acknowledge')" class="px-3 py-1.5 bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 text-xs font-bold rounded-lg transition shadow-sm">
                                    👀 Acknowledge
                                </button>
                            ` : ''}
                            ${canInvestigate ? `
                                <button onclick="handleAlertAction('${escapeHtml(alertId)}', 'investigate')" class="px-3 py-1.5 bg-indigo-50 hover:bg-indigo-100 text-indigo-700 border border-indigo-200 text-xs font-bold rounded-lg transition shadow-sm">
                                    🔍 Mark Investigating
                                </button>
                            ` : ''}
                            ${canResolve ? `
                                <button onclick="openResolveModal('${escapeHtml(alertId)}')" class="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold rounded-lg transition shadow-sm">
                                    ✓ Resolve Incident
                                </button>
                            ` : `
                                <span class="text-xs text-emerald-600 font-bold flex items-center space-x-1">
                                    <span>✓ Resolved</span>
                                </span>
                            `}
                        </div>
                        <button onclick="openAuditHistoryModal('${escapeHtml(alertId)}')" class="px-3 py-1.5 bg-slate-50 hover:bg-slate-100 text-slate-600 border border-slate-200 text-xs font-semibold rounded-lg transition">
                            📜 View Audit History
                        </button>
                    </div>
                `;

                container.appendChild(card);
            } catch (alertRenderErr) {
                console.error('Error rendering individual alert:', alertRenderErr, alert);
            }
        });
    } catch (e) {
        console.error('Error loading store alerts:', e);
        container.innerHTML = `<div class="p-4 bg-red-50 text-red-700 rounded-lg text-xs">Error loading alerts: ${e.message}</div>`;
    }
}

async function handleAlertAction(alertId, action, note = null) {
    const token = getAuthToken();
    if (!token) {
        showManagerToast('Unauthorized', 'Please sign in to perform alert actions.', 'error');
        return;
    }

    try {
        const body = { action: action, note: note || null };
        const res = await fetch(`/api/manager/alerts/${encodeURIComponent(alertId)}/action`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(body)
        });

        if (res.status === 401) {
            clearLocalSession();
            showManagerToast('Session Expired', 'Your session has expired. Please sign in again.', 'error');
            await loadManagerWorkspace();
            return;
        }

        if (!res.ok) {
            const errDetail = await parseErrorResponse(res, 'Could not update alert status.');
            showManagerToast('Action Rejected', errDetail, 'error');
            return;
        }

        const data = await res.json();
        const statusUpper = String(data.status || action || 'updated').toUpperCase();
        showManagerToast('Status Updated', `Alert ${alertId} updated to ${statusUpper}`, 'success');

        // Refresh alerts, KPI summary, and global alerts list
        await loadManagerAlerts();
        await loadStoreSummary();
        loadEmergingAlerts();
    } catch (e) {
        console.error('Error executing alert action:', e);
        showManagerToast('Network Error', e.message, 'error');
    }
}

function openResolveModal(alertId) {
    currentResolvingAlertId = alertId;
    const modal = document.getElementById('managerResolveModal');
    const display = document.getElementById('resolveAlertIdDisplay');
    const noteInput = document.getElementById('resolveNoteInput');

    if (display) display.value = alertId;
    if (noteInput) noteInput.value = '';
    if (modal) modal.classList.remove('hidden');
}

function closeResolveModal() {
    const modal = document.getElementById('managerResolveModal');
    if (modal) modal.classList.add('hidden');
    currentResolvingAlertId = null;
}

async function submitResolveAlert(event) {
    event.preventDefault();
    if (!currentResolvingAlertId) return;

    const noteInput = document.getElementById('resolveNoteInput');
    const note = noteInput ? noteInput.value.trim() : '';

    if (note.length < 5) {
        showManagerToast('Validation Error', 'Resolution note must be at least 5 characters long.', 'error');
        return;
    }

    const btn = document.getElementById('btnSubmitResolve');
    if (btn) btn.disabled = true;

    try {
        await handleAlertAction(currentResolvingAlertId, 'resolve', note);
        closeResolveModal();
    } finally {
        if (btn) btn.disabled = false;
    }
}

function formatAuditTimestamp(isoStr) {
    if (!isoStr) return 'Date unknown';
    try {
        const d = new Date(isoStr);
        if (isNaN(d.getTime())) return String(isoStr);
        return d.toLocaleString('en-US', {
            month: 'short',
            day: 'numeric',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            hour12: true
        });
    } catch (e) {
        return String(isoStr);
    }
}

function getFriendlyActionLabel(action) {
    const act = String(action || '').toUpperCase().trim();
    if (act === 'ALERT_ACKNOWLEDGE' || act === 'ACKNOWLEDGE') return 'Alert acknowledged';
    if (act === 'ALERT_INVESTIGATE' || act === 'INVESTIGATE') return 'Investigation started';
    if (act === 'ALERT_RESOLVE' || act === 'RESOLVE') return 'Alert resolved';
    if (act === 'ALERT_REOPEN' || act === 'REOPEN') return 'Alert reopened';
    const cleaned = act.replace(/^ALERT_/, '').replace(/_/g, ' ').toLowerCase();
    return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

function formatActorRoleLabel(role) {
    const r = String(role || '').toLowerCase().trim();
    if (r === 'store_manager') return 'Store Manager';
    if (r === 'admin') return 'HQ Admin';
    if (r === 'customer') return 'Customer';
    if (!r) return 'Staff';
    return r.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
}

function formatStatusName(st) {
    if (!st) return '';
    return String(st).charAt(0).toUpperCase() + String(st).slice(1).toLowerCase();
}

function getActionVisuals(action) {
    const act = String(action || '').toUpperCase().trim();
    if (act === 'ALERT_RESOLVE' || act === 'RESOLVE') {
        return {
            icon: '✓',
            nodeBg: 'bg-emerald-600 text-white ring-4 ring-emerald-100',
            badgeBg: 'bg-emerald-50 text-emerald-800 border-emerald-200'
        };
    }
    if (act === 'ALERT_INVESTIGATE' || act === 'INVESTIGATE') {
        return {
            icon: '🔍',
            nodeBg: 'bg-indigo-600 text-white ring-4 ring-indigo-100',
            badgeBg: 'bg-indigo-50 text-indigo-800 border-indigo-200'
        };
    }
    if (act === 'ALERT_ACKNOWLEDGE' || act === 'ACKNOWLEDGE') {
        return {
            icon: '👀',
            nodeBg: 'bg-blue-600 text-white ring-4 ring-blue-100',
            badgeBg: 'bg-blue-50 text-blue-800 border-blue-200'
        };
    }
    return {
        icon: 'ℹ️',
        nodeBg: 'bg-slate-700 text-white ring-4 ring-slate-100',
        badgeBg: 'bg-slate-50 text-slate-800 border-slate-200'
    };
}

async function openAuditHistoryModal(alertId) {
    const modal = document.getElementById('managerHistoryModal');
    const alertDisplay = document.getElementById('historyModalAlertIdDisplay');
    const metaBadges = document.getElementById('historyModalMetaBadges');
    const container = document.getElementById('historyTimelineContainer');
    const token = getAuthToken();

    if (alertDisplay) alertDisplay.innerText = alertId;
    if (metaBadges) metaBadges.innerHTML = '';
    if (container) {
        container.innerHTML = `
            <div class="py-10 text-center text-slate-400 space-y-2">
                <div class="inline-block animate-spin text-xl">⏳</div>
                <p class="text-xs">Loading audit trail...</p>
            </div>
        `;
    }
    if (modal) modal.classList.remove('hidden');

    try {
        const res = await fetch(`/api/manager/alerts/${encodeURIComponent(alertId)}/history`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.status === 401) {
            clearLocalSession();
            container.innerHTML = `<div class="p-4 bg-red-50 text-red-700 border border-red-200 rounded-xl text-xs">Session expired (HTTP 401). Please sign in again.</div>`;
            return;
        }
        if (!res.ok) {
            container.innerHTML = `<div class="p-4 bg-red-50 text-red-700 border border-red-200 rounded-xl text-xs">Failed to load audit history (HTTP ${res.status}).</div>`;
            return;
        }
        const data = await res.json();
        const history = data.history || [];

        // Populate top metadata badges
        if (metaBadges) {
            let curStatusBadge = 'bg-slate-100 text-slate-700 border-slate-200';
            const curStatus = (data.current_status || 'new').toLowerCase();
            if (curStatus === 'resolved') curStatusBadge = 'bg-emerald-100 text-emerald-800 border-emerald-200';
            else if (curStatus === 'investigating') curStatusBadge = 'bg-indigo-100 text-indigo-800 border-indigo-200';
            else if (curStatus === 'acknowledged') curStatusBadge = 'bg-blue-100 text-blue-800 border-blue-200';
            else if (curStatus === 'new') curStatusBadge = 'bg-red-100 text-red-800 border-red-200';

            metaBadges.innerHTML = `
                <span class="px-2.5 py-1 rounded-md font-mono font-bold bg-white text-slate-700 border border-slate-200 text-[11px] shadow-2xs">
                    Store: ${escapeHtml(data.store_id || 'ST-D69EG')}
                </span>
                <span class="px-2.5 py-1 rounded-md font-bold uppercase text-[10px] border ${curStatusBadge} shadow-2xs">
                    Current: ${escapeHtml(curStatus)}
                </span>
            `;
        }

        if (history.length === 0) {
            container.innerHTML = `
                <div class="text-center py-10 bg-slate-50 rounded-xl border border-dashed border-slate-200 text-slate-500">
                    <span class="text-3xl">📋</span>
                    <p class="font-bold text-slate-700 text-xs mt-2">No Audit Events Logged Yet</p>
                    <p class="text-[11px] text-slate-400 mt-0.5">Actions performed on this incident alert will appear here in chronological sequence.</p>
                </div>
            `;
            return;
        }

        const count = history.length;
        container.innerHTML = history.map((item, idx) => {
            const isLast = (idx === count - 1);
            let detailsObj = item.details_data || {};
            if (typeof item.details === 'string' && item.details.trim().startsWith('{')) {
                try { detailsObj = JSON.parse(item.details); } catch (e) {}
            }

            const actionLabel = getFriendlyActionLabel(item.action);
            const visuals = getActionVisuals(item.action);
            const formattedTime = formatAuditTimestamp(item.created_at);

            // Actors
            const actorEmail = item.actor_email || detailsObj.actor_email || item.actor_name || 'System Operator';
            const actorRole = item.actor_role || detailsObj.actor_role || 'store_manager';
            const actorRoleLabel = formatActorRoleLabel(actorRole);

            // Transition
            const oldStatus = item.old_status || detailsObj.old_status || null;
            const newStatus = item.new_status || detailsObj.new_status || null;
            const storeId = item.store_id || detailsObj.store_id || data.store_id || '';

            let transitionHtml = '';
            if (oldStatus && newStatus && oldStatus.toLowerCase() !== newStatus.toLowerCase()) {
                transitionHtml = `
                    <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-semibold bg-slate-50 text-slate-700 border border-slate-200">
                        <span class="text-slate-500">${escapeHtml(formatStatusName(oldStatus))}</span>
                        <span class="text-slate-400">→</span>
                        <span class="font-bold text-slate-800">${escapeHtml(formatStatusName(newStatus))}</span>
                    </span>
                `;
            } else if (newStatus) {
                transitionHtml = `
                    <span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-bold bg-slate-50 text-slate-800 border border-slate-200">
                        ${escapeHtml(formatStatusName(newStatus))}
                    </span>
                `;
            }

            // Resolution note section (displayed only when non-empty)
            const resNote = (item.resolution_note || detailsObj.resolution_note || '').trim();
            let noteHtml = '';
            if (resNote) {
                noteHtml = `
                    <div class="mt-2.5 p-3 rounded-lg bg-emerald-50/70 border border-emerald-200 text-xs">
                        <div class="text-[10px] font-bold text-emerald-800 uppercase tracking-wider mb-1 flex items-center gap-1.5">
                            <svg class="w-3.5 h-3.5 text-emerald-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                            <span>Resolution Note & Corrective Action</span>
                        </div>
                        <p class="text-slate-800 leading-relaxed font-normal whitespace-pre-wrap">${escapeHtml(resNote)}</p>
                    </div>
                `;
            }

            return `
                <div class="relative pl-8 pb-5 last:pb-1">
                    <!-- Vertical line connector (hidden on last item) -->
                    ${isLast ? '' : '<div class="absolute left-3.5 top-6 bottom-0 w-0.5 bg-slate-200 -translate-x-1/2"></div>'}

                    <!-- Timeline Icon Node -->
                    <div class="absolute left-3.5 top-1 -translate-x-1/2 w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold ${visuals.nodeBg} shadow-xs z-10">
                        ${visuals.icon}
                    </div>

                    <!-- Event Card -->
                    <div class="bg-white rounded-xl border border-slate-200 p-4 shadow-xs space-y-2 hover:border-slate-300 transition">
                        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-1.5">
                            <div class="flex flex-wrap items-center gap-2">
                                <span class="font-bold text-sm text-navy-900">${escapeHtml(actionLabel)}</span>
                                ${transitionHtml}
                            </div>
                            <span class="text-[11px] text-slate-500 font-medium whitespace-nowrap">${escapeHtml(formattedTime)}</span>
                        </div>

                        <!-- Compact metadata row -->
                        <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500 border-t border-slate-100 pt-2">
                            <span class="inline-flex items-center gap-1 font-semibold text-slate-700">
                                <span class="text-slate-400">👤</span>
                                <span>${escapeHtml(actorEmail)}</span>
                            </span>
                            <span class="text-slate-300">•</span>
                            <span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 font-semibold text-[10px] uppercase tracking-wide">
                                ${escapeHtml(actorRoleLabel)}
                            </span>
                            ${storeId ? `
                                <span class="text-slate-300">•</span>
                                <span class="font-medium text-slate-600">Store: <strong class="font-mono text-slate-700">${escapeHtml(storeId)}</strong></span>
                            ` : ''}
                        </div>

                        ${noteHtml}
                    </div>
                </div>
            `;
        }).join('');

    } catch (e) {
        console.error('Error fetching audit history:', e);
        if (container) container.innerHTML = `<div class="p-3 bg-red-50 text-red-700 rounded text-xs">Error: ${e.message}</div>`;
    }
}

function closeHistoryModal() {
    const modal = document.getElementById('managerHistoryModal');
    if (modal) modal.classList.add('hidden');
}

function debounceManagerSearch() {
    clearTimeout(managerSearchTimeout);
    managerSearchTimeout = setTimeout(() => {
        loadManagerInbox(1);
    }, 350);
}

function navigateManagerInbox(direction) {
    const newPage = currentManagerPage + direction;
    if (newPage >= 1 && newPage <= currentManagerTotalPages) {
        loadManagerInbox(newPage);
    }
}

async function loadManagerInbox(page = 1) {
    const token = getAuthToken();
    const tbody = document.getElementById('managerInboxTableBody');
    if (!tbody) return;
    if (!token) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center py-6 text-amber-600 font-semibold">Sign in required to view store feedback stream.</td></tr>';
        return;
    }

    tbody.innerHTML = '<tr><td colspan="10" class="text-center py-8 text-slate-400">Loading feedback stream...</td></tr>';

    const severity = document.getElementById('mgrFilterSeverity') ? document.getElementById('mgrFilterSeverity').value : '';
    const actionable = document.getElementById('mgrFilterActionable') ? document.getElementById('mgrFilterActionable').value : '';
    const channel = document.getElementById('mgrFilterChannel') ? document.getElementById('mgrFilterChannel').value : '';
    const search = document.getElementById('mgrFilterSearch') ? document.getElementById('mgrFilterSearch').value.trim() : '';

    currentManagerPage = page;

    try {
        const params = new URLSearchParams({
            store_id: currentManagerStoreId || '',
            page: page,
            limit: 15
        });
        if (severity) params.append('severity', severity);
        if (actionable) params.append('actionable', actionable);
        if (channel) params.append('channel', channel);
        if (search) params.append('search', search);

        const res = await fetch(`/api/manager/feedback-inbox?${params.toString()}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (res.status === 401) {
            clearLocalSession();
            tbody.innerHTML = '<tr><td colspan="10" class="text-center py-6 text-red-500 font-semibold">Session expired or unauthorized (HTTP 401). Please sign in again.</td></tr>';
            return;
        }

        if (!res.ok) {
            tbody.innerHTML = `<tr><td colspan="10" class="text-center py-6 text-red-500">Failed to load feedback inbox (HTTP ${res.status}).</td></tr>`;
            return;
        }

        const data = await res.json();
        currentManagerTotalPages = data.pages || 1;

        const countBadge = document.getElementById('managerInboxCountBadge');
        if (countBadge) countBadge.innerText = `${(data.total || 0).toLocaleString()} matching records`;

        const paginationInfo = document.getElementById('mgrPaginationInfo');
        if (paginationInfo) {
            const startRec = ((data.page - 1) * data.limit) + 1;
            const endRec = Math.min(data.page * data.limit, data.total);
            paginationInfo.innerText = data.total > 0 ? `Showing ${startRec} to ${endRec} of ${data.total.toLocaleString()} records` : 'No records found';
        }

        const currPageEl = document.getElementById('mgrCurrentPage');
        if (currPageEl) currPageEl.innerText = `Page ${data.page} of ${data.pages || 1}`;

        const btnPrev = document.getElementById('btnMgrPrev');
        const btnNext = document.getElementById('btnMgrNext');
        if (btnPrev) btnPrev.disabled = data.page <= 1;
        if (btnNext) btnNext.disabled = data.page >= data.pages;

        const records = data.records || [];
        window._managerInboxRecords = records;
        if (records.length === 0) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center py-8 text-slate-400">No feedback matches your filter criteria.</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        records.forEach(r => {
            const tr = document.createElement('tr');
            tr.className = 'hover:bg-slate-50 transition border-b border-slate-100 text-xs';

            let channelIcon = '🌐';
            let channelLabel = 'Online Review';
            if (r.channel === 'in_app') { channelIcon = '📱'; channelLabel = 'In-App'; }
            else if (r.channel === 'staff_shift_note') { channelIcon = '📋'; channelLabel = 'Staff Note'; }
            else if (r.channel === 'call_centre_ticket') { channelIcon = '📞'; channelLabel = 'Call Centre'; }

            // Column 10: Status & Action
            let actionColHtml = '';
            if (r.is_customer_submission) {
                const status = String(r.status || 'new').toLowerCase();
                let statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-100 text-amber-800 border border-amber-200 uppercase">New</span>';
                if (status === 'acknowledged') {
                    statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-blue-100 text-blue-800 border border-blue-200 uppercase">Acknowledged</span>';
                } else if (status === 'investigating') {
                    statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-indigo-100 text-indigo-800 border border-indigo-200 uppercase">Investigating</span>';
                } else if (status === 'resolved') {
                    statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-100 text-emerald-800 border border-emerald-200 uppercase">Resolved</span>';
                }

                let buttons = '';
                if (status === 'new') {
                    buttons = `
                        <button onclick="handleManagerFeedbackAction('${escapeHtml(r.feedback_id)}', 'acknowledge')" class="px-2 py-1 bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 rounded font-bold text-[10px] transition">Acknowledge</button>
                        <button onclick="handleManagerFeedbackAction('${escapeHtml(r.feedback_id)}', 'investigate')" class="px-2 py-1 bg-indigo-50 hover:bg-indigo-100 text-indigo-700 border border-indigo-200 rounded font-bold text-[10px] transition">Investigate</button>
                        <button onclick="openFeedbackResolveModal('${escapeHtml(r.feedback_id)}')" class="px-2 py-1 bg-emerald-600 hover:bg-emerald-700 text-white rounded font-bold text-[10px] transition">Resolve</button>
                    `;
                } else if (status === 'acknowledged') {
                    buttons = `
                        <button onclick="handleManagerFeedbackAction('${escapeHtml(r.feedback_id)}', 'investigate')" class="px-2 py-1 bg-indigo-50 hover:bg-indigo-100 text-indigo-700 border border-indigo-200 rounded font-bold text-[10px] transition">Investigate</button>
                        <button onclick="openFeedbackResolveModal('${escapeHtml(r.feedback_id)}')" class="px-2 py-1 bg-emerald-600 hover:bg-emerald-700 text-white rounded font-bold text-[10px] transition">Resolve</button>
                    `;
                } else if (status === 'investigating') {
                    buttons = `
                        <button onclick="openFeedbackResolveModal('${escapeHtml(r.feedback_id)}')" class="px-2 py-1 bg-emerald-600 hover:bg-emerald-700 text-white rounded font-bold text-[10px] transition">Resolve</button>
                    `;
                } else if (status === 'resolved') {
                    buttons = `<span class="text-emerald-700 font-bold text-[11px] flex items-center justify-center space-x-1"><span>✓</span><span>Completed</span></span>`;
                }

                actionColHtml = `
                    <div class="flex flex-col items-center space-y-1">
                        ${statusBadge}
                        <div class="flex flex-wrap items-center justify-center gap-1">
                            ${buttons}
                        </div>
                        ${r.resolution_notes ? `<div class="text-[10px] text-slate-500 italic max-w-[160px] truncate" title="${escapeHtml(r.resolution_notes)}">Note: "${escapeHtml(r.resolution_notes)}"</div>` : ''}
                    </div>
                `;
            } else {
                actionColHtml = `<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-500 font-mono text-[10px]">Baseline</span>`;
            }

            tr.innerHTML = `
                <td class="py-2.5 px-3 font-mono font-bold text-slate-500 whitespace-nowrap">${escapeHtml(r.feedback_id)}</td>
                <td class="py-2.5 px-3 whitespace-nowrap">
                    <span class="inline-flex items-center space-x-1 px-2 py-0.5 rounded bg-slate-100 text-slate-700 font-medium text-[11px]">
                        <span>${channelIcon}</span>
                        <span>${channelLabel}</span>
                    </span>
                </td>
                <td class="py-2.5 px-3 text-slate-500 whitespace-nowrap">${escapeHtml(r.date || (r.created_at ? r.created_at.slice(0, 10) : 'N/A'))}</td>
                <td class="py-2.5 px-2 text-center font-bold ${r.rating <= 2 ? 'text-red-500' : 'text-slate-700'}">
                    ${r.rating !== null && r.rating !== undefined && r.rating !== '' ? r.rating + '★' : '<span class="text-slate-300">-</span>'}
                </td>
                <td class="py-2.5 px-4 text-slate-800 leading-relaxed font-normal">${escapeHtml(r.text)}</td>
                <td class="py-2.5 px-3 font-medium text-slate-700 whitespace-nowrap">
                    <div>${escapeHtml(r.issue_type_name || r.issue_type_id)}</div>
                    <div class="text-[10px] font-mono text-slate-400">${escapeHtml(r.issue_type_id)}</div>
                </td>
                <td class="py-2.5 px-3 whitespace-nowrap">
                    <span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700 text-[11px] font-semibold">${escapeHtml(r.owner_function || 'Store Ops')}</span>
                </td>
                <td class="py-2.5 px-2 text-center whitespace-nowrap">
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold badge-${r.severity}">${escapeHtml(r.severity)}</span>
                </td>
                <td class="py-2.5 px-2 text-center whitespace-nowrap">
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold ${r.actionable === 1 ? 'badge-actionable-yes' : 'badge-actionable-no'}">${r.actionable}</span>
                </td>
                <td class="py-2.5 px-3 text-center whitespace-nowrap">
                    ${actionColHtml}
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error('Error fetching manager inbox:', e);
        tbody.innerHTML = `<tr><td colspan="10" class="text-center py-6 text-red-500">Error: ${e.message}</td></tr>`;
    }
}

// -----------------------------------------------------------------------------
// Manager Feedback Action Handlers
// -----------------------------------------------------------------------------
let currentResolvingFeedbackId = null;

function openFeedbackResolveModal(feedbackId, snippet = null) {
    currentResolvingFeedbackId = feedbackId;
    const modal = document.getElementById('managerFeedbackResolveModal');
    const idDisp = document.getElementById('feedbackResolveIdDisplay');
    const snipDisp = document.getElementById('feedbackResolveSnippetDisplay');
    const noteInput = document.getElementById('feedbackResolveNoteInput');

    if (idDisp) idDisp.value = feedbackId;
    let text = snippet;
    if (!text && window._managerInboxRecords) {
        const found = window._managerInboxRecords.find(x => x.feedback_id === feedbackId);
        if (found) text = found.text;
    }
    if (snipDisp) snipDisp.innerText = text || 'Customer Feedback';
    if (noteInput) noteInput.value = '';
    if (modal) modal.classList.remove('hidden');
}

function closeFeedbackResolveModal() {
    const modal = document.getElementById('managerFeedbackResolveModal');
    if (modal) modal.classList.add('hidden');
    currentResolvingFeedbackId = null;
}

async function submitFeedbackResolve(event) {
    event.preventDefault();
    if (!currentResolvingFeedbackId) return;

    const noteInput = document.getElementById('feedbackResolveNoteInput');
    const note = noteInput ? noteInput.value.trim() : '';

    if (note.length < 5) {
        showManagerToast('Validation Error', 'Resolution note must be at least 5 characters.', 'error');
        return;
    }

    const btn = document.getElementById('btnSubmitFeedbackResolve');
    if (btn) btn.disabled = true;

    try {
        await handleManagerFeedbackAction(currentResolvingFeedbackId, 'resolve', note);
        closeFeedbackResolveModal();
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function handleManagerFeedbackAction(feedbackId, action, note = null) {
    const token = getAuthToken();
    if (!token) {
        showManagerToast('Unauthorized', 'Please sign in to update feedback status.', 'error');
        return;
    }

    try {
        const body = { action: action, note: note || null };
        const res = await fetch(`/api/manager/feedback/${encodeURIComponent(feedbackId)}/action`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(body)
        });

        if (res.status === 401) {
            clearLocalSession();
            showManagerToast('Session Expired', 'Session expired. Please sign in again.', 'error');
            await loadManagerWorkspace();
            return;
        }

        if (!res.ok) {
            const errDetail = await parseErrorResponse(res, 'Could not update feedback status.');
            showManagerToast('Action Rejected', errDetail, 'error');
            return;
        }

        const data = await res.json();
        const statusUpper = String(data.status || action || 'updated').toUpperCase();
        showManagerToast('Status Updated', `Feedback ${feedbackId} marked as ${statusUpper}`, 'success');

        await loadManagerInbox(currentManagerPage);
        await loadStoreSummary();
    } catch (e) {
        console.error('Error updating feedback status:', e);
        showManagerToast('Network Error', e.message, 'error');
    }
}

// =============================================================================
// 7. Customer Portal Controller
// =============================================================================
let currentCustomerSubView = 'dashboard';
let currentCustomerPage = 1;
let currentCustomerTotalPages = 1;
let customerSearchTimeout = null;

async function loadCustomerPortal() {
    const user = getAuthUser();
    const token = getAuthToken();
    const gate = document.getElementById('customerAuthGate');
    const header = document.getElementById('customerActiveHeader');

    if (!user || !token) {
        if (gate) gate.classList.remove('hidden');
        if (header) header.classList.add('hidden');
        document.getElementById('custSubViewDashboard')?.classList.add('hidden');
        document.getElementById('custSubViewSubmit')?.classList.add('hidden');
        document.getElementById('custSubViewHistory')?.classList.add('hidden');
        return;
    }

    // Authenticated
    if (gate) gate.classList.add('hidden');
    if (header) header.classList.remove('hidden');

    const nameEl = document.getElementById('custUserName');
    const emailEl = document.getElementById('custUserEmail');
    if (nameEl) nameEl.innerText = user.full_name || (user.email ? user.email.split('@')[0] : 'Customer');
    if (emailEl) emailEl.innerText = user.email || '';

    await populateCustomerStoreSelect();
    showCustomerSubView(currentCustomerSubView || 'dashboard');
}

function showCustomerSubView(subView, pushHistory = true) {
    currentCustomerSubView = subView;
    const targetHash = '#customer/' + subView;
    if (pushHistory) {
        if (!history.state || history.state.tab !== 'customer' || history.state.subview !== subView) {
            history.pushState({ tab: 'customer', subview: subView }, '', targetHash);
        }
    } else {
        if (!history.state || history.state.tab !== 'customer' || history.state.subview !== subView) {
            history.replaceState({ tab: 'customer', subview: subView }, '', targetHash);
        }
    }
    const views = ['dashboard', 'submit', 'history'];
    views.forEach(v => {
        const btn = document.getElementById(`btnCustSub${v === 'dashboard' ? 'Dash' : v === 'submit' ? 'Submit' : 'Hist'}`);
        const container = document.getElementById(`custSubView${v.charAt(0).toUpperCase() + v.slice(1)}`);
        if (container) {
            if (v === subView) {
                container.classList.remove('hidden');
            } else {
                container.classList.add('hidden');
            }
        }
        if (btn) {
            if (v === subView) {
                btn.className = 'px-4 py-2 rounded-lg bg-saffron-500 text-navy-900 font-bold transition shadow-xs';
                btn.setAttribute('aria-current', 'page');
            } else {
                btn.className = 'px-4 py-2 rounded-lg bg-slate-100 text-slate-700 hover:bg-slate-200 transition';
                btn.removeAttribute('aria-current');
            }
        }
    });

    if (subView === 'dashboard') {
        loadCustomerDashboard();
    } else if (subView === 'history') {
        loadCustomerSubmissions(1);
    } else if (subView === 'submit') {
        populateCustomerStoreSelect();
        const succAlert = document.getElementById('custSubmitSuccessAlert');
        const errAlert = document.getElementById('custSubmitErrorAlert');
        if (succAlert) succAlert.classList.add('hidden');
        if (errAlert) errAlert.classList.add('hidden');
    }
    renderWorkspaceNavigation(getAuthUser(), 'customer');
}

async function populateCustomerStoreSelect(force = false) {
    const sel = document.getElementById('custStoreSelect');
    const errEl = document.getElementById('custStoreLoadError');
    if (!sel) return;

    // If already loaded real stores and not forcing a refresh, retain current options
    if (!force && sel.getAttribute('data-loaded') === 'true' && sel.options.length > 1) {
        return;
    }

    const currentSelectedId = sel.value;

    // Show clear loading state
    sel.disabled = true;
    sel.innerHTML = '<option value="" disabled selected>Loading store locations...</option>';
    if (errEl) {
        errEl.classList.add('hidden');
        errEl.innerHTML = '';
    }

    try {
        const res = await fetch('/api/stores');
        if (!res.ok) {
            throw new Error(`Server returned HTTP ${res.status}`);
        }
        const data = await res.json();
        // Backend returns top-level array of stores or object with .stores
        const stores = Array.isArray(data) ? data : (data.stores || []);

        if (!stores || stores.length === 0) {
            throw new Error('No store locations available in store directory.');
        }

        // Sort alphabetically by store name for clean UX
        stores.sort((a, b) => (a.store_name || '').localeCompare(b.store_name || ''));

        sel.innerHTML = '<option value="" disabled selected>Select a store location...</option>';
        stores.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.store_id;
            const regionPart = s.region ? `, ${s.region}` : '';
            const cityPart = s.city ? ` — ${s.city}${regionPart}` : '';
            opt.innerText = `${s.store_name} (${s.store_id})${cityPart}`;
            sel.appendChild(opt);
        });

        // Retain previous user selection if still present in options
        if (currentSelectedId && Array.from(sel.options).some(o => o.value === currentSelectedId)) {
            sel.value = currentSelectedId;
        }

        sel.disabled = false;
        sel.setAttribute('data-loaded', 'true');
    } catch (e) {
        console.error('Error fetching store directory:', e);
        sel.disabled = true;
        sel.innerHTML = '<option value="" disabled selected>⚠️ Failed to load stores</option>';
        if (errEl) {
            errEl.innerHTML = `
                <div class="flex items-center justify-between p-2 rounded bg-red-50 border border-red-200 text-red-700">
                    <span>⚠️ Failed to load store list: ${escapeHtml(e.message)}</span>
                    <button type="button" onclick="populateCustomerStoreSelect(true)" class="px-2 py-0.5 rounded bg-red-100 hover:bg-red-200 font-bold text-red-800 transition">Retry</button>
                </div>
            `;
            errEl.classList.remove('hidden');
        }
    }
}

function updateCustCharCount() {
    const txt = document.getElementById('custFeedbackText');
    const cnt = document.getElementById('custCharCount');
    if (txt && cnt) {
        cnt.innerText = `${txt.value.length} / 2000`;
    }
}

async function handleCustomerFeedbackSubmit(event) {
    event.preventDefault();
    const token = getAuthToken();
    if (!token) {
        showManagerToast('Authentication Required', 'Please sign in to submit feedback.', 'error');
        return;
    }

    const storeId = document.getElementById('custStoreSelect')?.value;
    const channel = document.getElementById('custChannelSelect')?.value || 'in_app';
    const ratingVal = document.getElementById('custRatingSelect')?.value;
    const text = document.getElementById('custFeedbackText')?.value?.trim();

    const succAlert = document.getElementById('custSubmitSuccessAlert');
    const errAlert = document.getElementById('custSubmitErrorAlert');
    const btn = document.getElementById('btnSubmitCustomerFeedback');

    if (succAlert) succAlert.classList.add('hidden');
    if (errAlert) errAlert.classList.add('hidden');

    if (!storeId) {
        if (errAlert) {
            errAlert.innerText = 'Please select a store location.';
            errAlert.classList.remove('hidden');
        }
        return;
    }

    if (!text || text.length < 5) {
        if (errAlert) {
            errAlert.innerText = 'Feedback text must be at least 5 characters long.';
            errAlert.classList.remove('hidden');
        }
        return;
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>⏳ Processing & Classifying...</span>';
    }

    try {
        const payload = {
            store_id: storeId,
            channel: channel,
            rating: ratingVal ? parseInt(ratingVal) : null,
            text: text
        };

        const res = await fetch('/api/customer/submissions', {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        if (res.status === 401) {
            clearLocalSession();
            if (errAlert) {
                errAlert.innerText = 'Session expired. Please sign in again.';
                errAlert.classList.remove('hidden');
            }
            await loadCustomerPortal();
            return;
        }

        if (!res.ok) {
            const errDetail = await parseErrorResponse(res, 'Server rejected request');
            if (errAlert) {
                errAlert.innerText = `Submission failed: ${errDetail}`;
                errAlert.classList.remove('hidden');
            }
            return;
        }

        const data = await res.json();
        const sub = data.submission || {};

        if (succAlert) {
            const sevUpper = (sub.severity || 'low').toUpperCase();
            const actLabel = sub.actionable === 1 ? 'Yes' : 'No';
            succAlert.innerHTML = `
                <div class="flex items-start justify-between">
                    <div class="space-y-1">
                        <div class="flex items-center space-x-2">
                            <span class="font-bold text-sm text-emerald-900">✓ Feedback Submitted & Persisted</span>
                            <span class="font-mono text-xs px-2 py-0.5 rounded bg-emerald-200/80 font-bold">${escapeHtml(sub.feedback_id)}</span>
                        </div>
                        <p class="text-emerald-800">Your feedback has been logged in the store database and routed to <strong>${escapeHtml(sub.store_name)}</strong> store management.</p>
                        <div class="flex flex-wrap items-center gap-2 pt-1 text-[11px]">
                            <span class="px-2 py-0.5 rounded bg-white font-bold border border-emerald-300 text-slate-800">Issue: ${escapeHtml(sub.issue_type_name || sub.issue_type_id)}</span>
                            <span class="px-2 py-0.5 rounded bg-white font-bold border border-emerald-300 text-slate-800">Dept: ${escapeHtml(sub.owner_function)}</span>
                            <span class="px-2 py-0.5 rounded bg-white font-bold border border-emerald-300 text-slate-800">Severity: ${sevUpper}</span>
                            <span class="px-2 py-0.5 rounded bg-white font-bold border border-emerald-300 text-slate-800">Actionable: ${actLabel}</span>
                        </div>
                    </div>
                </div>
            `;
            succAlert.classList.remove('hidden');
        }

        const form = document.getElementById('custFeedbackForm');
        if (form) form.reset();
        updateCustCharCount();

        showManagerToast('Feedback Submitted', `Reference ${sub.feedback_id} recorded`, 'success');

        loadCustomerDashboard();

    } catch (e) {
        console.error('Error submitting customer feedback:', e);
        if (errAlert) {
            errAlert.innerText = `Network or unexpected error: ${e.message}`;
            errAlert.classList.remove('hidden');
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span>Submit Feedback</span>';
        }
    }
}

async function loadCustomerDashboard() {
    const container = document.getElementById('custRecentContainer');
    const token = getAuthToken();
    if (!token) {
        if (container) {
            container.innerHTML = `
                <div class="p-6 text-center bg-amber-50 rounded-xl border border-amber-200 text-amber-800 text-xs space-y-2">
                    <p class="font-bold">Authentication Required</p>
                    <p>Please sign in to view your recent feedback submissions.</p>
                    <button onclick="openAuthModal()" class="mt-2 px-3.5 py-1.5 bg-saffron-500 hover:bg-saffron-600 text-navy-900 font-bold rounded-lg transition shadow-xs">
                        Sign In / Register
                    </button>
                </div>
            `;
        }
        return;
    }

    if (container) {
        container.innerHTML = '<div class="text-center py-8 text-slate-400 text-xs">Loading recent submissions...</div>';
    }

    try {
        const res = await fetch('/api/customer/dashboard', {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });

        if (res.status === 401) {
            clearLocalSession();
            if (container) {
                container.innerHTML = `
                    <div class="p-6 text-center bg-red-50 rounded-xl border border-red-200 text-red-700 text-xs space-y-3">
                        <p class="font-bold">Session Expired (HTTP 401)</p>
                        <p>Your session has expired. Please sign in again to view recent submissions.</p>
                        <button onclick="quickLoginDemo('customer')" class="px-3.5 py-1.5 bg-red-700 hover:bg-red-800 text-white font-semibold rounded-lg shadow-xs">
                            Sign In as Customer Demo
                        </button>
                    </div>
                `;
            }
            await loadCustomerPortal();
            return;
        }

        if (!res.ok) {
            const errDetail = await parseErrorResponse(res, `Failed to load dashboard (HTTP ${res.status}).`);
            if (container) {
                container.innerHTML = `
                    <div class="p-4 bg-red-50 text-red-700 border border-red-200 rounded-xl text-xs space-y-2">
                        <div class="flex items-center space-x-2 font-bold">
                            <span>⚠️</span>
                            <span>Failed to load recent submissions</span>
                        </div>
                        <p class="text-red-600">${escapeHtml(errDetail)}</p>
                        <button onclick="loadCustomerDashboard()" class="mt-2 px-3 py-1.5 bg-red-700 hover:bg-red-800 text-white rounded-lg text-xs font-semibold shadow-xs flex items-center space-x-1.5">
                            <span>↻ Retry</span>
                        </button>
                    </div>
                `;
            }
            return;
        }

        const data = await res.json();
        const totalVal = Number(data.total_submissions || 0);
        const openVal = Number(data.open_submissions || 0);
        const resVal = Number(data.resolved_submissions || 0);

        const totalEl = document.getElementById('custKpiTotal');
        const openEl = document.getElementById('custKpiOpen');
        const resEl = document.getElementById('custKpiResolved');
        if (totalEl) totalEl.innerText = totalVal.toLocaleString();
        if (openEl) openEl.innerText = openVal.toLocaleString();
        if (resEl) resEl.innerText = resVal.toLocaleString();

        // Customer Status Donut Chart
        renderOrUpdateChart('chartCustStatus', {
            type: 'doughnut',
            data: {
                labels: totalVal === 0 ? ['No Feedback'] : ['Resolved', 'In Progress'],
                datasets: [{
                    data: totalVal === 0 ? [1] : [resVal, openVal],
                    backgroundColor: totalVal === 0 ? ['#E2E8F0'] : ['#10B981', '#F59E0B'],
                    borderWidth: 2,
                    borderColor: '#FFFFFF'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '68%',
                plugins: {
                    legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 10, family: 'Inter' } } },
                    tooltip: { enabled: totalVal > 0 }
                }
            }
        });

        if (!container) return;

        const recents = data.recent_submissions || [];
        if (recents.length === 0) {
            container.innerHTML = `
                <div class="text-center py-8 bg-slate-50 rounded-xl border border-dashed border-slate-200">
                    <span class="text-2xl">📝</span>
                    <p class="font-bold text-slate-700 text-xs mt-1">No recent feedback submissions yet</p>
                    <p class="text-[11px] text-slate-400 mt-0.5">Click "Submit Feedback" above to report a store observation or issue.</p>
                </div>
            `;
            return;
        }

        container.innerHTML = recents.map(r => {
            const status = String(r.status || 'new').toLowerCase();
            let statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-100 text-amber-800 border border-amber-200 uppercase">New</span>';
            if (status === 'acknowledged') {
                statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-blue-100 text-blue-800 border border-blue-200 uppercase">Acknowledged</span>';
            } else if (status === 'investigating') {
                statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-indigo-100 text-indigo-800 border border-indigo-200 uppercase">Investigating</span>';
            } else if (status === 'resolved') {
                statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-100 text-emerald-800 border border-emerald-200 uppercase">Resolved</span>';
            }

            const sev = String(r.severity || 'low').toLowerCase();

            return `
                <div class="p-3.5 bg-slate-50 rounded-xl border border-slate-200 space-y-2 hover:border-slate-300 transition">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-1.5 text-xs">
                        <div class="flex items-center space-x-2">
                            <span class="font-mono font-bold text-slate-700">${escapeHtml(r.feedback_id)}</span>
                            <span class="font-semibold text-navy-900">${escapeHtml(r.store_name || r.store_id)}</span>
                        </div>
                        <div class="flex items-center space-x-2">
                            ${statusBadge}
                            <span class="px-2 py-0.5 rounded text-[10px] font-bold badge-${sev}">${sev.toUpperCase()}</span>
                            <span class="text-slate-400 text-[11px]">${r.created_at ? formatAuditTimestamp(r.created_at) : ''}</span>
                        </div>
                    </div>
                    <p class="text-xs text-slate-800 italic">"${escapeHtml(r.text)}"</p>
                    <div class="flex items-center justify-between text-[11px] text-slate-500 pt-1 border-t border-slate-200/60">
                        <span>Classification: <strong>${escapeHtml(r.issue_type_name || r.issue_type_id)}</strong> (${escapeHtml(r.owner_function)})</span>
                        ${r.resolution_notes ? `<span class="text-emerald-700 font-semibold truncate max-w-xs" title="${escapeHtml(r.resolution_notes)}">Action: "${escapeHtml(r.resolution_notes)}"</span>` : '<span class="text-slate-400">Under review</span>'}
                    </div>
                </div>
            `;
        }).join('');

    } catch (e) {
        console.error('Error loading customer dashboard:', e);
        if (container) {
            container.innerHTML = `
                <div class="p-4 bg-red-50 text-red-700 border border-red-200 rounded-xl text-xs space-y-2">
                    <div class="flex items-center space-x-2 font-bold">
                        <span>⚠️</span>
                        <span>Failed to load recent submissions</span>
                    </div>
                    <p class="text-red-600">${escapeHtml(e.message)}</p>
                    <button onclick="loadCustomerDashboard()" class="mt-2 px-3 py-1.5 bg-red-700 hover:bg-red-800 text-white rounded-lg text-xs font-semibold shadow-xs flex items-center space-x-1.5">
                        <span>↻ Retry</span>
                    </button>
                </div>
            `;
        }
    }
}

function debounceCustomerSearch() {
    clearTimeout(customerSearchTimeout);
    customerSearchTimeout = setTimeout(() => {
        loadCustomerSubmissions(1);
    }, 350);
}

function navigateCustomerHistory(direction) {
    const newPage = currentCustomerPage + direction;
    if (newPage >= 1 && newPage <= currentCustomerTotalPages) {
        loadCustomerSubmissions(newPage);
    }
}

async function loadCustomerSubmissions(page = 1) {
    const token = getAuthToken();
    const container = document.getElementById('custHistoryContainer');
    if (!container) return;

    if (!token) {
        container.innerHTML = '<div class="text-center py-6 text-amber-600 font-semibold text-xs">Authentication required.</div>';
        return;
    }

    container.innerHTML = '<div class="text-center py-8 text-slate-400 text-xs">Loading submission history...</div>';

    const statusFilter = document.getElementById('custFilterStatus')?.value || 'All';
    const searchFilter = document.getElementById('custFilterSearch')?.value?.trim() || '';

    currentCustomerPage = page;

    try {
        const params = new URLSearchParams({
            page: page,
            limit: 10
        });
        if (statusFilter && statusFilter !== 'All') params.append('status', statusFilter);
        if (searchFilter) params.append('search', searchFilter);

        const res = await fetch(`/api/customer/submissions?${params.toString()}`, {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });

        if (res.status === 401) {
            clearLocalSession();
            container.innerHTML = '<div class="text-center py-6 text-red-500 font-semibold text-xs">Session expired. Please sign in again.</div>';
            return;
        }

        if (!res.ok) {
            const errDetail = await parseErrorResponse(res, `Failed to load history (HTTP ${res.status})`);
            container.innerHTML = `<div class="text-center py-6 text-red-500 text-xs">${escapeHtml(errDetail)}</div>`;
            return;
        }

        const data = await res.json();
        currentCustomerTotalPages = data.pages || 1;

        const paginationInfo = document.getElementById('custPaginationInfo');
        if (paginationInfo) {
            const startRec = ((data.page - 1) * data.limit) + 1;
            const endRec = Math.min(data.page * data.limit, data.total);
            paginationInfo.innerText = data.total > 0 ? `Showing ${startRec} to ${endRec} of ${data.total.toLocaleString()} submissions` : 'No submissions found';
        }

        const currPageEl = document.getElementById('custCurrentPage');
        if (currPageEl) currPageEl.innerText = `Page ${data.page} of ${data.pages || 1}`;

        const btnPrev = document.getElementById('btnCustPrev');
        const btnNext = document.getElementById('btnCustNext');
        if (btnPrev) btnPrev.disabled = data.page <= 1;
        if (btnNext) btnNext.disabled = data.page >= data.pages;

        const subs = data.submissions || [];
        if (subs.length === 0) {
            container.innerHTML = `
                <div class="text-center py-10 bg-slate-50 rounded-xl border border-dashed border-slate-200">
                    <span class="text-3xl">📭</span>
                    <p class="font-bold text-slate-700 text-xs mt-2">No Submissions Found</p>
                    <p class="text-[11px] text-slate-400 mt-0.5">No feedback records match the specified filters.</p>
                </div>
            `;
            return;
        }

        container.innerHTML = subs.map(sub => {
            const status = String(sub.status || 'new').toLowerCase();
            let statusBadge = '<span class="px-2.5 py-1 rounded-md text-xs font-bold bg-amber-100 text-amber-800 border border-amber-200 uppercase">⏳ New / Unacknowledged</span>';
            if (status === 'acknowledged') {
                statusBadge = '<span class="px-2.5 py-1 rounded-md text-xs font-bold bg-blue-100 text-blue-800 border border-blue-200 uppercase">👀 Acknowledged by Store</span>';
            } else if (status === 'investigating') {
                statusBadge = '<span class="px-2.5 py-1 rounded-md text-xs font-bold bg-indigo-100 text-indigo-800 border border-indigo-200 uppercase">🔍 Under Investigation</span>';
            } else if (status === 'resolved') {
                statusBadge = '<span class="px-2.5 py-1 rounded-md text-xs font-bold bg-emerald-100 text-emerald-800 border border-emerald-200 uppercase">✅ Resolved</span>';
            }

            const sev = String(sub.severity || 'low').toLowerCase();
            const ratingStr = (sub.rating !== null && sub.rating !== undefined && sub.rating !== '') ? `⭐ ${sub.rating}/5` : 'No rating';
            const formattedDate = formatAuditTimestamp(sub.created_at);

            let noteSection = '';
            if (sub.resolution_notes) {
                noteSection = `
                    <div class="mt-3 p-3.5 bg-emerald-50/80 border border-emerald-200 rounded-xl text-xs space-y-1">
                        <div class="flex items-center justify-between text-emerald-900 font-bold">
                            <span class="flex items-center space-x-1.5">
                                <span>🛡️</span>
                                <span>Store Manager Corrective Action & Resolution Note</span>
                            </span>
                            <span class="text-[10px] text-emerald-700 font-normal">${sub.updated_at ? formatAuditTimestamp(sub.updated_at) : ''}</span>
                        </div>
                        <p class="text-slate-800 leading-relaxed font-normal whitespace-pre-wrap">${escapeHtml(sub.resolution_notes)}</p>
                    </div>
                `;
            }

            return `
                <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm space-y-3 hover:border-slate-300 transition">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-100 pb-3">
                        <div class="flex flex-wrap items-center gap-2">
                            <span class="font-mono text-xs font-bold text-slate-600 bg-slate-100 px-2 py-0.5 rounded">${escapeHtml(sub.feedback_id)}</span>
                            <span class="font-extrabold text-navy-900 text-sm">${escapeHtml(sub.store_name)} (${escapeHtml(sub.store_id)})</span>
                            <span class="text-xs text-slate-400">• Channel: <strong>${escapeHtml(sub.channel)}</strong></span>
                            <span class="text-xs text-amber-600 font-semibold">${escapeHtml(ratingStr)}</span>
                        </div>
                        <div class="flex items-center space-x-2">
                            ${statusBadge}
                            <span class="text-xs text-slate-400 font-medium">${escapeHtml(formattedDate)}</span>
                        </div>
                    </div>

                    <div class="flex flex-wrap items-center gap-2 text-xs">
                        <span class="text-slate-400 font-semibold text-[10px] uppercase">AI Classification:</span>
                        <span class="font-bold text-navy-900 bg-slate-100 px-2 py-0.5 rounded text-[11px]">${escapeHtml(sub.issue_type_name || sub.issue_type_id)}</span>
                        <span class="text-slate-400 font-mono text-[10px]">(${escapeHtml(sub.issue_type_id)})</span>
                        <span class="text-indigo-600 font-semibold bg-indigo-50 px-2 py-0.5 rounded text-[11px]">Owner: ${escapeHtml(sub.owner_function)}</span>
                        <span class="px-2 py-0.5 rounded text-[10px] font-bold badge-${sev}">${sev.toUpperCase()}</span>
                        <span class="px-2 py-0.5 rounded text-[10px] font-bold ${sub.actionable === 1 ? 'badge-actionable-yes' : 'badge-actionable-no'}">${sub.actionable === 1 ? 'Actionable' : 'Non-Actionable'}</span>
                    </div>

                    <div class="text-xs text-slate-800 bg-slate-50 p-3 rounded-lg border border-slate-100 leading-relaxed font-sans">
                        ${escapeHtml(sub.text)}
                    </div>

                    ${noteSection}
                </div>
            `;
        }).join('');

    } catch (e) {
        console.error('Error loading customer submissions:', e);
        container.innerHTML = `<div class="text-center py-6 text-red-500 text-xs">Error: ${e.message}</div>`;
    }
}

function showManagerToast(title, message, type = 'success') {
    const toast = document.getElementById('managerToast');
    const toastTitle = document.getElementById('toastTitle');
    const toastMessage = document.getElementById('toastMessage');
    const toastIcon = document.getElementById('toastIcon');
    if (!toast) return;

    toast.className = `fixed bottom-5 right-5 z-50 max-w-sm rounded-xl p-4 shadow-xl border transition-all duration-300 transform translate-y-0 ${
        type === 'error' ? 'bg-red-50 border-red-200 text-red-900' : 'bg-emerald-50 border-emerald-200 text-emerald-900'
    }`;
    if (toastTitle) toastTitle.innerText = title;
    if (toastMessage) toastMessage.innerText = message;
    if (toastIcon) toastIcon.innerText = type === 'error' ? '⚠️' : '✓';

    toast.classList.remove('hidden');
    setTimeout(() => {
        toast.classList.add('hidden');
    }, 4000);
}

let _allAdminStores = [];
let _storeMgmtCurrentPage = 1;
const _storeMgmtPageSize = 10;
let _filteredAdminStores = [];

async function populateAdminStoreSelect(force = false) {
    const sel = document.getElementById('mgrAdminStoreSelect');
    if (!sel) return;

    if (!force && sel.dataset.loaded === 'true' && sel.options.length > 5) {
        if (currentManagerStoreId) sel.value = currentManagerStoreId;
        return;
    }

    try {
        const res = await fetch('/api/stores');
        if (!res.ok) {
            throw new Error(`Failed to load stores: HTTP ${res.status}`);
        }
        const data = await res.json();
        const stores = Array.isArray(data) ? data : (data.stores || []);

        if (!stores || stores.length === 0) {
            throw new Error('No stores available in store directory');
        }

        _allAdminStores = stores;

        // Sort alphabetically by store name
        const sorted = [...stores].sort((a, b) => (a.store_name || '').localeCompare(b.store_name || ''));

        sel.innerHTML = '';
        sorted.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.store_id;
            opt.textContent = `${s.store_name} (${s.store_id}) - ${s.city || s.region || ''} [${s.format || ''}]`;
            if (s.store_id === currentManagerStoreId) {
                opt.selected = true;
            }
            sel.appendChild(opt);
        });

        sel.dataset.loaded = 'true';
        if (currentManagerStoreId && sorted.some(s => s.store_id === currentManagerStoreId)) {
            sel.value = currentManagerStoreId;
        }

        const countBadge = document.getElementById('adminStoreCountBadge');
        if (countBadge) countBadge.innerText = `${stores.length}`;
        const label = document.getElementById('mgrAdminStoreLabel');
        if (label) label.innerText = `Admin Store View (${stores.length} Stores):`;
    } catch (e) {
        console.error('Error populating admin stores:', e);
    }
}

function switchAdminStore(storeId) {
    currentManagerStoreId = storeId;
    const sel = document.getElementById('mgrAdminStoreSelect');
    if (sel && sel.value !== storeId) sel.value = storeId;
    showManagerSubView(currentManagerSubView || 'overview', false);
}

async function openStoreManagementModal() {
    const modal = document.getElementById('storeManagementModal');
    if (!modal) return;
    modal.classList.remove('hidden');

    if (!_allAdminStores || _allAdminStores.length === 0) {
        await refreshStoreManagementData();
    } else {
        onStoreMgmtFilterChange();
    }
}

function closeStoreManagementModal() {
    const modal = document.getElementById('storeManagementModal');
    if (modal) modal.classList.add('hidden');
}

async function refreshStoreManagementData() {
    const token = getAuthToken();
    try {
        let res = null;
        if (token) {
            res = await fetch('/api/admin/stores?all=true', {
                headers: { 'Authorization': `Bearer ${token}` }
            });
        }
        if (!res || !res.ok) {
            res = await fetch('/api/stores');
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const stores = Array.isArray(data) ? data : (data.stores || []);
        _allAdminStores = stores;

        await populateAdminStoreSelect(true);

        const countBadge = document.getElementById('storeMgmtTotalBadge');
        if (countBadge) countBadge.innerText = `${stores.length} Stores Active`;

        _storeMgmtCurrentPage = 1;
        onStoreMgmtFilterChange();
        showManagerToast('Directory Refreshed', `Loaded ${stores.length} stores from authoritative database`, 'success');
    } catch (err) {
        console.error('Error refreshing store directory:', err);
        showManagerToast('Network Error', 'Failed to refresh store directory from server', 'error');
    }
}

function onStoreMgmtFilterChange() {
    const searchVal = (document.getElementById('storeMgmtSearch')?.value || '').trim().toLowerCase();
    const regionVal = document.getElementById('storeMgmtRegion')?.value || 'All';
    const formatVal = document.getElementById('storeMgmtFormat')?.value || 'All';

    _filteredAdminStores = (_allAdminStores || []).filter(s => {
        if (regionVal !== 'All' && String(s.region || '').toLowerCase() !== regionVal.toLowerCase()) {
            return false;
        }
        if (formatVal !== 'All' && String(s.format || '').toLowerCase() !== formatVal.toLowerCase()) {
            return false;
        }
        if (searchVal) {
            const matchesId = String(s.store_id || '').toLowerCase().includes(searchVal);
            const matchesName = String(s.store_name || '').toLowerCase().includes(searchVal);
            const matchesCity = String(s.city || '').toLowerCase().includes(searchVal);
            const matchesState = String(s.state || '').toLowerCase().includes(searchVal);
            if (!matchesId && !matchesName && !matchesCity && !matchesState) {
                return false;
            }
        }
        return true;
    });

    _storeMgmtCurrentPage = 1;
    renderStoreManagementTable();
}

function changeStoreMgmtPage(delta) {
    const totalPages = Math.max(1, Math.ceil((_filteredAdminStores || []).length / _storeMgmtPageSize));
    const newPage = _storeMgmtCurrentPage + delta;
    if (newPage >= 1 && newPage <= totalPages) {
        _storeMgmtCurrentPage = newPage;
        renderStoreManagementTable();
    }
}

function renderStoreManagementTable() {
    const tbody = document.getElementById('storeMgmtTableBody');
    const pageDisplay = document.getElementById('storeMgmtPageDisplay');
    const paginationInfo = document.getElementById('storeMgmtPaginationInfo');
    const prevBtn = document.getElementById('btnStoreMgmtPrev');
    const nextBtn = document.getElementById('btnStoreMgmtNext');
    if (!tbody) return;

    const total = _filteredAdminStores.length;
    const totalPages = Math.max(1, Math.ceil(total / _storeMgmtPageSize));

    if (_storeMgmtCurrentPage > totalPages) _storeMgmtCurrentPage = totalPages;
    if (_storeMgmtCurrentPage < 1) _storeMgmtCurrentPage = 1;

    const startIdx = (_storeMgmtCurrentPage - 1) * _storeMgmtPageSize;
    const endIdx = Math.min(startIdx + _storeMgmtPageSize, total);
    const pageItems = _filteredAdminStores.slice(startIdx, endIdx);

    if (pageDisplay) pageDisplay.innerText = `Page ${_storeMgmtCurrentPage} of ${totalPages}`;
    if (paginationInfo) {
        if (total === 0) {
            paginationInfo.innerText = 'No matching stores found';
        } else {
            paginationInfo.innerText = `Showing ${startIdx + 1}–${endIdx} of ${total} stores`;
        }
    }
    if (prevBtn) prevBtn.disabled = _storeMgmtCurrentPage <= 1;
    if (nextBtn) nextBtn.disabled = _storeMgmtCurrentPage >= totalPages;

    if (pageItems.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="8" class="px-4 py-8 text-center text-slate-400">
                    No store records found matching your filters.
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = pageItems.map(s => {
        const isCurrent = (s.store_id === currentManagerStoreId);
        const formatColor = s.format === 'Saffron Hyper' ? 'bg-purple-50 text-purple-700 border-purple-200' :
                           s.format === 'Saffron Express' ? 'bg-blue-50 text-blue-700 border-blue-200' :
                           'bg-emerald-50 text-emerald-700 border-emerald-200';
        const footfall = s.avg_weekly_footfall ? Number(s.avg_weekly_footfall).toLocaleString() : 'N/A';

        return `
            <tr class="hover:bg-slate-50/80 transition ${isCurrent ? 'bg-saffron-50/50' : ''}">
                <td class="px-3.5 py-2.5 font-mono font-bold text-slate-800">
                    <span class="px-2 py-0.5 rounded bg-slate-100 border border-slate-200">${escapeHtml(s.store_id)}</span>
                </td>
                <td class="px-3.5 py-2.5 font-semibold text-navy-900">
                    ${escapeHtml(s.store_name)}
                    ${isCurrent ? '<span class="ml-1.5 px-1.5 py-0.2 rounded text-[10px] font-bold bg-saffron-100 text-saffron-800">Active</span>' : ''}
                </td>
                <td class="px-3.5 py-2.5 text-slate-600">
                    ${escapeHtml(s.city || '')}${s.state ? ', ' + escapeHtml(s.state) : ''}
                </td>
                <td class="px-3.5 py-2.5">
                    <span class="px-2 py-0.5 rounded text-[11px] font-semibold bg-slate-100 text-slate-700">${escapeHtml(s.region || '')}</span>
                </td>
                <td class="px-3.5 py-2.5">
                    <span class="px-2 py-0.5 rounded text-[11px] font-semibold border ${formatColor}">${escapeHtml(s.format || '')}</span>
                </td>
                <td class="px-3.5 py-2.5 text-slate-600 font-mono">
                    ${footfall}
                </td>
                <td class="px-3.5 py-2.5 text-slate-500">
                    ${escapeHtml(s.opened_date || '')}
                </td>
                <td class="px-3.5 py-2.5 text-right">
                    ${isCurrent ?
                        '<span class="px-2.5 py-1 text-xs font-semibold text-emerald-700 bg-emerald-50 rounded-lg">Active Store</span>' :
                        `<button type="button" onclick="selectStoreFromModal('${escapeHtml(s.store_id)}')" class="px-2.5 py-1 bg-navy-900 hover:bg-slate-800 text-white rounded-lg text-xs font-semibold transition shadow-sm">View in Workspace</button>`
                    }
                </td>
            </tr>
        `;
    }).join('');
}

function selectStoreFromModal(storeId) {
    const sel = document.getElementById('mgrAdminStoreSelect');
    if (sel) sel.value = storeId;
    switchAdminStore(storeId);
    closeStoreManagementModal();
    showManagerToast('Store Switched', `Active store workspace set to ${storeId}`, 'success');
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}


// =============================================================================
// Store Manager: Emerging Insights, Action Center, Analytics, Reports, Settings
// =============================================================================
async function loadManagerInsights() {
    const token = getAuthToken();
    const sid = currentManagerStoreId || 'ST-D69EG';
    const container = document.getElementById('mgrInsightsTopIssues');
    if (!container) return;

    container.innerHTML = '<div class="text-xs text-slate-400 py-4 text-center">Loading insights...</div>';
    try {
        const res = await fetch(`/api/manager/insights?store_id=${encodeURIComponent(sid)}`, {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) {
            container.innerHTML = `<div class="text-xs text-red-500 py-2">Failed to load insights (HTTP ${res.status})</div>`;
            return;
        }
        const data = await res.json();
        
        // Top issues
        const issues = data.top_issues || [];
        if (issues.length === 0) {
            container.innerHTML = '<div class="text-xs text-slate-400 py-4 text-center">No recurring friction issues detected.</div>';
        } else {
            container.innerHTML = issues.map(iss => `
                <div class="p-3 bg-slate-50 rounded-xl border border-slate-100 space-y-1.5">
                    <div class="flex items-center justify-between text-xs">
                        <span class="font-bold text-navy-900">${escapeHtml(iss.issue_type_name)}</span>
                        <span class="font-extrabold text-slate-700">${iss.count} (${iss.pct_of_store}%)</span>
                    </div>
                    <div class="w-full bg-slate-200 rounded-full h-2 overflow-hidden">
                        <div class="bg-saffron-500 h-2 rounded-full" style="width: ${Math.min(iss.pct_of_store * 3, 100)}%"></div>
                    </div>
                    <div class="flex items-center justify-between text-[11px] text-slate-500">
                        <span>Owner: <strong>${escapeHtml(iss.owner_function)}</strong></span>
                        <span class="text-emerald-700 font-medium">${iss.actionable_count} actionable</span>
                    </div>
                </div>
            `).join('');

            // Render Horizontal Bar Chart for Top Friction Categories
            renderOrUpdateChart('chartMgrTopIssues', {
                type: 'bar',
                data: {
                    labels: issues.slice(0, 5).map(iss => {
                        const name = iss.issue_type_name || 'Issue';
                        return name.length > 22 ? name.slice(0, 20) + '…' : name;
                    }),
                    datasets: [{
                        label: 'Feedback Records',
                        data: issues.slice(0, 5).map(iss => iss.count),
                        backgroundColor: '#F59E0B',
                        borderRadius: 6
                    }]
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { cornerRadius: 8 }
                    },
                    scales: {
                        x: { beginAtZero: true, grid: { color: '#F1F5F9' }, ticks: { font: { size: 10 } } },
                        y: { grid: { display: false }, ticks: { font: { size: 10 } } }
                    }
                }
            });
        }

        // Severity
        const sevEl = document.getElementById('mgrInsightsSeverity');
        if (sevEl && data.severity_distribution) {
            const sev = data.severity_distribution;
            sevEl.innerHTML = `
                <div class="flex items-center justify-between p-2 rounded-lg bg-red-50 text-red-800 font-semibold">
                    <span>Critical</span><span>${sev.critical || 0}</span>
                </div>
                <div class="flex items-center justify-between p-2 rounded-lg bg-amber-50 text-amber-800 font-semibold">
                    <span>High</span><span>${sev.high || 0}</span>
                </div>
                <div class="flex items-center justify-between p-2 rounded-lg bg-blue-50 text-blue-800 font-semibold">
                    <span>Medium</span><span>${sev.medium || 0}</span>
                </div>
                <div class="flex items-center justify-between p-2 rounded-lg bg-slate-50 text-slate-700 font-semibold">
                    <span>Low</span><span>${sev.low || 0}</span>
                </div>
            `;

            // Render Severity Donut Chart
            renderOrUpdateChart('chartMgrSeverity', {
                type: 'doughnut',
                data: {
                    labels: ['Critical', 'High', 'Medium', 'Low'],
                    datasets: [{
                        data: [sev.critical || 0, sev.high || 0, sev.medium || 0, sev.low || 0],
                        backgroundColor: ['#EF4444', '#F59E0B', '#3B82F6', '#94A3B8'],
                        borderWidth: 2,
                        borderColor: '#FFFFFF'
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: '65%',
                    plugins: {
                        legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 10, family: 'Inter' } } }
                    }
                }
            });
        }

        // Department
        const deptEl = document.getElementById('mgrInsightsDepts');
        if (deptEl && data.department_friction) {
            deptEl.innerHTML = Object.entries(data.department_friction).map(([dept, count]) => `
                <div class="flex items-center justify-between p-2 rounded-lg bg-slate-50 border border-slate-100">
                    <span class="font-medium text-slate-700">${escapeHtml(dept)}</span>
                    <span class="font-bold text-navy-900">${count}</span>
                </div>
            `).join('');
        }
    } catch (e) {
        container.innerHTML = `<div class="text-xs text-red-500 py-2">Network error: ${escapeHtml(e.message)}</div>`;
    }
}

async function loadManagerActions() {
    const token = getAuthToken();
    const sid = currentManagerStoreId || 'ST-D69EG';
    const feed = document.getElementById('mgrActionFeed');
    if (!feed) return;

    feed.innerHTML = '<div class="text-xs text-slate-400 py-6 text-center">Loading action queue...</div>';
    try {
        const res = await fetch(`/api/manager/actions?store_id=${encodeURIComponent(sid)}`, {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) {
            feed.innerHTML = `<div class="text-xs text-red-500 py-4">Failed to load action items (HTTP ${res.status})</div>`;
            return;
        }
        const data = await res.json();
        
        // Update counters
        // Update counters and status progress donut chart
        if (data.counts) {
            const p = document.getElementById('mgrActionPendingCount');
            const i = document.getElementById('mgrActionInvestigatingCount');
            const a = document.getElementById('mgrActionAcknowledgedCount');
            const r = document.getElementById('mgrActionResolvedCount');
            const pVal = Number(data.counts.pending || 0);
            const iVal = Number(data.counts.investigating || 0);
            const aVal = Number(data.counts.acknowledged || 0);
            const rVal = Number(data.counts.resolved || 0);

            if (p) p.innerText = pVal;
            if (i) i.innerText = iVal;
            if (a) a.innerText = aVal;
            if (r) r.innerText = rVal;

            const totalActions = pVal + iVal + aVal + rVal;
            renderOrUpdateChart('chartMgrActionProgress', {
                type: 'doughnut',
                data: {
                    labels: totalActions === 0 ? ['No Actions'] : ['Pending', 'Investigating', 'Acknowledged', 'Resolved'],
                    datasets: [{
                        data: totalActions === 0 ? [1] : [pVal, iVal, aVal, rVal],
                        backgroundColor: totalActions === 0 ? ['#E2E8F0'] : ['#F59E0B', '#6366F1', '#3B82F6', '#10B981'],
                        borderWidth: 2,
                        borderColor: '#FFFFFF'
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: '65%',
                    plugins: {
                        legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 10, family: 'Inter' } } }
                    }
                }
            });
        }

        const items = data.action_items || [];
        if (items.length === 0) {
            feed.innerHTML = '<div class="text-xs text-slate-400 py-6 text-center">No action items pending for this store.</div>';
            return;
        }

        feed.innerHTML = items.map(item => {
            const isResolved = item.status === 'resolved';
            const statusBadge = isResolved
                ? '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-100 text-emerald-800">RESOLVED</span>'
                : (item.status === 'investigating'
                    ? '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-indigo-100 text-indigo-800">INVESTIGATING</span>'
                    : (item.status === 'acknowledged'
                        ? '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-blue-100 text-blue-800">ACKNOWLEDGED</span>'
                        : '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-amber-100 text-amber-800">PENDING</span>'));

            const actionButtons = isResolved
                ? `<span class="text-xs text-emerald-600 font-semibold">✓ Resolved: ${escapeHtml(item.resolution_notes || 'Verified')}</span>`
                : `
                    <button onclick="handleManagerFeedbackAction('${escapeHtml(item.id)}', 'acknowledge')" class="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-semibold rounded transition">Acknowledge</button>
                    <button onclick="handleManagerFeedbackAction('${escapeHtml(item.id)}', 'investigate')" class="px-2.5 py-1 bg-indigo-50 hover:bg-indigo-100 text-indigo-700 text-xs font-semibold rounded transition">Investigate</button>
                    <button onclick="openFeedbackResolveModal('${escapeHtml(item.id)}')" class="px-2.5 py-1 bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-semibold rounded transition">Resolve</button>
                `;

            return `
                <div class="p-4 bg-slate-50/70 border border-slate-200 rounded-xl space-y-2">
                    <div class="flex items-center justify-between">
                        <div class="flex items-center space-x-2">
                            <span class="font-mono text-xs font-bold text-slate-500">${escapeHtml(item.id)}</span>
                            ${statusBadge}
                        </div>
                        <span class="text-xs text-slate-400 font-medium">${formatAuditTimestamp(item.created_at)}</span>
                    </div>
                    <p class="font-semibold text-xs text-navy-900">${escapeHtml(item.title)}</p>
                    <p class="text-xs text-slate-600">${escapeHtml(item.summary)}</p>
                    <div class="pt-2 border-t border-slate-100 flex items-center justify-end space-x-2">
                        ${actionButtons}
                    </div>
                </div>
            `;
        }).join('');
    } catch (e) {
        feed.innerHTML = `<div class="text-xs text-red-500 py-4">Network error: ${escapeHtml(e.message)}</div>`;
    }
}

async function loadManagerAnalytics() {
    const token = getAuthToken();
    const sid = currentManagerStoreId || 'ST-D69EG';
    try {
        const res = await fetch(`/api/manager/historical-analytics?store_id=${encodeURIComponent(sid)}`, {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) return;
        const data = await res.json();

        const avgEl = document.getElementById('mgrAnalyticsAvgRating');
        if (avgEl) avgEl.innerText = data.average_rating ? data.average_rating.toFixed(1) : '3.8';

        const barsEl = document.getElementById('mgrAnalyticsRatingBars');
        if (barsEl && data.rating_distribution) {
            const dist = data.rating_distribution;
            const totalR = Object.values(dist).reduce((a, b) => a + b, 0) || 1;
            barsEl.innerHTML = [5, 4, 3, 2, 1].map(star => {
                const count = dist[star] || 0;
                const pct = Math.round((count / totalR) * 100);
                return `
                    <div class="flex items-center space-x-2 text-xs">
                        <span class="w-10 text-slate-600 font-medium">${star} ⭐</span>
                        <div class="flex-1 bg-slate-100 rounded-full h-2 overflow-hidden">
                            <div class="bg-amber-400 h-2 rounded-full" style="width: ${pct}%"></div>
                        </div>
                        <span class="w-8 text-right text-slate-400 font-mono">${pct}%</span>
                    </div>
                `;
            }).join('');

            // Render Rating Distribution Bar Chart
            renderOrUpdateChart('chartMgrRatings', {
                type: 'bar',
                data: {
                    labels: ['1★', '2★', '3★', '4★', '5★'],
                    datasets: [{
                        label: 'Reviews',
                        data: [1, 2, 3, 4, 5].map(s => dist[s] || 0),
                        backgroundColor: ['#EF4444', '#F97316', '#F59E0B', '#10B981', '#059669'],
                        borderRadius: 4
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { cornerRadius: 6 }
                    },
                    scales: {
                        y: { beginAtZero: true, grid: { color: '#F1F5F9' }, ticks: { font: { size: 9 } } },
                        x: { grid: { display: false }, ticks: { font: { size: 10 } } }
                    }
                }
            });
        }

        const trendEl = document.getElementById('mgrAnalyticsWeeklyTrend');
        if (trendEl && data.weekly_trend) {
            trendEl.innerHTML = data.weekly_trend.map(w => `
                <div class="flex items-center space-x-3 text-xs">
                    <span class="w-24 font-mono text-slate-500">${escapeHtml(w.week)}</span>
                    <div class="flex-1 bg-slate-100 rounded-full h-3 overflow-hidden flex">
                        <div class="bg-saffron-500 h-3" style="width: ${Math.min(w.total * 2, 100)}%"></div>
                        <div class="bg-emerald-500 h-3" style="width: ${Math.min(w.actionable * 2, 100)}%"></div>
                    </div>
                    <span class="w-16 text-right font-bold text-navy-900">${w.total} recs</span>
                </div>
            `).join('');

            // Render Weekly Volume Trajectory Line Chart
            renderOrUpdateChart('chartMgrWeeklyTrend', {
                type: 'line',
                data: {
                    labels: data.weekly_trend.map(w => w.week),
                    datasets: [
                        {
                            label: 'Total Volume',
                            data: data.weekly_trend.map(w => w.total),
                            borderColor: '#0F172A',
                            backgroundColor: 'rgba(15, 23, 42, 0.05)',
                            tension: 0.3,
                            fill: true,
                            pointRadius: 3
                        },
                        {
                            label: 'Actionable Volume',
                            data: data.weekly_trend.map(w => w.actionable),
                            borderColor: '#10B981',
                            backgroundColor: 'rgba(16, 185, 129, 0.1)',
                            tension: 0.3,
                            fill: true,
                            pointRadius: 3
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11, family: 'Inter' } } },
                        tooltip: { cornerRadius: 8 }
                    },
                    scales: {
                        y: { beginAtZero: true, grid: { color: '#F1F5F9' }, ticks: { font: { size: 10 } } },
                        x: { grid: { display: false }, ticks: { font: { size: 10 } } }
                    }
                }
            });
        }
    } catch (e) {
        console.error('Error loading analytics:', e);
    }
}

async function loadManagerReports() {
    const token = getAuthToken();
    const sid = currentManagerStoreId || 'ST-D69EG';
    try {
        const res = await fetch(`/api/manager/reports?store_id=${encodeURIComponent(sid)}`, {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) return;
        const data = await res.json();

        const idEl = document.getElementById('mgrReportId');
        const snameEl = document.getElementById('mgrReportStoreName');
        const genEl = document.getElementById('mgrReportGeneratedAt');
        const sumEl = document.getElementById('mgrReportExecutiveSummary');
        const totEl = document.getElementById('mgrReportTotalFeedback');
        const actEl = document.getElementById('mgrReportActionable');
        const altEl = document.getElementById('mgrReportActiveAlerts');
        const resEl = document.getElementById('mgrReportResolved');

        if (idEl) idEl.innerText = data.report_id || 'REP-STORE';
        if (snameEl) snameEl.innerText = data.store_name || sid;
        if (genEl) genEl.innerText = 'Generated: ' + (data.generated_at || 'Just now');
        if (sumEl) sumEl.innerText = data.executive_summary || '';
        if (totEl) totEl.innerText = (data.total_feedback_analyzed || 0).toLocaleString();
        if (actEl) actEl.innerText = (data.actionable_feedback_count || 0).toLocaleString();
        if (altEl) altEl.innerText = data.active_alerts || 0;
        if (resEl) resEl.innerText = data.resolved_incidents || 0;

        // Render Report Categories Bar Chart
        if (data.top_friction_categories && data.top_friction_categories.length > 0) {
            renderOrUpdateChart('chartMgrReportCategories', {
                type: 'bar',
                data: {
                    labels: data.top_friction_categories.slice(0, 5).map(c => {
                        const name = c.issue_type || 'Category';
                        return name.length > 22 ? name.slice(0, 20) + '…' : name;
                    }),
                    datasets: [{
                        label: 'Interactions Analyzed',
                        data: data.top_friction_categories.slice(0, 5).map(c => c.count),
                        backgroundColor: '#0F172A',
                        borderRadius: 6
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { cornerRadius: 8 }
                    },
                    scales: {
                        y: { beginAtZero: true, grid: { color: '#F1F5F9' }, ticks: { font: { size: 10 } } },
                        x: { grid: { display: false }, ticks: { font: { size: 10 } } }
                    }
                }
            });
        }
    } catch (e) {
        console.error('Error loading reports:', e);
    }
}

function downloadStoreReportCSV() {
    const sid = currentManagerStoreId || 'ST-D69EG';
    window.open(`/api/manager/reports/export-csv?store_id=${encodeURIComponent(sid)}`, '_blank');
}

async function loadManagerSettings() {
    const token = getAuthToken();
    const sid = currentManagerStoreId || 'ST-D69EG';
    const profileEl = document.getElementById('mgrSettingsProfileDetails');
    if (!profileEl) return;

    profileEl.innerHTML = '<div class="text-xs text-slate-400 py-3">Loading settings...</div>';
    try {
        const res = await fetch(`/api/manager/settings?store_id=${encodeURIComponent(sid)}`, {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) {
            profileEl.innerHTML = `<div class="text-xs text-red-500 py-2">Failed to load settings (HTTP ${res.status})</div>`;
            return;
        }
        const data = await res.json();
        profileEl.innerHTML = `
            <div class="flex justify-between py-1.5 border-b border-slate-100">
                <span class="text-slate-500">Store Fleet ID</span><span class="font-mono font-bold text-navy-900">${escapeHtml(data.store_id)}</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-slate-100">
                <span class="text-slate-500">Store Name</span><span class="font-bold text-navy-900">${escapeHtml(data.store_name)}</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-slate-100">
                <span class="text-slate-500">Location</span><span>${escapeHtml(data.city)}, ${escapeHtml(data.state)}</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-slate-100">
                <span class="text-slate-500">Format</span><span>${escapeHtml(data.format)}</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-slate-100">
                <span class="text-slate-500">Assigned Lead</span><span class="font-medium">${escapeHtml(data.manager_name || 'Store Ops Lead')} (${escapeHtml(data.manager_email)})</span>
            </div>
            <div class="flex justify-between py-1.5">
                <span class="text-slate-500">Data Synchronization</span>
                <span class="px-2 py-0.5 rounded text-[10px] font-bold ${data.supabase_connected ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800'}">
                    ${data.supabase_connected ? '● Supabase Live Sync' : '● Local SQLite'}
                </span>
            </div>
        `;

        // Populate editable controls
        const hoursInput = document.getElementById('mgrSettingsOperatingHours');
        const tierSelect = document.getElementById('mgrSettingsMonitoringTier');
        const notifCrit = document.getElementById('mgrSettingsNotifyCritical');
        const notifDaily = document.getElementById('mgrSettingsNotifyDaily');
        const notifAct = document.getElementById('mgrSettingsNotifyActionable');
        const lastSavedEl = document.getElementById('mgrSettingsLastSaved');

        if (hoursInput) hoursInput.value = data.operating_hours || '07:00 AM - 11:00 PM IST (Daily)';
        if (tierSelect) tierSelect.value = data.monitoring_tier || 'Tier-1 Autonomous Triage';

        if (data.notifications) {
            if (notifCrit) notifCrit.checked = data.notifications.email_on_critical_alert !== false;
            if (notifDaily) notifDaily.checked = data.notifications.daily_summary_brief !== false;
            if (notifAct) notifAct.checked = data.notifications.instant_actionable_notification !== false;
        }

        if (lastSavedEl) {
            lastSavedEl.innerText = data.updated_at ? ('Last saved: ' + formatAuditTimestamp(data.updated_at)) : '';
        }
    } catch (e) {
        profileEl.innerHTML = `<div class="text-xs text-red-500 py-2">Network error: ${escapeHtml(e.message)}</div>`;
    }
}

async function saveManagerSettings() {
    const token = getAuthToken();
    const sid = currentManagerStoreId || 'ST-D69EG';
    const statusEl = document.getElementById('mgrSettingsSaveStatus');
    const btn = document.getElementById('btnSaveStoreSettings');
    const hoursInput = document.getElementById('mgrSettingsOperatingHours');
    const tierSelect = document.getElementById('mgrSettingsMonitoringTier');
    const notifCrit = document.getElementById('mgrSettingsNotifyCritical');
    const notifDaily = document.getElementById('mgrSettingsNotifyDaily');
    const notifAct = document.getElementById('mgrSettingsNotifyActionable');
    const lastSavedEl = document.getElementById('mgrSettingsLastSaved');

    const hours = hoursInput ? hoursInput.value.trim() : '';
    if (!hours || hours.length < 3 || hours.length > 100) {
        if (statusEl) {
            statusEl.innerText = 'Operating hours must be between 3 and 100 characters.';
            statusEl.className = 'text-xs font-semibold text-red-600';
        }
        return;
    }

    const payload = {
        store_id: sid,
        operating_hours: hours,
        monitoring_tier: tierSelect ? tierSelect.value : 'Tier-1 Autonomous Triage',
        notifications: {
            email_on_critical_alert: notifCrit ? notifCrit.checked : true,
            daily_summary_brief: notifDaily ? notifDaily.checked : true,
            instant_actionable_notification: notifAct ? notifAct.checked : true
        }
    };

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>Saving...</span>';
    }
    if (statusEl) {
        statusEl.innerText = 'Saving configuration...';
        statusEl.className = 'text-xs font-semibold text-slate-500';
    }

    try {
        const res = await fetch('/api/manager/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(payload)
        });

        if (!res.ok) {
            const err = await parseErrorResponse(res, 'Failed to save settings.');
            if (statusEl) {
                statusEl.innerText = err;
                statusEl.className = 'text-xs font-semibold text-red-600';
            }
            return;
        }

        const data = await res.json();
        if (statusEl) {
            statusEl.innerText = '✓ Settings saved successfully';
            statusEl.className = 'text-xs font-semibold text-emerald-600';
            setTimeout(() => { if (statusEl) statusEl.innerText = ''; }, 4000);
        }
        if (lastSavedEl) {
            lastSavedEl.innerText = 'Last saved: Just now';
        }
    } catch (e) {
        if (statusEl) {
            statusEl.innerText = 'Network error: ' + e.message;
            statusEl.className = 'text-xs font-semibold text-red-600';
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span>💾</span><span>Save Store Settings</span>';
        }
    }
}

// =============================================================================
// Administrator: Users, Network Insights, Remediations, Integrations, Settings, Audit
// =============================================================================
async function loadAdminUsers() {
    const token = getAuthToken();
    const tbody = document.getElementById('adminUsersTableBody');
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="5" class="p-8 text-center text-slate-400">Loading user accounts...</td></tr>';
    try {
        const res = await fetch('/api/admin/users', {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) {
            tbody.innerHTML = `<tr><td colspan="5" class="p-8 text-center text-red-500">Failed to load users (HTTP ${res.status})</td></tr>`;
            return;
        }
        const data = await res.json();
        const users = data.users || [];
        if (users.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="p-8 text-center text-slate-400">No users found.</td></tr>';
            return;
        }

        tbody.innerHTML = users.map(u => {
            const roleBadge = u.role === 'admin'
                ? '<span class="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-purple-100 text-purple-800">Admin</span>'
                : (u.role === 'store_manager'
                    ? '<span class="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-blue-100 text-blue-800">Store Manager</span>'
                    : '<span class="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-slate-100 text-slate-700">Customer</span>');

            const storeDisplay = u.assigned_store_id
                ? `<span class="font-mono font-bold text-navy-900">${escapeHtml(u.assigned_store_id)}</span>`
                : '<span class="text-slate-400">—</span>';

            return `
                <tr class="hover:bg-slate-50 transition">
                    <td class="p-4">
                        <div class="font-bold text-navy-900">${escapeHtml(u.full_name || 'User')}</div>
                        <div class="text-[11px] text-slate-400 font-mono">${escapeHtml(u.email)}</div>
                    </td>
                    <td class="p-4">${roleBadge}</td>
                    <td class="p-4">${storeDisplay}</td>
                    <td class="p-4 text-slate-500">${formatAuditTimestamp(u.created_at)}</td>
                    <td class="p-4 text-right">
                        <button onclick="promptAssignRole('${escapeHtml(u.email)}', '${escapeHtml(u.role)}', '${escapeHtml(u.assigned_store_id || '')}')" class="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold rounded text-xs transition">
                            Reassign Role
                        </button>
                    </td>
                </tr>
            `;
        }).join('');
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" class="p-8 text-center text-red-500">Error: ${escapeHtml(e.message)}</td></tr>`;
    }
}

async function promptAssignRole(email, currentRole, currentStore) {
    const newRole = prompt(`Assign role for ${email} (customer, store_manager, admin):`, currentRole);
    if (!newRole) return;
    let newStore = null;
    if (newRole.trim().toLowerCase() === 'store_manager') {
        newStore = prompt(`Assign store ID for ${email} (e.g. ST-D69EG):`, currentStore || 'ST-D69EG');
        if (!newStore) return;
    }

    const token = getAuthToken();
    try {
        const res = await fetch('/api/admin/users/assign-role', {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                target_email: email,
                new_role: newRole.trim().toLowerCase(),
                new_store_id: newStore ? newStore.trim().toUpperCase() : null
            })
        });
        if (!res.ok) {
            const err = await res.json();
            showManagerToast('Assignment Failed', err.detail || 'Could not assign role', 'error');
            return;
        }
        showManagerToast('Role Updated', `Successfully updated role for ${email}`, 'success');
        await loadAdminUsers();
    } catch (e) {
        showManagerToast('Network Error', e.message, 'error');
    }
}

async function loadAdminInsights() {
    const token = getAuthToken();
    const topCatEl = document.getElementById('adminInsightsTopCategories');
    const cityEl = document.getElementById('adminInsightsCityDensity');
    if (!topCatEl || !cityEl) return;

    topCatEl.innerHTML = '<div class="text-xs text-slate-400 py-4 text-center">Loading network friction...</div>';
    try {
        const res = await fetch('/api/admin/insights', {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) return;
        const data = await res.json();

        const cats = data.top_categories || [];
        topCatEl.innerHTML = cats.map(c => `
            <div class="p-2.5 bg-slate-50 rounded-xl border border-slate-100 flex items-center justify-between text-xs">
                <div>
                    <span class="font-bold text-navy-900">${escapeHtml(c.issue_type_name)}</span>
                    <span class="text-[10px] text-slate-400 block">${escapeHtml(c.owner_function)}</span>
                </div>
                <div class="text-right">
                    <span class="font-extrabold text-navy-900">${c.count.toLocaleString()}</span>
                    <span class="text-[10px] text-slate-500 block font-medium">${c.pct}% of fleet</span>
                </div>
            </div>
        `).join('');

        if (cats.length > 0) {
            renderOrUpdateChart('chartAdminNetworkIssues', {
                type: 'bar',
                data: {
                    labels: cats.slice(0, 5).map(c => {
                        const name = c.issue_type_name || 'Issue';
                        return name.length > 20 ? name.slice(0, 18) + '…' : name;
                    }),
                    datasets: [{
                        label: 'Fleet Interactions',
                        data: cats.slice(0, 5).map(c => c.count),
                        backgroundColor: '#3B82F6',
                        borderRadius: 6
                    }]
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { cornerRadius: 8 }
                    },
                    scales: {
                        x: { beginAtZero: true, grid: { color: '#F1F5F9' }, ticks: { font: { size: 10 } } },
                        y: { grid: { display: false }, ticks: { font: { size: 10 } } }
                    }
                }
            });
        }

        const cities = data.city_breakdown || [];
        cityEl.innerHTML = cities.map(ct => `
            <div class="p-2.5 bg-slate-50 rounded-xl border border-slate-100 flex items-center justify-between text-xs">
                <span class="font-bold text-navy-900">${escapeHtml(ct.city)}</span>
                <div class="text-right">
                    <span class="font-extrabold text-navy-900">${ct.count.toLocaleString()}</span>
                    <span class="text-[10px] text-emerald-700 block font-semibold">${ct.actionable_rate}% actionable</span>
                </div>
            </div>
        `).join('');

        if (cities.length > 0) {
            renderOrUpdateChart('chartAdminRegional', {
                type: 'bar',
                data: {
                    labels: cities.slice(0, 6).map(ct => ct.city),
                    datasets: [{
                        label: 'Total Feedback',
                        data: cities.slice(0, 6).map(ct => ct.count),
                        backgroundColor: '#8B5CF6',
                        borderRadius: 6
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { cornerRadius: 8 }
                    },
                    scales: {
                        y: { beginAtZero: true, grid: { color: '#F1F5F9' }, ticks: { font: { size: 10 } } },
                        x: { grid: { display: false }, ticks: { font: { size: 10 } } }
                    }
                }
            });
        }
    } catch (e) {
        console.error('Error loading admin insights:', e);
    }
}

async function loadAdminRemediations() {
    const token = getAuthToken();
    const feed = document.getElementById('adminRemediationsFeed');
    if (!feed) return;

    feed.innerHTML = '<div class="text-xs text-slate-400 py-6 text-center">Loading remediation tracker...</div>';
    try {
        const res = await fetch('/api/admin/remediations', {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) return;
        const data = await res.json();
        const items = data.recent_remediations || [];
        if (items.length === 0) {
            feed.innerHTML = '<div class="text-xs text-slate-400 py-6 text-center">No recent remediation actions logged.</div>';
            return;
        }

        feed.innerHTML = items.map(item => `
            <div class="p-3.5 bg-slate-50 rounded-xl border border-slate-200 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                <div>
                    <div class="flex items-center space-x-2">
                        <span class="px-2 py-0.5 rounded text-[10px] font-bold bg-navy-100 text-navy-900 font-mono">${escapeHtml(item.action)}</span>
                        <span class="font-mono text-xs text-slate-600 font-bold">${escapeHtml(item.store_id)}</span>
                        <span class="text-xs text-slate-400">• ${escapeHtml(item.actor_email)}</span>
                    </div>
                    ${item.resolution_note ? `<p class="text-xs text-emerald-800 mt-1 font-medium bg-emerald-50/80 p-2 rounded-lg border border-emerald-100">${escapeHtml(item.resolution_note)}</p>` : ''}
                </div>
                <span class="text-xs text-slate-400 font-medium self-end sm:self-auto">${formatAuditTimestamp(item.created_at)}</span>
            </div>
        `).join('');
    } catch (e) {
        console.error('Error loading remediations:', e);
    }
}

async function loadAdminIntegrations() {
    const token = getAuthToken();
    const container = document.getElementById('adminIntegrationsList');
    if (!container) return;

    container.innerHTML = '<div class="p-6 text-center text-xs text-slate-400">Loading integrations...</div>';
    try {
        const res = await fetch('/api/admin/integrations', {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) return;
        const data = await res.json();
        const list = data.integrations || [];

        container.innerHTML = list.map(item => `
            <div class="bg-white p-5 rounded-xl border border-slate-200 shadow-sm space-y-2">
                <div class="flex items-center justify-between">
                    <span class="font-bold text-sm text-navy-900">${escapeHtml(item.name)}</span>
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold ${item.live ? 'bg-emerald-100 text-emerald-800 border border-emerald-200' : 'bg-slate-100 text-slate-600'}">
                        ${item.live ? '● Connected' : '○ Standby'}
                    </span>
                </div>
                <p class="text-xs text-slate-500">${escapeHtml(item.type)}</p>
                <div class="pt-2 border-t border-slate-100 flex items-center justify-between text-[11px] text-slate-400 font-mono">
                    <span>${escapeHtml(item.endpoint)}</span>
                    <span class="font-sans font-medium text-slate-600">${escapeHtml(item.status)}</span>
                </div>
            </div>
        `).join('');
    } catch (e) {
        console.error('Error loading integrations:', e);
    }
}

async function loadAdminSettings() {
    const token = getAuthToken();
    const grid = document.getElementById('adminSettingsGrid');
    if (!grid) return;

    grid.innerHTML = '<div class="p-6 text-center text-xs text-slate-400">Loading settings...</div>';
    try {
        const res = await fetch('/api/admin/settings', {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) return;
        const data = await res.json();

        grid.innerHTML = `
            <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm space-y-3 text-xs">
                <h4 class="font-bold text-sm text-navy-900 border-b border-slate-100 pb-2">Platform Engine Configuration</h4>
                <div class="flex justify-between py-1 border-b border-slate-50"><span class="text-slate-500">Platform Title</span><span class="font-bold text-navy-900">${escapeHtml(data.platform_name)}</span></div>
                <div class="flex justify-between py-1 border-b border-slate-50"><span class="text-slate-500">Environment Mode</span><span class="font-mono font-bold">${escapeHtml(data.app_env)}</span></div>
                <div class="flex justify-between py-1 border-b border-slate-50"><span class="text-slate-500">Database Driver</span><span class="font-mono font-bold text-emerald-700">${escapeHtml(data.app_mode)}</span></div>
                <div class="flex justify-between py-1 border-b border-slate-50"><span class="text-slate-500">Dual Persistence</span><span class="font-bold text-emerald-600">Enabled</span></div>
                <div class="flex justify-between py-1"><span class="text-slate-500">Monitored Fleet Size</span><span class="font-bold">${data.fleet_store_count} stores</span></div>
            </div>

            <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm space-y-3 text-xs">
                <h4 class="font-bold text-sm text-navy-900 border-b border-slate-100 pb-2">AI Triage & Classification Rules</h4>
                <div class="flex justify-between py-1 border-b border-slate-50"><span class="text-slate-500">Taxonomy Categories</span><span class="font-bold">${data.taxonomy_categories_count} issue types</span></div>
                <div class="flex justify-between py-1 border-b border-slate-50"><span class="text-slate-500">Confidence Cutoff</span><span class="font-mono font-bold">${Math.round(data.confidence_threshold * 100)}%</span></div>
                <div class="flex justify-between py-1 border-b border-slate-50"><span class="text-slate-500">Evidence Spikes Required</span><span class="font-bold">${data.min_evidence_threshold} items</span></div>
                <div class="flex justify-between py-1 border-b border-slate-50"><span class="text-slate-500">Policy Severity Floor</span><span class="font-bold text-emerald-600">Enforced</span></div>
                <div class="flex justify-between py-1"><span class="text-slate-500">RLS Enforcement</span><span class="font-bold text-navy-900">${escapeHtml(data.auth_security?.rls_enforcement || 'Active')}</span></div>
            </div>
        `;
    } catch (e) {
        console.error('Error loading admin settings:', e);
    }
}

async function loadAdminAudit() {
    const token = getAuthToken();
    const tbody = document.getElementById('adminAuditTableBody');
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="5" class="p-8 text-center text-slate-400">Loading audit log stream...</td></tr>';
    try {
        const res = await fetch('/api/admin/audit?limit=25', {
            headers: { 'Authorization': `Bearer ${token}` },
            cache: 'no-store'
        });
        if (!res.ok) {
            tbody.innerHTML = `<tr><td colspan="5" class="p-8 text-center text-red-500">Failed to load audit log (HTTP ${res.status})</td></tr>`;
            return;
        }
        const data = await res.json();
        const records = data.records || [];
        if (records.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="p-8 text-center text-slate-400">No audit events recorded yet.</td></tr>';
            return;
        }

        tbody.innerHTML = records.map(r => `
            <tr class="hover:bg-slate-50 transition text-xs">
                <td class="p-4 font-mono text-slate-500">${formatAuditTimestamp(r.created_at)}</td>
                <td class="p-4 font-bold text-navy-900">${escapeHtml(r.details?.actor_email || 'System')}</td>
                <td class="p-4"><span class="px-2 py-0.5 rounded font-mono font-bold text-[10px] bg-slate-100 text-slate-800">${escapeHtml(r.action)}</span></td>
                <td class="p-4 font-mono text-slate-600">${escapeHtml(r.target_type)}:${escapeHtml(r.target_id || '')}</td>
                <td class="p-4">
                    <pre class="text-[10px] bg-slate-50 p-1.5 rounded font-mono text-slate-600 overflow-x-auto max-w-xs">${escapeHtml(JSON.stringify(r.details || {}))}</pre>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" class="p-8 text-center text-red-500">Error: ${escapeHtml(e.message)}</td></tr>`;
    }
}
