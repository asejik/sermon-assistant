import streamlit as st
import pandas as pd
from google.oauth2.service_account import Credentials
import gspread
from thefuzz import process, fuzz
import google.generativeai as genai
import json
import datetime
from datetime import timedelta

# --- Configuration ---
st.set_page_config(page_title="Sermon Assistant", layout="wide")

# Securely Load Keys
if "gemini" in st.secrets:
    GEMINI_API_KEY = st.secrets["gemini"]["api_key"]
else:
    GEMINI_API_KEY = ""

if "sheets" in st.secrets:
    SHEET_ID = st.secrets["sheets"]["sheet_id"]
else:
    SHEET_ID = "1q4-sO9g_lq9euOE-mN9rDysRjFbWJa_l8uaJ3nq2ffA"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

# --- UI Header ---
st.title("Citizens of Light Sermon Assistant")
with st.sidebar:
    st.header("About")
    st.markdown("This AI helps you find Citizens of Light sermons based on **Themes**, **Stories**, or **Preachers**.")
    st.markdown("---")
    if st.button("Clear Chat History"):
        st.session_state.messages = []
        st.session_state.search_memory = {"last_query": "", "results": pd.DataFrame(), "current_index": 0}
        st.rerun()
    st.markdown("---")
    st.caption("Powered by Gemini 2.5 & Google Sheets")

# --- 1. Data Loading ---
@st.cache_data(ttl=600)
def load_data():
    try:
        if "gcp_service_account" in st.secrets:
            service_account_info = st.secrets["gcp_service_account"]
            creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)

        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        data = sheet.get_all_records()

        if not data: return pd.DataFrame()
        df = pd.DataFrame(data)
        df.columns = df.columns.str.strip()

        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
        else:
            df['Date'] = pd.NaT
        return df
    except Exception as e:
        st.error(f"⚠️ Connection Error: {e}")
        return pd.DataFrame()

# --- 2. The Brain (Gemini 2.5) ---
def extract_search_terms(user_query):
    try:
        if not GEMINI_API_KEY:
            return {"keywords": user_query, "preacher": None, "start_date": None, "end_date": None, "limit": 10, "sort": "relevance"}

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.5-flash')

        today_str = datetime.date.today().strftime("%Y-%m-%d")

        prompt = f"""
        System: You are a smart search parser for a church database. Today is {today_str}.

        Task: Analyze the user's query and extract:
        1. Keywords (The topic/theme. Remove Preacher names from this).
        2. Preacher (Name only. Ignore titles like Pastor, Apostle).
        3. Date Range (YYYY-MM-DD).
        4. Limit (Default 10).
        5. Sort ("newest" or "relevance").

        User Query: "{user_query}"

        Output JSON format:
        {{
            "keywords": "string",
            "preacher": "string" or null,
            "start_date": "string" or null,
            "end_date": "string" or null,
            "limit": integer,
            "sort": "string"
        }}
        """
        response = model.generate_content(prompt)
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_text)
    except Exception as e:
        # Fallback
        return {"keywords": user_query, "preacher": None, "start_date": None, "end_date": None, "limit": 10, "sort": "relevance"}

# --- HELPER: Preacher Name Normalizer ---
def check_name_match(query_name, db_name):
    """
    Smart Logic:
    1. Removes titles (Pastor, Apostle, etc).
    2. If query is 1 word ("Seun"), use loose matching (allows "Seun Akinloye").
    3. If query is 2+ words ("Damilola Faleye"), use strict matching (blocks "Ibukun Faleye").
    """
    if not query_name or not db_name: return False

    # List of titles to strip out
    titles = ["pastor", "apostle", "rev", "reverend", "prophet", "evangelist", "min", "minister", "dr", "mr", "mrs", "pst"]

    q_clean = query_name.lower()
    t_clean = str(db_name).lower()

    # Remove titles
    for title in titles:
        q_clean = q_clean.replace(f"{title} ", "").strip()
        t_clean = t_clean.replace(f"{title} ", "").strip()

    # Match Logic
    if " " not in q_clean:
        # One word query (e.g. "Seun") -> Use Partial Ratio (High Threshold)
        # 85 prevents "Seun" matching "Segun" but allows "Seun" in "Seun Akinloye"
        return fuzz.partial_ratio(q_clean, t_clean) >= 85
    else:
        # Multi word query (e.g. "Damilola Faleye") -> Use Token Sort (Very High Threshold)
        # This prevents "Damilola Faleye" matching "Ibukun Faleye" just because they share "Faleye"
        return fuzz.token_sort_ratio(q_clean, t_clean) >= 70

