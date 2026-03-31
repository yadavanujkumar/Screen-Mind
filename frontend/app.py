"""
Screen-Mind Frontend Dashboard
Streamlit multi-tab dashboard for monitoring and controlling the Screen-Mind agent.
"""

import base64
import io
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
import streamlit as st
from PIL import Image

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Screen-Mind Control Center",
    page_icon="🖥️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------

if "api_key" not in st.session_state:
    st.session_state.api_key = ""
if "base_url" not in st.session_state:
    st.session_state.base_url = "http://localhost:8000"
if "connected" not in st.session_state:
    st.session_state.connected = False
if "selected_task_id" not in st.session_state:
    st.session_state.selected_task_id = ""
if "submitted_task_id" not in st.session_state:
    st.session_state.submitted_task_id = None

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _headers() -> Dict[str, str]:
    return {"X-API-Key": st.session_state.api_key, "Content-Type": "application/json"}


def _url(path: str) -> str:
    return f"{st.session_state.base_url.rstrip('/')}{path}"


def api_get(path: str, params: Optional[Dict] = None) -> Optional[Dict]:
    try:
        resp = requests.get(_url(path), headers=_headers(), params=params, timeout=8)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        st.error("❌ Cannot reach API gateway. Check Base URL.")
    except requests.exceptions.Timeout:
        st.error("❌ Request timed out.")
    except requests.exceptions.HTTPError as exc:
        st.error(f"❌ HTTP {exc.response.status_code}: {exc.response.text}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"❌ Unexpected error: {exc}")
    return None


def api_post(path: str, payload: Dict) -> Optional[Dict]:
    try:
        resp = requests.post(_url(path), headers=_headers(), json=payload, timeout=8)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        st.error("❌ Cannot reach API gateway. Check Base URL.")
    except requests.exceptions.Timeout:
        st.error("❌ Request timed out.")
    except requests.exceptions.HTTPError as exc:
        st.error(f"❌ HTTP {exc.response.status_code}: {exc.response.text}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"❌ Unexpected error: {exc}")
    return None


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.image(
        "https://img.icons8.com/fluency/96/monitor.png",
        width=64,
    )
    st.title("Screen-Mind")
    st.caption("Enterprise AI Computer Control Agent")
    st.divider()

    st.subheader("🔌 Connection")
    st.session_state.base_url = st.text_input(
        "Base URL", value=st.session_state.base_url, key="base_url_input"
    )
    st.session_state.api_key = st.text_input(
        "API Key", type="password", value=st.session_state.api_key, key="api_key_input"
    )

    if st.button("Connect", use_container_width=True, type="primary"):
        result = api_get("/api/v1/health")
        if result is not None:
            st.session_state.connected = True
            st.success("✅ Connected")
        else:
            st.session_state.connected = False

    if st.session_state.connected:
        st.success("🟢 Connected")
    else:
        st.warning("🔴 Not connected")

    st.divider()
    st.caption("© 2024 Screen-Mind Systems")

# ---------------------------------------------------------------------------
# Main tabs
# ---------------------------------------------------------------------------

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    [
        "➕ New Task",
        "🔄 Running Tasks",
        "🔍 Task Details",
        "📊 System Metrics",
        "🧠 Memory Browser",
        "🖥️ Live Screen",
    ]
)

# ===========================================================================
# TAB 1 – New Task
# ===========================================================================

with tab1:
    st.header("Submit New Task")
    st.markdown("Describe what the AI agent should do on your screen.")

    col_form, col_info = st.columns([2, 1])

    with col_form:
        task_description = st.text_area(
            "Task Description",
            placeholder="e.g. Open Chrome, navigate to github.com and star the Screen-Mind repository.",
            height=160,
        )
        priority = st.slider("Priority", min_value=1, max_value=10, value=5, step=1)
        st.caption("1 = lowest priority · 10 = highest priority")

        if st.button("🚀 Submit Task", type="primary", use_container_width=True):
            if not task_description.strip():
                st.warning("Please enter a task description.")
            elif not st.session_state.api_key:
                st.warning("Enter your API key in the sidebar first.")
            else:
                with st.spinner("Submitting task…"):
                    result = api_post(
                        "/api/v1/tasks",
                        {
                            "task_description": task_description,
                            "priority": priority,
                        },
                    )
                if result:
                    st.session_state.submitted_task_id = result.get("task_id")
                    st.success(f"✅ Task submitted! ID: `{result.get('task_id')}`")
                    st.json(result)

    with col_info:
        st.markdown("### Tips")
        st.info(
            "**Be specific**: Describe the exact steps you want the agent to take.\n\n"
            "**Priority**: Higher priority tasks run first if the queue is busy.\n\n"
            "**Monitor**: Switch to the **Running Tasks** tab to track progress."
        )
        if st.session_state.submitted_task_id:
            st.markdown("### Last Submitted")
            st.code(st.session_state.submitted_task_id)
            if st.button("View Details →"):
                st.session_state.selected_task_id = st.session_state.submitted_task_id

# ===========================================================================
# TAB 2 – Running Tasks
# ===========================================================================

with tab2:
    st.header("Running Tasks")

    col_ctrl, col_refresh = st.columns([3, 1])
    with col_ctrl:
        auto_refresh = st.checkbox("Auto-refresh (5 s)", value=False)
    with col_refresh:
        manual_refresh = st.button("🔄 Refresh", use_container_width=True)

    tasks_placeholder = st.empty()
    detail_placeholder = st.empty()

    def render_tasks():
        data = api_get("/api/v1/tasks")
        if data is None:
            tasks_placeholder.warning("Could not fetch tasks.")
            return

        tasks: List[Dict[str, Any]] = data.get("tasks", [])
        if not tasks:
            tasks_placeholder.info("No tasks found.")
            return

        rows = []
        for t in tasks:
            rows.append(
                {
                    "Task ID": t.get("task_id", ""),
                    "Status": t.get("status", ""),
                    "Priority": t.get("priority", ""),
                    "Progress %": t.get("progress", 0),
                    "Started": t.get("started_at", ""),
                    "Description": (t.get("task_description", "")[:60] + "…")
                    if len(t.get("task_description", "")) > 60
                    else t.get("task_description", ""),
                }
            )

        with tasks_placeholder.container():
            st.dataframe(
                rows,
                use_container_width=True,
                column_config={
                    "Progress %": st.column_config.ProgressColumn(
                        "Progress %", min_value=0, max_value=100
                    ),
                    "Status": st.column_config.TextColumn("Status"),
                },
            )

            # Task selector
            task_ids = [t.get("task_id", "") for t in tasks]
            selected = st.selectbox(
                "Select a task to view live feed",
                options=[""] + task_ids,
                key="running_task_select",
            )
            if selected:
                st.session_state.selected_task_id = selected

        # Live action feed
        if st.session_state.selected_task_id:
            feed_data = api_get(
                f"/api/v1/tasks/{st.session_state.selected_task_id}/actions"
            )
            with detail_placeholder.container():
                st.subheader(f"Live Feed – `{st.session_state.selected_task_id}`")
                if feed_data:
                    actions = feed_data.get("actions", [])
                    for action in actions[-10:]:
                        status_icon = (
                            "✅" if action.get("status") == "success"
                            else "❌" if action.get("status") == "failed"
                            else "⏳"
                        )
                        st.markdown(
                            f"{status_icon} **{action.get('action_type', 'action')}** "
                            f"— {action.get('description', '')} "
                            f"*(confidence: {action.get('confidence', 'N/A')})*"
                        )
                else:
                    st.caption("No action feed available.")

    render_tasks()

    if auto_refresh:
        time.sleep(5)
        st.rerun()

# ===========================================================================
# TAB 3 – Task Details & Explainability
# ===========================================================================

with tab3:
    st.header("Task Details & Explainability")

    col_input, col_btn = st.columns([3, 1])
    with col_input:
        detail_task_id = st.text_input(
            "Task ID",
            value=st.session_state.selected_task_id,
            placeholder="e.g. task-abc-123",
            key="detail_task_id_input",
        )
    with col_btn:
        st.write("")  # vertical spacer
        load_task = st.button("🔍 Load Task", type="primary", use_container_width=True)

    if load_task and detail_task_id:
        with st.spinner("Loading task details…"):
            task_data = api_get(f"/api/v1/tasks/{detail_task_id}")
            explain_data = api_get(f"/api/v1/tasks/{detail_task_id}/explain")

        if task_data:
            st.divider()

            # Task summary
            c1, c2, c3 = st.columns(3)
            status = task_data.get("status", "unknown")
            status_color = (
                "green" if status == "completed"
                else "red" if status == "failed"
                else "orange"
            )
            c1.metric("Status", status.upper())
            c2.metric("Started", task_data.get("started_at", "N/A"))
            c3.metric("Completed", task_data.get("completed_at", "N/A"))

            st.markdown(f"**Description:** {task_data.get('task_description', '')}")

            st.divider()
            st.subheader("🔎 Step-by-Step Decision Log")

            steps: List[Dict] = []
            if explain_data:
                steps = explain_data.get("steps", [])
            elif task_data.get("steps"):
                steps = task_data["steps"]

            if steps:
                for idx, step in enumerate(steps, start=1):
                    step_status = step.get("status", "in_progress")
                    icon = (
                        "✅" if step_status == "success"
                        else "❌" if step_status == "failed"
                        else "🟡"
                    )
                    border_color = (
                        "#28a745" if step_status == "success"
                        else "#dc3545" if step_status == "failed"
                        else "#ffc107"
                    )
                    with st.expander(
                        f"{icon} Step {idx}: {step.get('action', 'Unknown Action')}",
                        expanded=(step_status != "success"),
                    ):
                        sc1, sc2 = st.columns(2)
                        with sc1:
                            st.markdown("**What the AI saw:**")
                            st.caption(step.get("observation", "N/A"))
                            st.markdown("**Decision:**")
                            st.caption(step.get("decision", "N/A"))
                        with sc2:
                            st.markdown("**Reasoning:**")
                            st.caption(step.get("reasoning", "N/A"))
                            conf = step.get("confidence", None)
                            if conf is not None:
                                st.metric("Confidence", f"{float(conf):.1%}")
            else:
                st.info("No decision log available for this task.")

            # Download report
            st.divider()
            report = {
                "task_id": detail_task_id,
                "task_info": task_data,
                "explainability": explain_data or {},
                "generated_at": datetime.utcnow().isoformat(),
            }
            st.download_button(
                label="⬇️ Download Explainability Report (JSON)",
                data=json.dumps(report, indent=2),
                file_name=f"screenmind_report_{detail_task_id}.json",
                mime="application/json",
            )

# ===========================================================================
# TAB 4 – System Metrics
# ===========================================================================

with tab4:
    st.header("System Metrics")

    if st.button("🔄 Refresh Metrics", key="refresh_metrics"):
        st.rerun()

    metrics_data = api_get("/api/v1/metrics")

    if metrics_data:
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric(
            "✅ Success Rate",
            f"{metrics_data.get('success_rate', 0):.1%}",
            delta=f"{metrics_data.get('success_rate_delta', 0):+.1%}",
        )
        m2.metric(
            "⏱ Avg Completion",
            f"{metrics_data.get('avg_completion_time_s', 0):.1f}s",
        )
        m3.metric(
            "⚡ LLM Latency",
            f"{metrics_data.get('llm_latency_ms', 0):.0f}ms",
        )
        m4.metric(
            "❌ Error Rate",
            f"{metrics_data.get('error_rate', 0):.1%}",
            delta=f"{metrics_data.get('error_rate_delta', 0):+.1%}",
            delta_color="inverse",
        )
        m5.metric(
            "🎬 Actions Executed",
            metrics_data.get("actions_executed", 0),
        )

        st.divider()

        # LLM latency trend chart
        latency_history = metrics_data.get("llm_latency_history", [])
        if latency_history:
            st.subheader("LLM Latency Trend (ms)")
            st.line_chart(latency_history)

        col_l, col_r = st.columns(2)
        with col_l:
            task_dist = metrics_data.get("task_status_distribution", {})
            if task_dist:
                st.subheader("Task Status Distribution")
                st.bar_chart(task_dist)
        with col_r:
            action_types = metrics_data.get("action_type_counts", {})
            if action_types:
                st.subheader("Action Types Breakdown")
                st.bar_chart(action_types)
    else:
        st.info("Connect to the API gateway to view live metrics.")

        # Placeholder demo layout
        st.divider()
        dc1, dc2, dc3, dc4, dc5 = st.columns(5)
        dc1.metric("✅ Success Rate", "—")
        dc2.metric("⏱ Avg Completion", "—")
        dc3.metric("⚡ LLM Latency", "—")
        dc4.metric("❌ Error Rate", "—")
        dc5.metric("🎬 Actions Executed", "—")

# ===========================================================================
# TAB 5 – Memory Browser
# ===========================================================================

with tab5:
    st.header("Memory Browser")
    st.markdown("Search and explore the agent's memory store.")

    col_q, col_type, col_btn2 = st.columns([3, 1, 1])
    with col_q:
        mem_query = st.text_input(
            "Search query",
            placeholder="e.g. login form, file download, error recovery",
            key="mem_query",
        )
    with col_type:
        mem_type = st.selectbox(
            "Memory type",
            options=["all", "short_term", "long_term", "failure"],
            key="mem_type",
        )
    with col_btn2:
        st.write("")
        search_mem = st.button("🔍 Search", type="primary", use_container_width=True)

    if search_mem and mem_query:
        with st.spinner("Searching memories…"):
            payload: Dict[str, Any] = {"query": mem_query, "top_k": 20}
            if mem_type != "all":
                payload["memory_type"] = mem_type
            mem_result = api_post("/api/v1/memory/search", payload)

        if mem_result:
            memories: List[Dict] = mem_result.get("memories", [])
            if memories:
                st.success(f"Found {len(memories)} memories")
                for mem in memories:
                    sim = mem.get("similarity", 0.0)
                    mem_type_label = mem.get("memory_type", "unknown")
                    type_icon = (
                        "⚡" if mem_type_label == "short_term"
                        else "📚" if mem_type_label == "long_term"
                        else "⚠️" if mem_type_label == "failure"
                        else "🧠"
                    )
                    with st.expander(
                        f"{type_icon} [{mem_type_label}] {mem.get('summary', 'Memory')} "
                        f"— similarity: {sim:.3f}",
                        expanded=False,
                    ):
                        mc1, mc2 = st.columns([2, 1])
                        with mc1:
                            st.markdown("**Content:**")
                            st.caption(mem.get("content", "N/A"))
                        with mc2:
                            st.metric("Similarity", f"{sim:.3f}")
                            st.caption(f"Created: {mem.get('created_at', 'N/A')}")
                            st.caption(f"Task ID: {mem.get('task_id', 'N/A')}")
            else:
                st.info("No memories found matching your query.")
        else:
            st.warning("Memory search unavailable.")

# ===========================================================================
# TAB 6 – Live Screen View
# ===========================================================================

with tab6:
    st.header("Live Screen View")
    st.markdown("View the current screenshot captured by the agent.")

    col_av, col_rb = st.columns([1, 1])
    with col_av:
        auto_screen = st.checkbox("Auto-refresh screen (3 s)", value=False)
    with col_rb:
        refresh_screen = st.button("📸 Capture Now", use_container_width=True)

    screen_placeholder = st.empty()
    state_placeholder = st.empty()

    def fetch_and_render_screen():
        screen_data = api_get("/api/v1/screen/current")
        if screen_data:
            b64 = screen_data.get("screenshot_base64", "")
            if b64:
                try:
                    img_bytes = base64.b64decode(b64)
                    img = Image.open(io.BytesIO(img_bytes))
                    with screen_placeholder.container():
                        st.image(img, caption="Current Screen Capture", use_container_width=True)
                except Exception:
                    screen_placeholder.error("Failed to decode screenshot.")
            else:
                screen_placeholder.info("No screenshot available.")

            summary = screen_data.get("state_summary", "")
            if summary:
                with state_placeholder.container():
                    st.subheader("Current State Summary")
                    st.info(summary)

            meta_cols = st.columns(3)
            meta_cols[0].metric("Resolution", screen_data.get("resolution", "N/A"))
            meta_cols[1].metric("Captured At", screen_data.get("captured_at", "N/A"))
            meta_cols[2].metric("Active Window", screen_data.get("active_window", "N/A"))
        else:
            screen_placeholder.warning("Could not fetch screen data from the agent.")

    if refresh_screen or auto_screen:
        fetch_and_render_screen()

    if not (refresh_screen or auto_screen):
        with screen_placeholder.container():
            st.info("Click **Capture Now** or enable **Auto-refresh** to view the live screen.")

    if auto_screen:
        time.sleep(3)
        st.rerun()
