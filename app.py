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

# --- 2. The Brain (Improved Prompt) ---
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
        1. Keywords: The core topic (e.g. "Faith", "Love").
           CRITICAL: If the user query is ONLY about a preacher (e.g. "Messages by Pastor Seun"), leave Keywords EMPTY.
           Do NOT include words like "Message", "Sermon", "Preach", "Series" in Keywords.
        2. Preacher: Name only. Remove titles (Pastor, Apostle).
        3. Date Range: YYYY-MM-DD.
        4. Limit: Integer (Default 10).
        5. Sort: "newest" or "relevance".

        User Query: "{user_query}"

        Output JSON format:
        {{
            "keywords": "string" or null,
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
        return {"keywords": user_query, "preacher": None, "start_date": None, "end_date": None, "limit": 10, "sort": "relevance"}

# --- HELPER: Flexible Name Matcher (Strict for Short Names) ---
def check_name_match(query_name, db_name):
    if not query_name or not db_name: return False

    # Strip titles
    titles = ["pastor", "apostle", "rev", "reverend", "prophet", "evangelist", "min", "minister", "dr", "mr", "mrs", "pst"]

    q_clean = query_name.lower()
    t_clean = str(db_name).lower()

    for title in titles:
        q_clean = q_clean.replace(f"{title} ", "").strip()
        t_clean = t_clean.replace(f"{title} ", "").strip()

    # --- STRICT LOGIC ---

    # 1. Exact Word Match (Primary Check)
    # If "Segun" is a distinct word in "Apostle Segun Obadje", return True immediately.
    # If "Segun" is NOT in "Pastor Seun Akinloye", this fails (which is good).
    t_words = t_clean.split()
    if q_clean in t_words:
        return True

    # 2. Conflict Resolver (Seun vs Segun)
    # If the names are very similar but short, prevent crossover.
    if len(q_clean) <= 5:
        # Require a near-perfect match (95%+) for short names
        # "Seun" vs "Segun" is ~80%, so this will return False (Correct!)
        return fuzz.ratio(q_clean, t_clean) >= 95

    # 3. Fuzzy Fallback for Longer Names
    # "Damilola" vs "Pastor Damiola Faleye" (Typo) -> Returns True
    return fuzz.partial_ratio(q_clean, t_clean) >= 75

# --- 3. The Search Engine (Stop-Words Added) ---
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

    # 2. Preacher Filter
    preacher_query = search_params.get("preacher")
    if preacher_query and preacher_query.lower() != "none":
        df = df[df['Preacher'].apply(lambda x: check_name_match(preacher_query, x))]

    # 3. Keyword Search
    query_text = search_params.get("keywords", "")

    # --- STOP WORD LOGIC ---
    # Even if AI sends "message", we ignore it here so we don't return 0 results
    stop_words = ["message", "messages", "sermon", "sermons", "preaching", "preached", "series", "audio", "mp3"]

    # Clean the query
    valid_topics = []
    if query_text and query_text.lower() != "none":
        raw_topics = query_text.replace(" and ", ",").split(",")
        for t in raw_topics:
            clean_t = t.strip().lower()
            if clean_t not in stop_words and clean_t != "":
                valid_topics.append(clean_t)

    # Check if we have any VALID keywords left
    if not valid_topics:
        # If no real keywords (e.g. user just said "Messages by Seun"), return all preacher matches
        results = df.copy()
        results['matches_found'] = 1
        results['match_score'] = 100
    else:
        # We have real keywords (e.g. "Faith"), so we filter
        df = df.copy()
        df['match_score'] = 0
        df['matches_found'] = 0

        for index, row in df.iterrows():
            total_score = 0
            matches = 0
            title = str(row.get('Title', ''))
            row_text = f"{title}".lower()

            for topic in valid_topics:
                score = fuzz.partial_ratio(topic, row_text)
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

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Button Logic
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

# Input Logic
if prompt := st.chat_input("Search..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if df.empty:
            response_text = "⚠️ Database not connected. Check logs."
        else:
            with st.spinner("Thinking & Searching..."):
                search_params = extract_search_terms(prompt)
                results = search_sermons(search_params, df)

            st.session_state.search_memory["results"] = results
            st.session_state.search_memory["current_index"] = 0

            # DEBUG: Show what AI found to help you verify
            debug_msg = []
            if search_params.get("preacher"): debug_msg.append(f"Preacher: {search_params['preacher']}")
            if search_params.get("keywords"): debug_msg.append(f"Keywords: {search_params['keywords']}")
            if debug_msg: st.caption(f"🤖 *Filter:* {' | '.join(debug_msg)}")

            if results.empty:
                response_text = f"I couldn't find sermons matching that request."
            else:
                count = len(results)
                user_limit = search_params.get("limit", 10)
                response_text = f"Found {count} sermons. Here are the results:"

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