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

# --- 2. The Brain ---
def extract_search_terms(user_query):
    try:
        if not GEMINI_API_KEY:
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

# --- HELPER: Name Matcher ---
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

    if fuzz.partial_ratio(q_raw, t_clean) >= 95: return True
    if q_raw in aliases:
        q_expanded = aliases[q_raw]
        if fuzz.partial_ratio(q_expanded, t_clean) >= 80: return True

    t_words = t_clean.split()
    if q_raw in t_words: return True

    if len(q_raw) <= 5:
        return fuzz.ratio(q_raw, t_clean) >= 95
    else:
        return fuzz.partial_ratio(q_raw, t_clean) >= 75

# --- 3. The Search Engine ---
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
                    response_text += "\n\n---"
                    response_text += f"\n\n**{row.get('Title', '')}**\n"
                    response_text += f"- 👤 {row.get('Preacher', '')}\n"
                    response_text += f"- 📅 {date_str} | 🔗 [Download]({row.get('DownloadLink', '#')})"
                st.session_state.search_memory["current_index"] += 10
                st.session_state.messages.append({"role": "assistant", "content": response_text})
                st.rerun()

# Input Logic
# UPDATED: New Placeholder Text
if prompt := st.chat_input("Search sermons by topic, preacher, scripture, or date..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if df.empty:
            response_text = "⚠️ Database not connected. Check logs."
        else:
            # UPDATED: New Loading Text
            with st.spinner("Searching for requested sermon, please wait..."):
                search_params = extract_search_terms(prompt)
                results = search_sermons(search_params, df)

            # Limit Suggestions
            if len(results) > 20 and "Suggested" in results['match_type'].values:
                exacts = results[results['match_type'] == "Exact"]
                suggested = results[results['match_type'] == "Suggested"].head(20 - len(exacts))
                results = pd.concat([exacts, suggested])

            st.session_state.search_memory["results"] = results

            # UPDATED: Restored AI Themes Display
            debug_msg = []
            if search_params.get("preacher"): debug_msg.append(f"Preacher: {search_params['preacher']}")
            if search_params.get("keywords"): debug_msg.append(f"Keywords: {search_params['keywords']}")
            if search_params.get("synonyms"): debug_msg.append(f"Related: {search_params['synonyms']}")

            # Label changed back to "AI Detected Themes"
            if debug_msg: st.caption(f"🤖 *AI Detected Themes:* {' | '.join(debug_msg)}")

            if results.empty:
                response_text = f"I couldn't find any exact matches for '{prompt}', and no related topics were found."
            else:
                count = len(results)
                user_limit = search_params.get("limit", 10)

                # Check Counts
                exact_count = len(results[results['match_type'] == "Exact"])
                suggested_count = len(results[results['match_type'] == "Suggested"])

                if exact_count == 0:
                    response_text = f"I did not find any sermon with an exact match, here are {suggested_count} related/suggested results:"
                else:
                    response_text = f"Found {count} sermons. Here are the results:"

                # Display Logic
                batch_size = user_limit
                batch = results.iloc[0:batch_size]
                st.session_state.search_memory["current_index"] = batch_size

                current_section = ""

                for _, row in batch.iterrows():
                    match_type = row.get('match_type', 'Exact')

                    # UPDATED: Smaller Headers & Singular/Plural Logic
                    if match_type != current_section:
                        current_section = match_type
                        if match_type == "Exact":
                            # Singular vs Plural check
                            header_text = "Exact Match" if exact_count == 1 else "Exact Matches"
                            response_text += f"\n\n#### ✅ {header_text}"
                        elif match_type == "Suggested":
                            response_text += "\n\n#### 💡 Related / Suggested Results"

                    date_val = row.get('Date', pd.NaT)
                    date_str = date_val.strftime('%Y-%m-%d') if pd.notnull(date_val) else "N/A"

                    # UPDATED: Added Divider
                    response_text += "\n\n---"
                    response_text += f"\n\n**{row.get('Title', '')}**\n"
                    response_text += f"- 👤 {row.get('Preacher', '')}\n"
                    response_text += f"- 📅 {date_str} | 🔗 [Download]({row.get('DownloadLink', '#')})"

        st.markdown(response_text)
        st.session_state.messages.append({"role": "assistant", "content": response_text})
        st.rerun()