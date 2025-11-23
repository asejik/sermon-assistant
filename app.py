import json
import datetime
from datetime import timedelta
import streamlit as st
import pandas as pd
from google.oauth2.service_account import Credentials
import gspread
from thefuzz import process, fuzz
import google.generativeai as genai

# --- Configuration ---
# 1. Setup Page Config First
st.set_page_config(page_title="Sermon Assistant", layout="wide")

# 2. Securely Load Keys (Cloud or Local)
# Gemini Key
if "gemini" in st.secrets:
    GEMINI_API_KEY = st.secrets["gemini"]["api_key"]
else:
    GEMINI_API_KEY = "" # Fallback to prevent crash, will show error later if needed

# Sheet ID
if "sheets" in st.secrets:
    SHEET_ID = st.secrets["sheets"]["sheet_id"]
else:
    SHEET_ID = "1q4-sO9g_lq9euOE-mN9rDysRjFbWJa_l8uaJ3nq2ffA"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

# --- UI Header ---
st.title("Citizens of Light Sermon Assistant")

with st.sidebar:
    st.header("About")
    st.markdown("This AI helps you find Citizens of Light sermons based on **Themes**, **Stories**, or **Scriptures**.")
    st.markdown("---")
    if st.button("Clear Chat History"):
        st.session_state.messages = []
        st.session_state.search_memory = {"last_query": "", "results": pd.DataFrame(), "current_index": 0}
        st.rerun()
    st.markdown("---")
    st.caption("Powered by Gemini 2.5 & Google Sheets")

# --- 1. Data Loading Function ---
@st.cache_data(ttl=600)
def load_data():
    try:
        # Check if we are in the cloud (using secrets)
        if "gcp_service_account" in st.secrets:
            service_account_info = st.secrets["gcp_service_account"]
            creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        else:
            # Load from File (Local Mode fallback)
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

# --- 2. The Brain (Gemini - Advanced) ---
def extract_search_terms(user_query):
    try:
        if not GEMINI_API_KEY:
            # Fallback: Just return the query as a keyword, no date filters
            return {"keywords": user_query, "start_date": None, "end_date": None, "limit": 10, "sort": "relevance"}

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.5-flash') # Using the requested 2.5 model

        # We must tell AI what today is so it can calculate "yesterday" or "last week"
        today_str = datetime.date.today().strftime("%Y-%m-%d")

        prompt = f"""
        System: You are a smart search parser for a church database. Today is {today_str}.

        Task: Analyze the user's query and extract:
        1. Keywords (Themes, Topics, Scripture, Preacher).
        2. Date Range (Start and End dates in YYYY-MM-DD format).
        3. Limit (How many results to show. Default to 10).
        4. Sort Order ("newest" if they ask for latest/recent, otherwise "relevance").

        Rules:
        - If user says "2025", start=2025-01-01, end=2025-12-31.
        - If user says "April 2022", start=2022-04-01, end=2022-04-30.
        - If user says "Yesterday", calculate the date based on today ({today_str}).
        - If user says "Latest message", set sort="newest" and limit=1.
        - Return ONLY raw JSON. No markdown formatting.

        User Query: "{user_query}"

        Output JSON format:
        {{
            "keywords": "string",
            "start_date": "YYYY-MM-DD" or null,
            "end_date": "YYYY-MM-DD" or null,
            "limit": integer,
            "sort": "newest" or "relevance"
        }}
        """
        response = model.generate_content(prompt)

        # Clean up response (sometimes AI adds ```json ... ```)
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_text)

    except Exception as e:
        # Fallback if AI fails
        print(f"AI Error: {e}")
        return {"keywords": user_query, "start_date": None, "end_date": None, "limit": 10, "sort": "relevance"}

# --- 3. Search Engine (Advanced) ---
def search_sermons(search_params, df):
    if df.empty: return pd.DataFrame()

    # 1. Apply Date Filter FIRST (if dates exist)
    # This makes the search faster and accurate
    if search_params.get("start_date"):
        try:
            start_dt = pd.to_datetime(search_params["start_date"])
            df = df[df['Date'] >= start_dt]
        except: pass # Ignore invalid dates

    if search_params.get("end_date"):
        try:
            end_dt = pd.to_datetime(search_params["end_date"])
            # Add one day to include the end date fully if needed, or just compare
            df = df[df['Date'] <= end_dt]
        except: pass

    # 2. Keyword Search
    # If there are keywords, we Fuzzy Match.
    # If keywords are empty (e.g. user just said "Messages from 2025"), we skip scoring and just return the dated rows.
    query_text = search_params.get("keywords", "")

    if query_text and query_text.lower() != "none":
        topics = query_text.replace(" and ", ",").split(",")
        topics = [t.strip() for t in topics if t.strip()]

        df = df.copy() # Avoid SettingWithCopy warnings
        df['match_score'] = 0
        df['matches_found'] = 0

        for index, row in df.iterrows():
            total_score = 0
            matches = 0
            title = str(row.get('Title', ''))
            preacher = str(row.get('Preacher', ''))
            row_text = f"{title} {preacher}".lower()

            for topic in topics:
                score = fuzz.partial_ratio(topic.lower(), row_text)
                if score > 75:
                    total_score += score
                    matches += 1

            df.at[index, 'match_score'] = total_score
            df.at[index, 'matches_found'] = matches

        # Filter: Keep matches
        results = df[df['matches_found'] > 0].copy()
    else:
        # No keywords provided, just return the date-filtered list
        results = df.copy()
        results['matches_found'] = 1 # Dummy value for sorting
        results['match_score'] = 0

    # 3. Sorting
    if not results.empty:
        sort_order = search_params.get("sort", "relevance")
        if sort_order == "newest":
            results = results.sort_values(by=['Date'], ascending=[False])
        else:
            # Default: Match Strength then Date
            results = results.sort_values(by=['matches_found', 'match_score', 'Date'], ascending=[False, False, False])

    return results

