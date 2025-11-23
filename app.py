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

# --- 2. The Brain (Gemini) ---
def extract_search_terms(user_query):
    try:
        if not GEMINI_API_KEY:
            return user_query # Fallback if no key

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        # Note: Using 1.5-flash as it is the most stable current release for this library

        prompt = f"""
        You are a theological assistant.
        Convert this user query into 3-5 comma-separated keywords (Themes, Topics, Bible Characters).
        User Query: "{user_query}"
        Keywords:
        """
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return user_query

# --- 3. Search Engine ---
def search_sermons(query, expanded_keywords, df):
    if df.empty: return pd.DataFrame(), []

    full_search_text = f"{query}, {expanded_keywords}"
    topics = full_search_text.replace(" and ", ",").split(",")
    topics = [t.strip() for t in topics if t.strip()]

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

    results = df[df['matches_found'] > 0].copy()

    if not results.empty:
        results = results.sort_values(by=['matches_found', 'match_score', 'Date'], ascending=[False, False, False])

    return results, topics

# --- 4. Main App Loop ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "search_memory" not in st.session_state:
    st.session_state.search_memory = {"last_query": "", "results": pd.DataFrame(), "current_index": 0}

df = load_data()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Search (e.g. 'The story of Jonah' or 'John 3:16')..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if df.empty:
            response_text = "⚠️ Database not connected. Check logs."
        else:
            is_continuation = prompt.lower() in ["next", "more", "continue"]

            if is_continuation and not st.session_state.search_memory["results"].empty:
                results = st.session_state.search_memory["results"]
                start_index = st.session_state.search_memory["current_index"]
                response_text = "Here are more results:"
            else:
                with st.spinner("Thinking & Searching..."):
                    ai_keywords = extract_search_terms(prompt)
                    results, _ = search_sermons(prompt, ai_keywords, df)

                st.session_state.search_memory["results"] = results
                st.session_state.search_memory["current_index"] = 0
                start_index = 0

                if expanded_msg := ai_keywords if ai_keywords != prompt else None:
                     st.caption(f"🤖 *AI Themes:* {expanded_msg}")

                if results.empty:
                    response_text = f"I couldn't find sermons for '{prompt}'. Try simpler keywords."
                else:
                    response_text = f"Found {len(results)} sermons. Top results:"

            batch = results.iloc[start_index : start_index + 10]

            if not batch.empty:
                for _, row in batch.iterrows():
                    date_val = row.get('Date', pd.NaT)
                    date_str = date_val.strftime('%Y-%m-%d') if pd.notnull(date_val) else "N/A"
                    response_text += f"\n\n**Message Title:** {row.get('Title', '')}\n"
                    response_text += f"- **Preacher:** {row.get('Preacher', '')}\n"
                    response_text += f"- **Date:** {date_str}\n"
                    response_text += f"- **Link:** [Download]({row.get('DownloadLink', '#')})"

                st.session_state.search_memory["current_index"] += 10
            elif is_continuation:
                 response_text = "No more results."

        st.markdown(response_text)
        st.session_state.messages.append({"role": "assistant", "content": response_text})