# --- 3. The Search Engine ---
def search_sermons(search_params, df):
    if df.empty: return pd.DataFrame()

    # 1. Date Filter
    if search_params.get("start_date"):
        try:
            start_dt = pd.to_datetime(search_params["start_date"])
            df = df[df['Date'] >= start_dt]
        except: pass

    if search_params.get("end_date"):
        try:
            end_dt = pd.to_datetime(search_params["end_date"])
            df = df[df['Date'] <= end_dt]
        except: pass

    # 2. Smart Preacher Filter
    preacher_query = search_params.get("preacher")
    if preacher_query and preacher_query.lower() != "none":
        # Apply the custom check_name_match function
        df = df[df['Preacher'].apply(lambda x: check_name_match(preacher_query, x))]

    # 3. Keyword Search
    query_text = search_params.get("keywords", "")

    # If no keywords (only Preacher/Date), return all matches
    if not query_text or query_text.lower() == "none" or query_text == "":
        results = df.copy()
        results['matches_found'] = 1
        results['match_score'] = 100
    else:
        topics = query_text.replace(" and ", ",").split(",")
        topics = [t.strip() for t in topics if t.strip()]

        df = df.copy()
        df['match_score'] = 0
        df['matches_found'] = 0

        for index, row in df.iterrows():
            total_score = 0
            matches = 0
            title = str(row.get('Title', ''))
            row_text = f"{title}".lower()

            for topic in topics:
                score = fuzz.partial_ratio(topic.lower(), row_text)
                if score > 75:
                    total_score += score
                    matches += 1

            df.at[index, 'match_score'] = total_score
            df.at[index, 'matches_found'] = matches

        results = df[df['matches_found'] > 0].copy()

    # 4. Sorting
    if not results.empty:
        sort_order = search_params.get("sort", "relevance")
        if sort_order == "newest":
            results = results.sort_values(by=['Date'], ascending=[False])
        else:
            results = results.sort_values(by=['matches_found', 'match_score', 'Date'], ascending=[False, False, False])

    return results

# --- 4. Main App Loop ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "search_memory" not in st.session_state:
    st.session_state.search_memory = {"last_query": "", "results": pd.DataFrame(), "current_index": 0}

df = load_data()

# Display History
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# --- SMART BUTTON LOGIC ---
mem_results = st.session_state.search_memory["results"]
mem_index = st.session_state.search_memory["current_index"]

# FIX: Added strict type checks to prevent TypeError
if isinstance(mem_results, pd.DataFrame) and not mem_results.empty and isinstance(mem_index, int):
    if mem_index < len(mem_results):
        remaining = len(mem_results) - mem_index
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button(f"⬇️ Load Next 10 Results ({remaining} remaining)", type="primary", use_container_width=True):
                st.session_state.messages.append({"role": "user", "content": "Show more results"})

                # Fetch Batch
                batch = mem_results.iloc[mem_index : mem_index + 10]
                response_text = "Here are the next set of results:"

                for _, row in batch.iterrows():
                    date_val = row.get('Date', pd.NaT)
                    date_str = date_val.strftime('%Y-%m-%d') if pd.notnull(date_val) else "N/A"
                    response_text += f"\n\n**Message Title:** {row.get('Title', '')}\n"
                    response_text += f"- **Preacher:** {row.get('Preacher', '')}\n"
                    response_text += f"- **Date:** {date_str}\n"
                    response_text += f"- **Link:** [Download]({row.get('DownloadLink', '#')})"

                st.session_state.search_memory["current_index"] += 10
                st.session_state.messages.append({"role": "assistant", "content": response_text})
                st.rerun()

# --- INPUT LOGIC ---
if prompt := st.chat_input("Search (e.g. 'Messages by Pastor Seun on Faith')..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if df.empty:
            response_text = "⚠️ Database not connected. Check logs."
        else:
            # AI SEARCH
            with st.spinner("Thinking & Searching..."):
                search_params = extract_search_terms(prompt)
                results = search_sermons(search_params, df)

            st.session_state.search_memory["results"] = results
            st.session_state.search_memory["current_index"] = 0

            # Debug (Shows what filters were applied)
            debug_msg = []
            if search_params.get("preacher"): debug_msg.append(f"Preacher: {search_params['preacher']}")
            if search_params.get("limit") != 10: debug_msg.append(f"Limit: {search_params['limit']}")
            if debug_msg: st.caption(f"🤖 *Filter:* {' | '.join(debug_msg)}")

            if results.empty:
                response_text = f"I couldn't find sermons matching that request."
            else:
                count = len(results)
                user_limit = search_params.get("limit", 10)

                response_text = f"Found {count} sermons. Here are the results:"

                # Set initial batch
                batch_size = user_limit
                batch = results.iloc[0:batch_size]
                st.session_state.search_memory["current_index"] = batch_size

                for _, row in batch.iterrows():
                    date_val = row.get('Date', pd.NaT)
                    date_str = date_val.strftime('%Y-%m-%d') if pd.notnull(date_val) else "N/A"
                    response_text += f"\n\n**Message Title:** {row.get('Title', '')}\n"
                    response_text += f"- **Preacher:** {row.get('Preacher', '')}\n"
                    response_text += f"- **Date:** {date_str}\n"
                    response_text += f"- **Link:** [Download]({row.get('DownloadLink', '#')})"

        st.markdown(response_text)
        st.session_state.messages.append({"role": "assistant", "content": response_text})
        st.rerun()