# --- 4. Main App Loop ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "search_memory" not in st.session_state:
    st.session_state.search_memory = {"last_query": "", "results": pd.DataFrame(), "current_index": 0}

df = load_data()

# 1. Display Chat History
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# 2. Check for "Next" Button Availability
# We check if we have results and if we haven't shown them all yet
mem_results = st.session_state.search_memory["results"]
mem_index = st.session_state.search_memory["current_index"]

# If there are results, and the current index is less than the total count...
if not mem_results.empty and mem_index < len(mem_results):
    remaining = len(mem_results) - mem_index
    # Create a centered button
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button(f"⬇️ Load Next 10 Results ({remaining} remaining)", type="primary", use_container_width=True):
            # --- HANDLE BUTTON CLICK ---
            # 1. Add a "user" message so the flow looks natural
            st.session_state.messages.append({"role": "user", "content": "Show more results"})

            # 2. Generate the batch
            batch = mem_results.iloc[mem_index : mem_index + 10]
            response_text = "Here are the next set of results:"

            for _, row in batch.iterrows():
                date_val = row.get('Date', pd.NaT)
                date_str = date_val.strftime('%Y-%m-%d') if pd.notnull(date_val) else "N/A"
                response_text += f"\n\n**Message Title:** {row.get('Title', '')}\n"
                response_text += f"- **Preacher:** {row.get('Preacher', '')}\n"
                response_text += f"- **Date:** {date_str}\n"
                response_text += f"- **Link:** [Download]({row.get('DownloadLink', '#')})"

            # 3. Update Memory
            st.session_state.search_memory["current_index"] += 10

            # 4. Append to history and Rerun
            st.session_state.messages.append({"role": "assistant", "content": response_text})
            st.rerun()

# 3. User Text Input
if prompt := st.chat_input("Search (e.g. 'The story of Jonah' or 'John 3:16')..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if df.empty:
            response_text = "⚠️ Database not connected. Check logs."
        else:
            # We don't need "Next" logic here anymore since the button handles it
            # But we keep it just in case they type it manually
            is_continuation = prompt.lower() in ["next", "more", "continue"]

            if is_continuation and not st.session_state.search_memory["results"].empty:
                # Manual typed "next" logic
                results = st.session_state.search_memory["results"]
                start_index = st.session_state.search_memory["current_index"]

                if start_index >= len(results):
                     response_text = "There are no more results for this search."
                     batch = pd.DataFrame() # Empty
                else:
                    response_text = "Here are more results:"
                    batch = results.iloc[start_index : start_index + 10]
                    st.session_state.search_memory["current_index"] += 10

            else:
                # NEW SEARCH
                with st.spinner("Analyzing request..."):
                    # 1. Get structured params from Brain
                    search_params = extract_search_terms(prompt)

                    # 2. Search with filters
                    results = search_sermons(search_params, df)

                # 3. Store in memory
                st.session_state.search_memory["results"] = results
                st.session_state.search_memory["current_index"] = 0

                # Debug Info (Optional - you can hide this later)
                if search_params.get("start_date") or search_params.get("limit") != 10:
                     st.caption(f"🤖 *Filter:* Date={search_params.get('start_date')} to {search_params.get('end_date')} | Limit={search_params.get('limit')}")

                if results.empty:
                    response_text = f"I couldn't find sermons matching that criteria."
                    batch = pd.DataFrame()
                else:
                    count = len(results)
                    response_text = f"Found {count} sermons. Here are the results:"

                    # 4. Apply the User's Limit (e.g., "I need 4 sermons")
                    user_limit = search_params.get("limit", 10)

                    # If user asked for specific number, use that. Otherwise default to 10 for batching.
                    batch_size = user_limit

                    batch = results.iloc[0:batch_size]

                    # Important: Update the index so the "Next" button works correctly
                    st.session_state.search_memory["current_index"] = batch_size

            # Process the batch (if any)
            if not batch.empty:
                # ... (Keep existing display loop code here) ...
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