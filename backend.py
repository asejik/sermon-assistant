# backend.py
import streamlit as st
import pandas as pd
from google.oauth2.service_account import Credentials
import gspread
from thefuzz import process, fuzz
import google.generativeai as genai
import json
import datetime

# --- CONFIGURATION & KEYS ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

if "gemini" in st.secrets:
    GEMINI_API_KEY = st.secrets["gemini"]["api_key"]
else:
    GEMINI_API_KEY = ""

if "sheets" in st.secrets:
    SHEET_ID = st.secrets["sheets"]["sheet_id"]
else:
    SHEET_ID = "1q4-sO9g_lq9euOE-mN9rDysRjFbWJa_l8uaJ3nq2ffA" # Fallback/Public ID

# --- 1. DATA LOADING ---
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

# --- 2. THE BRAIN (AI) ---
def extract_search_terms(user_query):
    try:
        if not GEMINI_API_KEY:
            # Fallback if AI key is missing
            return {"keywords": user_query, "synonyms": "", "preacher": None, "start_date": None, "end_date": None, "limit": 10, "sort": "relevance"}

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.5-flash')
        today_str = datetime.date.today().strftime("%Y-%m-%d")

        prompt = f"""
        System: You are a smart search parser for a church database. Today is {today_str}.

        Task: Analyze the user's query and output a JSON with search filters.

        Rules:
        1. **Preacher Aliases**: "Dami"->"Damilola", "Temi"->"Temitope", "Ibk"->"Ibukun".
        2. **Keywords**: Extract core topic. If "Generosity", Synonyms="Giving, Sacrifice".
        3. **Limits**: "Latest message" -> Limit=1, Sort="newest". Default Limit=10.

        User Query: "{user_query}"

        Output JSON format:
        {{
            "keywords": "string",
            "synonyms": "string",
            "preacher": "string" or null,
            "start_date": "YYYY-MM-DD" or null,
            "end_date": "YYYY-MM-DD" or null,
            "limit": integer,
            "sort": "newest" or "relevance"
        }}
        """
        response = model.generate_content(prompt)
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_text)
    except Exception as e:
        return {"keywords": user_query, "synonyms": "", "preacher": None, "start_date": None, "end_date": None, "limit": 10, "sort": "relevance"}

# --- 3. HELPER: Name Matcher ---
def check_name_match(query_name, db_name):
    if not query_name or not db_name: return False

    aliases = {
        "dami": "damilola",
        "temi": "temitope",
        "ibk": "ibukun",
        "pst": "pastor"
    }

    q_raw = query_name.lower().strip()
    t_clean = str(db_name).lower()

    titles = ["pastor", "apostle", "rev", "reverend", "prophet", "evangelist", "min", "minister", "dr", "mr", "mrs", "pst"]
    for title in titles:
        t_clean = t_clean.replace(f"{title} ", "").strip()

    # Priority 1: High confidence partial match (Handles "Ibk" finding "Pastor Ibk")
    if fuzz.partial_ratio(q_raw, t_clean) >= 95: return True

    # Priority 2: Alias expansion
    if q_raw in aliases:
        q_expanded = aliases[q_raw]
        if fuzz.partial_ratio(q_expanded, t_clean) >= 80: return True

    # Priority 3: Word check & Short name strictness
    t_words = t_clean.split()
    if q_raw in t_words: return True

    if len(q_raw) <= 5:
        return fuzz.ratio(q_raw, t_clean) >= 95
    else:
        return fuzz.partial_ratio(q_raw, t_clean) >= 75

# --- 4. THE SEARCH ENGINE ---
def search_sermons(search_params, df):
    if df.empty: return pd.DataFrame()

    # 1. Filters
    filtered_df = df.copy()
    if search_params.get("start_date"):
        try:
            filtered_df = filtered_df[filtered_df['Date'] >= pd.to_datetime(search_params["start_date"])]
        except: pass
    if search_params.get("end_date"):
        try:
            filtered_df = filtered_df[filtered_df['Date'] <= pd.to_datetime(search_params["end_date"])]
        except: pass

    preacher_query = search_params.get("preacher")
    if preacher_query and preacher_query.lower() != "none":
        filtered_df = filtered_df[filtered_df['Preacher'].apply(lambda x: check_name_match(preacher_query, x))]

    # 2. Keyword Search
    primary_keywords = search_params.get("keywords", "")
    secondary_keywords = search_params.get("synonyms", "")
    stop_words = ["message", "messages", "sermon", "sermons", "preaching", "preached", "series", "audio", "mp3", "living", "walking"]

    def score_rows(dataframe, keywords, match_type_label):
        if not keywords or keywords.lower() == "none": return pd.DataFrame()
        topic_list = [t.strip().lower() for t in keywords.replace(" and ", ",").split(",") if t.strip().lower() not in stop_words]
        if not topic_list: return pd.DataFrame()

        temp_df = dataframe.copy()
        temp_df['match_score'] = 0
        for index, row in temp_df.iterrows():
            title = str(row.get('Title', '')).lower()
            total_score = 0
            for topic in topic_list:
                score = fuzz.partial_ratio(topic, title)
                if score > 80: total_score += score
            temp_df.at[index, 'match_score'] = total_score

        matched = temp_df[temp_df['match_score'] > 0].copy()
        matched['match_type'] = match_type_label
        return matched

    results_exact = score_rows(filtered_df, primary_keywords, "Exact")
    results_suggested = pd.DataFrame()

    if len(results_exact) < 10 and secondary_keywords:
        results_suggested = score_rows(filtered_df, secondary_keywords, "Suggested")
        if not results_exact.empty:
            results_suggested = results_suggested[~results_suggested.index.isin(results_exact.index)]

    if results_exact.empty and results_suggested.empty and (not primary_keywords or primary_keywords.lower() == "none"):
        final_results = filtered_df.copy()
        final_results['match_type'] = "Exact"
        final_results['match_score'] = 100
    else:
        final_results = pd.concat([results_exact, results_suggested])

    if not final_results.empty:
        sort_order = search_params.get("sort", "relevance")
        if sort_order == "newest":
            final_results = final_results.sort_values(by=['Date'], ascending=[False])
        else:
            type_map = {"Exact": 1, "Suggested": 2}
            final_results['type_rank'] = final_results['match_type'].map(type_map)
            final_results = final_results.sort_values(by=['type_rank', 'match_score', 'Date'], ascending=[True, False, False])

    return final_results