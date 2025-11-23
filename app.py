# app.py
import streamlit as st
import pandas as pd
import backend # Import our new logic file

# --- Page Config ---
st.set_page_config(page_title="Sermon Assistant", layout="wide", page_icon="🎧")

# --- Load Custom CSS ---
def local_css(file_name):
    with open(file_name) as f:
        st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)

local_css("style.css") # Load styling

# --- UI Header & Sidebar ---
st.title("Citizens of Light Sermon Assistant")

with st.sidebar:
    st.header("About")
    st.markdown("This AI helps you find Citizens of Light sermons based on **Themes**, **Stories**, or **Preachers**.")
    st.markdown("---")
    if st.button("Clear Chat History", type="secondary"):
        st.session_state.messages = []
        st.session_state.search_memory = {"last_query": "", "results": pd.DataFrame(), "current_index": 0}
        st.rerun()
    st.markdown("---")
    st.caption("Powered by Gemini 2.5 & Google Sheets")

# --- Initialize Session State ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "search_memory" not in st.session_state:
    st.session_state.search_memory = {"last_query": "", "results": pd.DataFrame(), "current_index": 0}

# --- Load Data (From Backend) ---
df = backend.load_data()

# --- Display Chat History ---
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"], unsafe_allow_html=True)

# --- Button Logic (Load More) ---
mem_results = st.session_state.search_memory["results"]
mem_index = st.session_state.search_memory["current_index"]

if isinstance(mem_results, pd.DataFrame) and not mem_results.empty and isinstance(mem_index, int):
    if mem_index < len(mem_results):
        remaining = len(mem_results) - mem_index
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button(f"⬇️ Load Next 10 Results ({remaining} remaining)", type="primary", use_container_width=True):
                st.session_state.messages.append({"role": "user", "content": "Show more results"})
                batch = mem_results.iloc[mem_index : mem_index + 10]

                # Build HTML for next batch
                response_html = "<br>"
                for _, row in batch.iterrows():
                    date_val = row.get('Date', pd.NaT)
                    date_str = date_val.strftime('%Y-%m-%d') if pd.notnull(date_val) else "N/A"

                    response_html += f"""
                    <div class="sermon-card">
                        <div class="sermon-title">{row.get('Title', '')}</div>
                        <div class="sermon-details">
                            <span>👤 {row.get('Preacher', '')}</span>
                            <span>📅 {date_str}</span>
                        </div>
                        <a href="{row.get('DownloadLink', '#')}" target="_blank" class="download-link">
                            🔗 Download Sermon
                        </a>
                    </div>
                    """
                st.session_state.search_memory["current_index"] += 10
                st.session_state.messages.append({"role": "assistant", "content": response_html})
                st.rerun()

# --- Chat Input Logic ---
if prompt := st.chat_input("Search sermons by topic, preacher, scripture, or date..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if df.empty:
            response_html = "⚠️ Database not connected. Check logs."
        else:
            with st.spinner("Searching for requested sermon, please wait..."):
                # Call Backend Functions
                search_params = backend.extract_search_terms(prompt)
                results = backend.search_sermons(search_params, df)

            # Limit Suggestions
            if len(results) > 20 and "Suggested" in results['match_type'].values:
                exacts = results[results['match_type'] == "Exact"]
                suggested = results[results['match_type'] == "Suggested"].head(20 - len(exacts))
                results = pd.concat([exacts, suggested])

            st.session_state.search_memory["results"] = results

            # AI Caption
            debug_msg = []
            if search_params.get("preacher"): debug_msg.append(f"Preacher: {search_params['preacher']}")
            if search_params.get("keywords"): debug_msg.append(f"Keywords: {search_params['keywords']}")
            if search_params.get("synonyms"): debug_msg.append(f"Related: {search_params['synonyms']}")

            response_html = ""
            if debug_msg:
                response_html += f"""<div class="ai-caption">🤖 <b>AI Detected Themes:</b> {' | '.join(debug_msg)}</div>"""

            if results.empty:
                response_html += f"<p>I couldn't find any exact matches for '<b>{prompt}</b>', and no related topics were found.</p>"
            else:
                count = len(results)
                user_limit = search_params.get("limit", 10)
                exact_count = len(results[results['match_type'] == "Exact"])
                suggested_count = len(results[results['match_type'] == "Suggested"])

                if exact_count == 0:
                    response_html += f"<p>I did not find any sermon with an exact match, here are <b>{suggested_count}</b> related/suggested results:</p>"
                else:
                    response_html += f"<p>Found <b>{count}</b> sermons. Here are the results:</p>"

                # Display Batch
                batch_size = user_limit
                batch = results.iloc[0:batch_size]
                st.session_state.search_memory["current_index"] = batch_size

                current_section = ""

                for _, row in batch.iterrows():
                    match_type = row.get('match_type', 'Exact')

                    if match_type != current_section:
                        current_section = match_type
                        if match_type == "Exact":
                            header_text = "Exact Match" if exact_count == 1 else "Exact Matches"
                            response_html += f"""<div class="chat-header">✅ {header_text}</div>"""
                        elif match_type == "Suggested":
                            response_html += f"""<div class="chat-header">💡 Related / Suggested Results</div>"""

                    date_val = row.get('Date', pd.NaT)
                    date_str = date_val.strftime('%Y-%m-%d') if pd.notnull(date_val) else "N/A"

                    response_html += f"""
                    <div class="sermon-card">
                        <div class="sermon-title">{row.get('Title', '')}</div>
                        <div class="sermon-details">
                            <span>👤 {row.get('Preacher', '')}</span>
                            <span>📅 {date_str}</span>
                        </div>
                        <a href="{row.get('DownloadLink', '#')}" target="_blank" class="download-link">
                            🔗 Download Sermon
                        </a>
                    </div>
                    """

        st.markdown(response_html, unsafe_allow_html=True)
        st.session_state.messages.append({"role": "assistant", "content": response_html})
        st.rerun()