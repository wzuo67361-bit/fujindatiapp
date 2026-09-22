import streamlit as st
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
import re
import json
import html
import threading
import random
import pandas as pd
from pathlib import Path
from datetime import datetime

st.set_page_config(
    page_title="海口边检辅警刷题系统",
    layout="wide",
    initial_sidebar_state="collapsed",
)

if "font_size" not in st.session_state:
    st.session_state.font_size = 20
if "current_qid" not in st.session_state:
    st.session_state.current_qid = None
if "answered" not in st.session_state:
    st.session_state.answered = False
if "last_correct" not in st.session_state:
    st.session_state.last_correct = None
if "last_user_answer" not in st.session_state:
    st.session_state.last_user_answer = ""
if "all_done_choice" not in st.session_state:
    st.session_state.all_done_choice = None
if "case_idx" not in st.session_state:
    st.session_state.case_idx = 0
if "show_case_answer" not in st.session_state:
    st.session_state.show_case_answer = False


# ============ 数据库连接池 ============
@st.cache_resource
def get_pool():
    return pool.SimpleConnectionPool(1, 5, st.secrets["DATABASE_URL"])


@contextmanager
def db_cursor():
    p = get_pool()
    conn = p.getconn()
    try:
        yield conn, conn.cursor()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


# ============ 工具函数 ============
def normalize_stem_for_dedup(stem):
    if not stem:
        return ""
    s = str(stem).strip()
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[，。、；：？！,\.;:\?!"\'""''（）()【】\[\]《》<>—\-_]', '', s)
    return s.lower()


def clean_stem(text):
    if not text:
        return text
    patterns = [
        r'[（(]\s*[本共每]?题?\s*\d+(\.\d+)?\s*分\s*[)）]',
        r'[\[【]\s*[本共每]?题?\s*\d+(\.\d+)?\s*分\s*[\]】]',
        r'[（(]\s*第\s*\d+\s*题\s*[，,]?\s*\d+(\.\d+)?\s*分\s*[)）]',
        r'[（(]\s*[本共每]?题?\s*\d+(\.\d+)?\s*分\s*[，,].*?[)）]',
    ]
    for p in patterns:
        text = re.sub(p, '', text)
    return text.strip()


def to_halfwidth_letter(ch):
    if not ch:
        return ''
    ch = ch.upper()
    if 'Ａ' <= ch <= 'Ｅ':
        return chr(ord(ch) - ord('Ａ') + ord('A'))
    if 'A' <= ch <= 'E':
        return ch
    return ''


def _extract_with_pattern(content, pattern, allow_ocr_shift=False):
    raw_matches = list(pattern.finditer(content))
    if not raw_matches:
        return content, []
    candidates = []
    last_letter_ord = ord('A') - 1
    for m in raw_matches:
        groups = m.groups()
        letter = ''
        if allow_ocr_shift and len(groups) >= 3 and groups[2] is not None:
            o = last_letter_ord + 1
            if o > ord('E'):
                continue
            letter = chr(o)
        else:
            for g in groups:
                if g:
                    letter = to_halfwidth_letter(g)
                    break
            if not letter:
                continue
        candidates.append((m, letter))
        last_letter_ord = ord(letter)
    if len(candidates) < 2:
        return content, []
    best_seq = []
    for start_idx in range(len(candidates)):
        seq = [candidates[start_idx]]
        expected = chr(ord(candidates[start_idx][1]) + 1)
        used = {candidates[start_idx][1]}
        for j in range(start_idx + 1, len(candidates)):
            c_letter = candidates[j][1]
            if c_letter == expected and c_letter not in used:
                seq.append(candidates[j])
                used.add(c_letter)
                expected = chr(ord(expected) + 1)
                if expected > 'E':
                    break
        if len(seq) > len(best_seq):
            best_seq = seq
    if len(best_seq) < 2:
        return content, []
    stem = content[:best_seq[0][0].start()].strip()
    options = []
    for i, (m, letter) in enumerate(best_seq):
        start = m.end()
        end = best_seq[i + 1][0].start() if i + 1 < len(best_seq) else len(content)
        opt = content[start:end].strip()
        opt = re.sub(r'^[\.、．)）:：。，,\s]+', '', opt)
        options.append(opt)
    return stem, options


def parse_options_from_content(content):
    if not content:
        return content, []
    pattern1 = re.compile(
        r'(?:'
        r'[（(\[【]\s*([A-Ea-eＡ-Ｅａ-ｅ])\s*[）)\]】]'
        r'|'
        r'([A-Ea-eＡ-Ｅａ-ｅ])\s*[\.、．)）:：。，,\]]'
        r'|'
        r'[（(]\s*[\.、．]'
        r')'
    )
    result = _extract_with_pattern(content, pattern1, allow_ocr_shift=True)
    if result[1]:
        return result
    pattern2 = re.compile(r'(?:^|[\s。，,;；:：）)\]】])([A-EＡ-Ｅ])\s+')
    result = _extract_with_pattern(content, pattern2, allow_ocr_shift=False)
    if result[1]:
        return result
    pattern3 = re.compile(r'(?:^|[\s。，,;；:：）)\]】])([A-EＡ-Ｅ])(?=[\u4e00-\u9fa5])')
    result = _extract_with_pattern(content, pattern3, allow_ocr_shift=False)
    if result[1]:
        return result
    return content, []


# ============ 建表（只跑一次） ============
@st.cache_resource
def init_db_once():
    with db_cursor() as (conn, c):
        c.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id BIGSERIAL PRIMARY KEY, qtype TEXT, stem TEXT, options TEXT,
                answer TEXT, explanation TEXT, batch TEXT DEFAULT '', tag TEXT DEFAULT '',
                stem_key TEXT DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id BIGSERIAL PRIMARY KEY, question_id BIGINT, user_answer TEXT,
                is_correct INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS progress (
                question_id BIGINT PRIMARY KEY, times_seen INTEGER DEFAULT 0,
                times_correct INTEGER DEFAULT 0, consecutive_correct INTEGER DEFAULT 0,
                in_wrong_book INTEGER DEFAULT 0
            )
        """)
        c.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS batch TEXT DEFAULT ''")
        c.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS stem_key TEXT DEFAULT ''")
        c.execute("SELECT id, stem FROM questions WHERE stem_key = '' OR stem_key IS NULL")
        old_rows = c.fetchall()
        for qid, stem in old_rows:
            c.execute("UPDATE questions SET stem_key=%s WHERE id=%s", (normalize_stem_for_dedup(stem), qid))
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_stem_key ON questions(stem_key) WHERE stem_key != ''")
        c.execute("CREATE INDEX IF NOT EXISTS idx_records_qid ON records(question_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_progress_wrong ON progress(in_wrong_book)")
    return True


init_db_once()


# ============ 缓存：一次性加载题库和进度到内存 ============
def load_all_to_cache():
    with db_cursor() as (conn, c):
        c.execute("SELECT id, qtype, stem, options, answer, explanation, batch FROM questions")
        qrows = c.fetchall()
        c.execute("SELECT question_id, times_seen, times_correct, consecutive_correct, in_wrong_book FROM progress")
        prows = c.fetchall()
        c.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END), 0) FROM records")
        rrow = c.fetchone()

    qs = {}
    for r in qrows:
        qs[r[0]] = {
            "id": r[0], "qtype": r[1], "stem": r[2],
            "options": json.loads(r[3]) if r[3] else [],
            "answer": r[4], "explanation": r[5], "batch": r[6] or ""
        }
    ps = {r[0]: [r[1], r[2], r[3], r[4]] for r in prows}
    st.session_state.questions_cache = qs
    st.session_state.progress_cache = ps
    st.session_state.total_answers = rrow[0] or 0
    st.session_state.total_correct = rrow[1] or 0


def ensure_cache():
    if "questions_cache" not in st.session_state:
        load_all_to_cache()


def invalidate_cache():
    for k in ("questions_cache", "progress_cache", "total_answers", "total_correct"):
        if k in st.session_state:
            del st.session_state[k]
    load_all_to_cache()


# ============ 异步写入（后台线程） ============
def _async_write_answer(qid, user_answer, is_correct):
    def worker():
        try:
            with db_cursor() as (conn, c):
                c.execute("SELECT times_seen, times_correct, consecutive_correct, in_wrong_book FROM progress WHERE question_id=%s", (qid,))
                row = c.fetchone()
                if row is None:
                    c.execute("INSERT INTO progress (question_id) VALUES (%s) ON CONFLICT (question_id) DO NOTHING", (qid,))
                    row = (0, 0, 0, 0)
                seen, correct, consec, wrong = row
                seen += 1
                if is_correct:
                    correct += 1
                    consec += 1
                    if consec >= 2:
                        wrong = 0
                else:
                    consec = 0
                    wrong = 1
                c.execute("""
                    UPDATE progress SET times_seen=%s, times_correct=%s, consecutive_correct=%s, in_wrong_book=%s
                    WHERE question_id=%s
                """, (seen, correct, consec, wrong, qid))
                c.execute("INSERT INTO records (question_id, user_answer, is_correct) VALUES (%s, %s, %s)", (qid, user_answer, 1 if is_correct else 0))
        except Exception as e:
            print(f"async write error: {e}")
    t = threading.Thread(target=worker, daemon=True)
    t.start()


# ============ 从缓存读取 ============
def get_question_by_id_cached(qid):
    return st.session_state.questions_cache.get(qid)


def get_next_unseen_cached():
    qs = st.session_state.questions_cache
    ps = st.session_state.progress_cache
    ids = sorted([qid for qid, q in qs.items() if q["qtype"] in ("单选", "多选", "判断")])
    for qid in ids:
        p = ps.get(qid)
        if not p or p[0] == 0:
            return qid
    return None


def get_next_wrong_cached():
    qs = st.session_state.questions_cache
    ps = st.session_state.progress_cache
    candidates = []
    for qid, q in qs.items():
        if q["qtype"] not in ("单选", "多选", "判断"):
            continue
        p = ps.get(qid)
        if p and p[3] == 1:
            candidates.append((p[0], qid))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def get_next_random_cached():
    qs = st.session_state.questions_cache
    ids = [qid for qid, q in qs.items() if q["qtype"] in ("单选", "多选", "判断")]
    return random.choice(ids) if ids else None


def get_next_mixed_cached():
    qs = st.session_state.questions_cache
    ps = st.session_state.progress_cache
    unseen = []
    for qid, q in qs.items():
        if q["qtype"] not in ("单选", "多选", "判断"):
            continue
        p = ps.get(qid)
        if not p or p[0] == 0:
            unseen.append(qid)
    if not unseen:
        return None
    return random.choice(unseen)


def get_stats_cached():
    qs = st.session_state.questions_cache
    ps = st.session_state.progress_cache
    total = seen = wrong = 0
    for qid, q in qs.items():
        if q["qtype"] in ("单选", "多选", "判断"):
            total += 1
            p = ps.get(qid)
            if p and p[0] > 0:
                seen += 1
            if p and p[3] == 1:
                wrong += 1
    answers = st.session_state.get("total_answers", 0)
    correct = st.session_state.get("total_correct", 0)
    rate = round(correct / answers * 100, 1) if answers else 0
    return {"total": total, "seen": seen, "wrong": wrong, "answers": answers, "rate": rate}


def get_count_by_type_cached():
    qs = st.session_state.questions_cache
    counts = {}
    for q in qs.values():
        counts[q["qtype"]] = counts.get(q["qtype"], 0) + 1
    return counts


def count_bad_options_cached():
    qs = st.session_state.questions_cache
    bad = 0
    for q in qs.values():
        if len(q["options"]) <= 1 and q["stem"]:
            n = len(re.findall(r'(?:^|[\s。，,;；:：）)\]】])[A-EＡ-Ｅ]\s*[\.、．)）:：。，,]', q["stem"]))
            if n >= 2:
                bad += 1
    return bad


# ============ 数据库写入操作 ============
def parse_excel(file):
    df = pd.read_excel(file)
    cols = {str(c).lower().strip(): c for c in df.columns}

    def find_col(*keys):
        for k in keys:
            if k.lower() in cols:
                return cols[k.lower()]
        return None

    col_stem = find_col("题干", "题目", "问题", "stem")
    col_type = find_col("题型", "类型", "type")
    col_ans = find_col("答案", "正确答案", "参考答案", "answer")
    col_exp = find_col("解析", "explanation")
    opt_cols = []
    for letter in ["A", "B", "C", "D", "E"]:
        for key in [f"选项{letter}", f"option{letter}", letter, letter.lower()]:
            if key.lower() in cols:
                opt_cols.append(cols[key.lower()])
                break

    questions = []
    for _, row in df.iterrows():
        stem = str(row[col_stem]).strip() if col_stem and pd.notna(row[col_stem]) else ""
        if not stem:
            continue
        stem = clean_stem(stem)
        options = []
        for oc in opt_cols:
            if pd.notna(row[oc]):
                options.append(clean_stem(str(row[oc]).strip()))
        if not options:
            stem, options = parse_options_from_content(stem)
            stem = clean_stem(stem)
            options = [clean_stem(o) for o in options]
        answer = str(row[col_ans]).strip() if col_ans and pd.notna(row[col_ans]) else ""
        explanation = str(row[col_exp]).strip() if col_exp and pd.notna(row[col_exp]) else ""
        qtype = normalize_type(row[col_type] if col_type and pd.notna(row[col_type]) else "", stem, options)
        if qtype == "单选" and options and len(answer) > 1 and all(c in "ABCDE" for c in answer.upper()):
            qtype = "多选"
        if qtype == "判断" and answer and answer not in ["正确", "错误", "对", "错", "√", "×", "T", "F"]:
            qtype = "简答/案例"
        questions.append({"qtype": qtype, "stem": stem, "options": options, "answer": answer, "explanation": explanation})
    return questions


def parse_text(text):
    questions = []
    blocks = re.split(r'\n\s*(?=\d+[\.、\s])', text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        m = re.match(r'^(\d+)[\.、\s]+(.*)', block, re.S)
        if not m:
            continue
        content = m.group(2).strip()
        ans_match = re.search(r'(?:参考)?答案[：:\s]*(.+?)(?=\n\s*解析|\n\s*\d+[\.、]|$)', content, re.S)
        answer = ""
        if ans_match:
            answer = ans_match.group(1).strip()
            content = content[:ans_match.start()].strip()
        exp_match = re.search(r'解析[：:\s]*(.*)', content, re.S)
        explanation = ""
        if exp_match:
            explanation = exp_match.group(1).strip()
            content = content[:exp_match.start()].strip()
        stem, options = parse_options_from_content(content)
        stem = clean_stem(stem)
        options = [clean_stem(o) for o in options]
        if not stem:
            continue
        ans_upper = answer.strip().upper()
        if not options:
            if ans_upper in ["正确", "错误", "对", "错", "√", "×", "T", "F"]:
                qtype = "判断"
                if ans_upper in ["对", "√", "T"]:
                    answer = "正确"
                elif ans_upper in ["错", "×", "F"]:
                    answer = "错误"
            elif answer:
                qtype = "简答/案例"
            else:
                qtype = "判断"
        else:
            if len(ans_upper) > 1 and all(c in "ABCDE" for c in ans_upper):
                qtype = "多选"
                answer = ans_upper
            else:
                qtype = "单选"
                answer = ans_upper
        questions.append({"qtype": qtype, "stem": stem, "options": options, "answer": answer, "explanation": explanation})
    return questions


def parse_word(file):
    from docx import Document
    doc = Document(file)
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return parse_text(text)


def parse_pdf(file):
    import pdfplumber
    text_parts = []
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return parse_text("\n".join(text_parts))


def parse_file(uploaded):
    name = uploaded.name.lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        return parse_excel(uploaded)
    if name.endswith(".docx"):
        return parse_word(uploaded)
    if name.endswith(".pdf"):
        return parse_pdf(uploaded)
    if name.endswith(".txt"):
        text = uploaded.read().decode("utf-8", errors="ignore")
        return parse_text(text)
    return []


def normalize_type(t, stem="", options=None):
    t = str(t).strip()
    options = options or []
    if "多" in t and "选" in t:
        return "多选"
    if "单" in t and "选" in t:
        return "单选"
    if "判" in t or "对错" in t:
        return "判断"
    if any(k in t for k in ["简答", "案例", "论述", "名词解释", "分析", "问答"]):
        return "简答/案例"
    if any(k in stem for k in ["简答", "案例分析", "论述", "名词解释"]):
        return "简答/案例"
    if not options:
        return "判断"
    return "单选"


def check_duplicates(questions):
    existing = set()
    for q in st.session_state.questions_cache.values():
        k = normalize_stem_for_dedup(q["stem"])
        existing.add(k)
    seen_in_batch = set()
    dup_flags = []
    dup_count = 0
    for q in questions:
        key = normalize_stem_for_dedup(q["stem"])
        is_dup = (key in existing) or (key in seen_in_batch)
        dup_flags.append(is_dup)
        if is_dup:
            dup_count += 1
        else:
            seen_in_batch.add(key)
    return dup_flags, dup_count


def insert_questions(questions, batch_name):
    inserted = 0
    skipped = 0
    with db_cursor() as (conn, c):
        c.execute("SELECT stem_key FROM questions WHERE stem_key != ''")
        existing = set(r[0] for r in c.fetchall())
        seen_in_batch = set()
        for q in questions:
            if not q["stem"]:
                continue
            key = normalize_stem_for_dedup(q["stem"])
            if key in existing or key in seen_in_batch:
                skipped += 1
                continue
            seen_in_batch.add(key)
            try:
                c.execute("""
                    INSERT INTO questions (qtype, stem, options, answer, explanation, batch, stem_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (q["qtype"], q["stem"], json.dumps(q["options"], ensure_ascii=False),
                      q["answer"], q["explanation"], batch_name, key))
                row = c.fetchone()
                if row:
                    c.execute("INSERT INTO progress (question_id) VALUES (%s) ON CONFLICT (question_id) DO NOTHING", (row[0],))
                inserted += 1
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                skipped += 1
                continue
    return inserted, skipped


def list_batches_from_cache():
    batches = {}
    for q in st.session_state.questions_cache.values():
        b = q.get("batch", "")
        if b:
            batches[b] = batches.get(b, 0) + 1
    return sorted(batches.items(), key=lambda x: x[0], reverse=True)


def delete_batch(batch_name):
    with db_cursor() as (conn, c):
        c.execute("SELECT id FROM questions WHERE batch = %s", (batch_name,))
        qids = [r[0] for r in c.fetchall()]
        if qids:
            c.execute("DELETE FROM records WHERE question_id = ANY(%s)", (qids,))
            c.execute("DELETE FROM progress WHERE question_id = ANY(%s)", (qids,))
            c.execute("DELETE FROM questions WHERE id = ANY(%s)", (qids,))
    return len(qids)


def reset_answer_records():
    with db_cursor() as (conn, c):
        c.execute("DELETE FROM records")
        c.execute("DELETE FROM progress")
        c.execute("INSERT INTO progress (question_id) SELECT id FROM questions ON CONFLICT (question_id) DO NOTHING")


def clear_all():
    with db_cursor() as (conn, c):
        c.execute("DELETE FROM questions")
        c.execute("DELETE FROM records")
        c.execute("DELETE FROM progress")


def extract_answer(s):
    if s is None:
        return ""
    s = str(s).strip()
    if not s:
        return ""
    if s in ["正确", "错误", "对", "错", "√", "×", "T", "F", "t", "f"]:
        if s in ["对", "√", "T", "t"]:
            return "正确"
        if s in ["错", "×", "F", "f"]:
            return "错误"
        return s
    letters = re.findall(r'[A-Ea-e]', s)
    return "".join(sorted(set(L.upper() for L in letters)))


def judge(qtype, correct_answer, user_answer):
    ca = extract_answer(correct_answer)
    ua = extract_answer(user_answer)
    if not ca:
        return None
    if qtype == "多选":
        return ca == ua and len(ca) > 0
    return ca == ua


def format_answer_with_text(qtype, answer_str, options):
    if not answer_str:
        return "（题库未提供）"
    if qtype == "判断":
        return answer_str
    if not options:
        return answer_str
    letters = re.findall(r'[A-Ea-e]', str(answer_str).upper())
    if not letters:
        return answer_str
    letters = sorted(set(L.upper() for L in letters))
    parts = []
    for L in letters:
        idx = ord(L) - ord('A')
        if 0 <= idx < len(options):
            parts.append(f"{L}. {options[idx]}")
        else:
            parts.append(L)
    return "　".join(parts)


def _do_judge(q, user_answer):
    """本地判分 + 更新缓存 + 异步写库，不阻塞 UI。"""
    result = judge(q["qtype"], q["answer"], user_answer)
    st.session_state.last_user_answer = user_answer
    if result is None:
        st.session_state.last_correct = None
    else:
        st.session_state.last_correct = result
        qid = q["id"]
        ps = st.session_state.progress_cache
        p = list(ps.get(qid, [0, 0, 0, 0]))
        p[0] += 1
        if result:
            p[1] += 1
            p[2] += 1
            if p[2] >= 2:
                p[3] = 0
        else:
            p[2] = 0
            p[3] = 1
        ps[qid] = p
        st.session_state.total_answers = st.session_state.get("total_answers", 0) + 1
        if result:
            st.session_state.total_correct = st.session_state.get("total_correct", 0) + 1
        _async_write_answer(qid, user_answer, result)
    st.session_state.answered = True
    st.rerun()


# ============ 界面样式 ============
FONT_SIZES = {"标准": 18, "大": 22, "很大": 26, "特大": 30}


def inject_css():
    fs = st.session_state.font_size
    st.markdown(f"""
    <style>
    html, body {{ overflow-y: auto !important; height: auto !important; }}
    .stApp, [data-testid="stAppViewContainer"], .stMain, section.main {{ overflow-y: auto !important; }}
    [data-testid="stSidebar"] > div:first-child {{ overflow-y: auto !important; padding-bottom: 3rem !important; }}
    .stMainBlockContainer, .block-container {{ max-width: 1100px !important; margin: 0 auto !important; padding: 1rem 1rem 10rem 1rem !important; }}
    html, body, [class*="css"] {{ font-size: {fs}px !important; }}
    [data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li, [data-testid="stMarkdownContainer"] span {{
        font-size: {fs}px !important; line-height: 1.7 !important; word-break: break-word !important;
    }}
    .stem-text {{ font-size: {fs + 3}px !important; line-height: 1.7 !important; font-weight: 600 !important; margin: 0.6em 0 1em 0 !important; color: #111 !important; }}
    .answer-line {{ font-size: {fs + 1}px !important; line-height: 1.9 !important; padding: 0.4em 0 !important; }}
    .answer-correct {{ color: #0a7a3a !important; font-weight: 700 !important; }}
    .answer-wrong {{ color: #c0392b !important; font-weight: 700 !important; }}
    h1 {{ font-size: {fs + 10}px !important; }}
    h2 {{ font-size: {fs + 6}px !important; }}
    h3 {{ font-size: {fs + 3}px !important; }}
    .stButton > button {{ font-size: {fs}px !important; padding: 0.8em 1em !important; min-height: 3.4em !important; border-radius: 10px !important; font-weight: 600 !important; text-align: left !important; white-space: normal !important; line-height: 1.5 !important; width: 100% !important; }}
    [data-testid="stMetricValue"] {{ font-size: {fs + 6}px !important; }}
    [data-testid="stMetricLabel"] {{ font-size: {max(fs - 2, 14)}px !important; }}
    [data-testid="stSidebar"] label, [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {{ font-size: {fs}px !important; }}
    [data-testid="stRadio"] label, [data-testid="stCheckbox"] label {{ font-size: {fs + 1}px !important; }}
    .dup-tag {{ display: inline-block; background: #f39c12; color: #fff; font-size: {max(fs - 3, 13)}px; padding: 2px 8px; border-radius: 6px; margin-left: 6px; }}
    .opt-count {{ display: inline-block; background: #2e86de; color: #fff; font-size: {max(fs - 3, 13)}px; padding: 2px 8px; border-radius: 6px; margin-left: 6px; }}
    .opt-count-bad {{ display: inline-block; background: #e74c3c; color: #fff; font-size: {max(fs - 3, 13)}px; padding: 2px 8px; border-radius: 6px; margin-left: 6px; }}
    @media (max-width: 768px) {{
        .stMainBlockContainer, .block-container {{ padding: 0.6rem 0.6rem 8rem 0.6rem !important; max-width: 100% !important; }}
        html, body, [class*="css"] {{ font-size: {max(fs - 2, 16)}px !important; }}
        .stem-text {{ font-size: {max(fs + 1, 20)}px !important; }}
        .stButton > button {{ font-size: {max(fs - 1, 17)}px !important; min-height: 3.6em !important; }}
        [data-testid="column"] {{ min-width: calc(50% - 0.5rem) !important; flex: 1 1 calc(50% - 0.5rem) !important; }}
    }}
    </style>
    """, unsafe_allow_html=True)


# ============ 界面 ============
ensure_cache()
inject_css()

st.title("海口边检辅警刷题系统")

with st.sidebar:
    st.header("菜单")
    page = st.radio("选择页面", ["上传题库", "管理题库", "开始刷题", "简答/案例分析", "错题本", "统计"])
    st.divider()
    st.subheader("显示设置")
    current_label = [k for k, v in FONT_SIZES.items() if v == st.session_state.font_size]
    current_idx = list(FONT_SIZES.keys()).index(current_label[0]) if current_label else 1
    size_label = st.radio("字体大小", list(FONT_SIZES.keys()), index=current_idx, horizontal=True)
    st.session_state.font_size = FONT_SIZES[size_label]


# ---------- 上传题库 ----------
if page == "上传题库":
    st.subheader("上传带答案的题库文档")
    st.write("支持：Excel / Word / PDF / TXT")
    st.info("📌 系统会自动去重：题干相同的题，只保留一道。")

    bad = count_bad_options_cached()
    if bad > 0:
        st.error(f"🚨 检测到 {bad} 道题的选项疑似被合并。请先到「管理题库」清空，再重新上传。")

    with st.expander("格式说明", expanded=False):
        st.markdown("""
**Excel 建议列名：** 题型 | 题干 | 选项A | 选项B | 选项C | 选项D | 选项E | 答案 | 解析

**Word / PDF / TXT 示例：**

1. 以下哪个是海南自贸港的核心政策？（单选）
A. 零关税
B. 高关税
C. 配额限制
D. 出口补贴
答案：A

**选项写法（任选）：** `A. xx`（换行） / `A.xxB.yyC.zz`（挤一行） / `A xx B xx`（空格） / `AxxBxx`（无标点）
        """)

    uploaded = st.file_uploader("选择文件", type=["xlsx", "xls", "docx", "pdf", "txt"])

    if uploaded:
        with st.spinner("解析中..."):
            try:
                questions = parse_file(uploaded)
            except Exception as e:
                st.error(f"解析失败：{e}")
                questions = []

        if questions:
            counts = {}
            bad_count = 0
            for q in questions:
                counts[q["qtype"]] = counts.get(q["qtype"], 0) + 1
                if q["qtype"] in ("单选", "多选") and len(q["options"]) < 2:
                    bad_count += 1
            st.success(f"解析出 {len(questions)} 道　" + "　".join([f"{k}：{v}" for k, v in counts.items()]))
            if bad_count > 0:
                st.error(f"⚠️ {bad_count} 道题的选项没拆开。")

            dup_flags, dup_count = check_duplicates(questions)
            if dup_count > 0:
                st.warning(f"⚠️ {dup_count} 道题与已有题库重复，导入时会跳过。")

            default_batch = f"{Path(uploaded.name).stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            batch_name = st.text_input("批次名称", value=default_batch)

            col1, col2 = st.columns(2)
            with col1:
                if st.button("✅ 确认导入数据库", type="primary", use_container_width=True):
                    if not batch_name.strip():
                        st.error("批次名不能为空。")
                    else:
                        with st.spinner("导入中..."):
                            n, skipped = insert_questions(questions, batch_name.strip())
                            invalidate_cache()
                        if skipped:
                            st.success(f"导入 {n} 道，跳过 {skipped} 道重复。")
                        else:
                            st.success(f"导入 {n} 道。")
                        st.balloons()
            with col2:
                if st.button("🗑️ 放弃", use_container_width=True):
                    st.rerun()

            with st.expander(f"查看全部 {len(questions)} 道（点击展开）", expanded=True):
                for i, q in enumerate(questions, 1):
                    is_dup = dup_flags[i - 1] if i - 1 < len(dup_flags) else False
                    tags = '<span class="dup-tag">重复</span>' if is_dup else ''
                    n_opts = len(q["options"])
                    if q["qtype"] in ("单选", "多选") and n_opts < 2:
                        tags += f'<span class="opt-count-bad">选项：{n_opts}（未拆）</span>'
                    else:
                        tags += f'<span class="opt-count">选项：{n_opts}</span>'
                    st.markdown(f"**第 {i} 题【{q['qtype']}】**{tags}", unsafe_allow_html=True)
                    st.markdown(f'<div class="stem-text">{html.escape(q["stem"])}</div>', unsafe_allow_html=True)
                    for j, o in enumerate(q["options"]):
                        st.write(f"　{chr(65+j)}. {o}")
                    st.write(f"　**答案：** {q['answer'] or '（未识别）'}")
                    if q["explanation"]:
                        st.write(f"　**解析：** {q['explanation']}")
                    st.divider()
        else:
            st.warning("没有解析出题目。")


# ---------- 管理题库 ----------
elif page == "管理题库":
    st.subheader("管理已导入的题库")
    batches = list_batches_from_cache()
    if not batches:
        st.info("当前没有已导入的批次。")
    else:
        st.write(f"共 {len(batches)} 个批次：")
        for batch_name, count in batches:
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write(f"**{batch_name}**")
                st.caption(f"{count} 道")
            with col2:
                if st.button("删除", key=f"del_{batch_name}", type="secondary"):
                    with st.spinner("删除中..."):
                        n = delete_batch(batch_name)
                        invalidate_cache()
                    st.success(f"已删除「{batch_name}」，{n} 道。")
                    st.rerun()
            st.divider()

    st.divider()
    st.subheader("当前题库汇总")
    counts = get_count_by_type_cached()
    if not counts:
        st.write("题库为空。")
    else:
        for k, v in counts.items():
            st.write(f"- {k}：{v} 道")
        total = sum(counts.values())
        st.write(f"**合计：{total} 道**")
        bad = count_bad_options_cached()
        if bad > 0:
            st.error(f"🚨 {bad} 道题选项疑似未拆开。")

    st.divider()
    st.subheader("重置答题记录（保留题库）")
    if st.button("🔄 重置答题记录", type="secondary"):
        with st.spinner("重置中..."):
            reset_answer_records()
            invalidate_cache()
        st.success("已重置。")

    st.divider()
    st.subheader("危险操作")
    if st.button("🚨 清空全部题库和记录", type="secondary"):
        with st.spinner("清空中..."):
            clear_all()
            invalidate_cache()
        st.success("已清空。")
        st.rerun()


# ---------- 开始刷题 ----------
elif page == "开始刷题":
    stats = get_stats_cached()
    if stats["total"] == 0:
        st.warning("选择题/判断题题库为空，请先导入。")
        st.stop()

    mode = st.radio("刷题模式", ["顺序刷未做题", "错题复习", "随机抽题", "实战混合"], horizontal=True)

    if st.session_state.current_qid is None:
        if mode == "顺序刷未做题":
            qid = get_next_unseen_cached()
            if qid is None:
                st.session_state.all_done_choice = mode
                qid = None
            else:
                st.session_state.all_done_choice = None
        elif mode == "错题复习":
            qid = get_next_wrong_cached()
            if qid is None:
                st.session_state.all_done_choice = mode
                qid = None
            else:
                st.session_state.all_done_choice = None
        elif mode == "实战混合":
            qid = get_next_mixed_cached()
            if qid is None:
                st.session_state.all_done_choice = mode
                qid = None
            else:
                st.session_state.all_done_choice = None
        else:
            qid = get_next_random_cached()
            st.session_state.all_done_choice = None
        if qid is not None:
            st.session_state.current_qid = qid
            st.session_state.answered = False
            st.session_state.last_correct = None
            st.session_state.last_user_answer = ""

    if st.session_state.all_done_choice is not None:
        st.success("🎉 恭喜！你已经把这批题全部做完了。")
        st.write(f"总题数：{stats['total']}　已做：{stats['seen']}　错题：{stats['wrong']}　正确率：{stats['rate']}%")
        st.divider()
        st.subheader("是否重新开始刷题？")
        st.write("重新开始会清空答题记录和错题本，题库不会删除。")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔄 是，重新开始", type="primary", use_container_width=True):
                with st.spinner("清空记录中..."):
                    reset_answer_records()
                    invalidate_cache()
                st.session_state.current_qid = None
                st.session_state.answered = False
                st.session_state.last_correct = None
                st.session_state.last_user_answer = ""
                st.session_state.all_done_choice = None
                st.rerun()
        with col2:
            if st.button("否", use_container_width=True):
                st.session_state.all_done_choice = None
                st.rerun()
        st.stop()

    q = get_question_by_id_cached(st.session_state.current_qid)
    if q is None:
        st.session_state.current_qid = None
        st.rerun()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("总题数", stats["total"])
    col2.metric("已做", stats["seen"])
    col3.metric("错题", stats["wrong"])
    col4.metric("正确率", f"{stats['rate']}%")

    st.divider()
    st.markdown(f"**【{q['qtype']}】第 {q['id']} 题**")
    st.markdown(f'<div class="stem-text">{html.escape(q["stem"])}</div>', unsafe_allow_html=True)

    options = q["options"]

    if q["qtype"] in ("单选", "多选") and len(options) < 2:
        st.error(f"⚠️ 这道题的选项没有拆开（只有 {len(options)} 个）。")
        st.error("请到「管理题库」清空后重新上传。")
        st.stop()

    if not st.session_state.answered:
        if q["qtype"] == "判断":
            col_y, col_n = st.columns(2)
            if col_y.button("✔ 正确", key=f"opt_{q['id']}_T", use_container_width=True):
                _do_judge(q, "正确")
            if col_n.button("✘ 错误", key=f"opt_{q['id']}_F", use_container_width=True):
                _do_judge(q, "错误")
        elif q["qtype"] == "多选":
            st.write("（多选，漏选、多选、错选都算错。勾选后点「确认选择」）")
            selected = []
            for i, o in enumerate(options):
                letter = chr(65 + i)
                if st.checkbox(f"{letter}. {o}", key=f"cb_{q['id']}_{letter}"):
                    selected.append(letter)
            if st.button("确认选择", type="primary", key=f"confirm_{q['id']}", use_container_width=True):
                if not selected:
                    st.warning("请至少勾选一个选项。")
                else:
                    _do_judge(q, "".join(selected))
        else:
            for i, o in enumerate(options):
                letter = chr(65 + i)
                if st.button(f"{letter}. {o}", key=f"opt_{q['id']}_{letter}", use_container_width=True):
                    _do_judge(q, letter)

    if st.session_state.answered:
        st.divider()
        correct = st.session_state.last_correct
        if correct is True:
            st.success("✅ 答对了")
        elif correct is False:
            st.error("❌ 答错了")
        else:
            st.warning("⚠️ 待核对（题库无明确答案，不计分）")
        user_ans_text = format_answer_with_text(q["qtype"], st.session_state.last_user_answer, q["options"])
        correct_ans_text = format_answer_with_text(q["qtype"], q["answer"], q["options"])
        st.markdown(f'<div class="answer-line">你的答案：<span class="answer-wrong">{html.escape(user_ans_text)}</span></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="answer-line">正确答案：<span class="answer-correct">{html.escape(correct_ans_text)}</span></div>', unsafe_allow_html=True)
        if q["explanation"]:
            st.write(f"**解析：** {q['explanation']}")
        else:
            st.write("**解析：** （题库未提供解析）")
        st.divider()
        if st.button("下一题 →", type="primary", use_container_width=True):
            st.session_state.current_qid = None
            st.session_state.answered = False
            st.session_state.last_correct = None
            st.session_state.last_user_answer = ""
            st.rerun()


# ---------- 简答/案例分析 ----------
elif page == "简答/案例分析":
    st.subheader("简答题 / 案例分析题")
    cases = [q for q in st.session_state.questions_cache.values() if q["qtype"] == "简答/案例"]
    cases.sort(key=lambda x: x["id"])
    if not cases:
        st.write("题库里还没有简答/案例题。")
    else:
        if st.session_state.case_idx >= len(cases):
            st.session_state.case_idx = 0
        q = cases[st.session_state.case_idx]
        st.markdown(f"**第 {st.session_state.case_idx + 1} / {len(cases)} 题**")
        st.markdown(f'<div class="stem-text">{html.escape(q["stem"])}</div>', unsafe_allow_html=True)
        if st.button("查看参考答案", type="primary"):
            st.session_state.show_case_answer = True
        if st.session_state.show_case_answer:
            st.success(f"**参考答案：**\n\n{q['answer'] or '（文档未提供答案）'}")
            if q["explanation"]:
                st.info(f"**解析：**\n\n{q['explanation']}")
        st.divider()
        col1, col2 = st.columns(2)
        if col1.button("← 上一题", use_container_width=True):
            st.session_state.case_idx = max(0, st.session_state.case_idx - 1)
            st.session_state.show_case_answer = False
            st.rerun()
        if col2.button("下一题 →", use_container_width=True):
            st.session_state.case_idx = min(len(cases) - 1, st.session_state.case_idx + 1)
            st.session_state.show_case_answer = False
            st.rerun()


# ---------- 错题本 ----------
elif page == "错题本":
    st.subheader("错题本")
    qs = st.session_state.questions_cache
    ps = st.session_state.progress_cache
    rows = []
    for qid, q in qs.items():
        if q["qtype"] not in ("单选", "多选", "判断"):
            continue
        p = ps.get(qid)
        if p and p[3] == 1:
            rows.append({"id": qid, "题型": q["qtype"], "题干": q["stem"], "答案": q["answer"], "做题次数": p[0], "对": p[1]})
    if not rows:
        st.write("错题本是空的。")
    else:
        df = pd.DataFrame(rows)
        st.write(f"共 {len(df)} 道错题。")
        st.dataframe(df, use_container_width=True)
        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("导出错题本 CSV", csv, "错题本.csv", "text/csv")


# ---------- 统计 ----------
elif page == "统计":
    st.subheader("答题统计")
    stats = get_stats_cached()
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("选择/判断总数", stats["total"])
    col2.metric("已做", stats["seen"])
    col3.metric("错题", stats["wrong"])
    col4.metric("总答题次数", stats["answers"])
    col5.metric("正确率", f"{stats['rate']}%")

    st.divider()
    st.subheader("题库构成")
    counts = get_count_by_type_cached()
    if counts:
        for k, v in counts.items():
            st.write(f"- {k}：{v} 道")
    else:
        st.write("题库为空。")

    st.divider()
    if st.button("🔄 重置答题记录（保留题库）", type="secondary", use_container_width=True):
        with st.spinner("重置中..."):
            reset_answer_records()
            invalidate_cache()
        st.success("已重置。")