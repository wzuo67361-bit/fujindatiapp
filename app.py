import streamlit as st
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
import re, json, html, threading, random, os, time, traceback, sys
import pandas as pd
from pathlib import Path
from datetime import datetime

st.set_page_config(page_title="辅助人员刷题系统", layout="wide", initial_sidebar_state="collapsed")

# --- Session State 初始化 ---
for key, default in [
    ("font_size", 20), ("current_qid", None), ("answered", False),
    ("last_correct", None), ("last_user_answer", ""), ("all_done_choice", None),
    ("case_idx", 0), ("show_case_answer", False), ("db_ok", None),
    ("db_error", ""), ("last_error_trace", "")
]:
    if key not in st.session_state:
        st.session_state[key] = default

# --- 数据库连接 ---
def get_db_url():
    try:
        if "DATABASE_URL" in st.secrets:
            return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    return os.environ.get("DATABASE_URL", "")

@st.cache_resource
def get_pool():
    return pool.SimpleConnectionPool(
        1, 5, get_db_url(),
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
        connect_timeout=15,
    )

@contextmanager
def db_cursor():
    p = get_pool()
    conn = None
    try:
        conn = p.getconn()
        if conn.closed:
            p.putconn(conn, close=True)
            conn = p.getconn()
        yield conn, conn.cursor()
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.DatabaseError) as e:
        if conn is not None:
            try: p.putconn(conn, close=True)
            except Exception: pass
        try: get_pool.clear()
        except Exception: pass
        p2 = get_pool()
        conn = p2.getconn()
        try:
            yield conn, conn.cursor()
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            p2.putconn(conn)
    except Exception:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
        raise
    finally:
        if conn is not None:
            try: p.putconn(conn)
            except Exception: pass

# --- 自检函数 ---
def check_database():
    """检测数据库连接是否正常"""
    try:
        with db_cursor() as (conn, c):
            c.execute("SELECT 1")
            c.fetchone()
        st.session_state.db_ok = True
        st.session_state.db_error = ""
        return True
    except Exception as e:
        st.session_state.db_ok = False
        st.session_state.db_error = str(e)
        st.session_state.last_error_trace = traceback.format_exc()
        return False

def check_tables():
    """检测三张表是否存在"""
    try:
        with db_cursor() as (conn, c):
            for t in ["questions", "records", "progress"]:
                c.execute(f"SELECT COUNT(*) FROM {t}")
                c.fetchone()
        return True, ""
    except Exception as e:
        return False, f"表结构异常: {e}"

def get_error_code_snippet():
    """生成可复制的错误代码片段，方便用户发给AI排查"""
    err = st.session_state.db_error
    trace = st.session_state.last_error_trace
    snippet = f"""
# ===== 错误报告（请复制给AI） =====
# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# 错误类型: 数据库连接/查询异常
# 错误信息: {err}
# 
# 完整堆栈:
{trace}
# ===== 报告结束 =====
"""
    return snippet

# --- 工具函数（省略部分，保持原样）---
def normalize_stem_for_dedup(stem):
    if not stem: return ""
    s = re.sub(r'\s+', '', str(stem).strip())
    s = re.sub(r'[，。、；：？！,\.;:\?!"\'""''（）()【】\[\]《》<>—\-_]', '', s)
    return s.lower()

def clean_stem(text):
    if not text: return text
    for p in [
        r'[（(]\s*[本共每]?题?\s*\d+(\.\d+)?\s*分\s*[)）]',
        r'[\[【]\s*[本共每]?题?\s*\d+(\.\d+)?\s*分\s*[\]】]',
        r'[（(]\s*第\s*\d+\s*题\s*[，,]?\s*\d+(\.\d+)?\s*分\s*[)）]',
        r'[（(]\s*[本共每]?题?\s*\d+(\.\d+)?\s*分\s*[，,].*?[)）]',
    ]:
        text = re.sub(p, '', text)
    return text.strip()

def clean_parse_text(text):
    if not text: return ""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    for p in [r'={2,}\s*Page\s*\d+\s*={2,}', r'={3,}', r'-{3,}', r'\n{3,}']:
        text = re.sub(p, '\n', text)
    return text

def to_halfwidth_letter(ch):
    if not ch: return ''
    ch = ch.upper()
    if 'Ａ' <= ch <= 'Ｅ': return chr(ord(ch) - ord('Ａ') + ord('A'))
    if 'A' <= ch <= 'E': return ch
    return ''

def _extract_with_pattern(content, pattern, allow_ocr_shift=False):
    raw_matches = list(pattern.finditer(content))
    if not raw_matches: return content, []
    candidates = []
    last_letter_ord = ord('A') - 1
    for m in raw_matches:
        groups = m.groups()
        letter = ''
        if allow_ocr_shift and len(groups) >= 3 and groups[2] is not None:
            o = last_letter_ord + 1
            if o > ord('E'): continue
            letter = chr(o)
        else:
            for g in groups:
                if g:
                    letter = to_halfwidth_letter(g)
                    break
            if not letter: continue
        candidates.append((m, letter))
        last_letter_ord = ord(letter)
    if len(candidates) < 2: return content, []
    best_seq = []
    for start_idx in range(len(candidates)):
        seq = [candidates[start_idx]]
        expected = chr(ord(candidates[start_idx][1]) + 1)
        used = {candidates[start_idx][1]}
        for j in range(start_idx + 1, len(candidates)):
            c_letter = candidates[j][1]
            if c_letter == expected and c_letter not in used:
                seq.append(candidates[j]); used.add(c_letter)
                expected = chr(ord(expected) + 1)
                if expected > 'E': break
        if len(seq) > len(best_seq): best_seq = seq
    if len(best_seq) < 2: return content, []
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
    if not content: return content, []
    for pat, shift in [
        (r'(?:[（(\[【]\s*([A-Ea-eＡ-Ｅａ-ｅ])\s*[）)\]】]|([A-Ea-eＡ-Ｅａ-ｅ])\s*[\.、．)）:：。，,\]]|[（(]\s*[\.、．])', True),
        (r'(?:^|[\s。，,;；:：）)\]】])([A-EＡ-Ｅ])\s+', False),
        (r'(?:^|[\s。，,;；:：）)\]】])([A-EＡ-Ｅ])(?=[\u4e00-\u9fa5])', False),
    ]:
        result = _extract_with_pattern(content, re.compile(pat), allow_ocr_shift=shift)
        if result[1]: return result
    return content, []

# --- 数据库初始化（带自检）---
@st.cache_resource
def init_db_once():
    try:
        with db_cursor() as (conn, c):
            c.execute("""CREATE TABLE IF NOT EXISTS questions (id BIGSERIAL PRIMARY KEY, qtype TEXT, stem TEXT, options TEXT, answer TEXT, explanation TEXT, batch TEXT DEFAULT '', tag TEXT DEFAULT '', stem_key TEXT DEFAULT '')""")
            c.execute("""CREATE TABLE IF NOT EXISTS records (id BIGSERIAL PRIMARY KEY, question_id BIGINT, user_answer TEXT, is_correct INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS progress (question_id BIGINT PRIMARY KEY, times_seen INTEGER DEFAULT 0, times_correct INTEGER DEFAULT 0, consecutive_correct INTEGER DEFAULT 0, in_wrong_book INTEGER DEFAULT 0)""")
            c.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS batch TEXT DEFAULT ''")
            c.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS stem_key TEXT DEFAULT ''")
            c.execute("SELECT id, stem FROM questions WHERE stem_key = '' OR stem_key IS NULL")
            for qid, stem in c.fetchall():
                c.execute("UPDATE questions SET stem_key=%s WHERE id=%s", (normalize_stem_for_dedup(stem), qid))
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_stem_key ON questions(stem_key) WHERE stem_key != ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_records_qid ON records(question_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_progress_wrong ON progress(in_wrong_book)")
        return True, ""
    except Exception as e:
        return False, str(e)

db_init_ok, db_init_err = init_db_once()

# --- 缓存加载 ---
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
        qs[r[0]] = {"id": r[0], "qtype": r[1], "stem": r[2], "options": json.loads(r[3]) if r[3] else [], "answer": r[4], "explanation": r[5], "batch": r[6] or ""}
    ps = {r[0]: [r[1], r[2], r[3], r[4]] for r in prows}
    st.session_state.questions_cache = qs
    st.session_state.progress_cache = ps
    st.session_state.total_answers = rrow[0] or 0
    st.session_state.total_correct = rrow[1] or 0

def ensure_cache():
    if "questions_cache" not in st.session_state:
        try:
            load_all_to_cache()
        except Exception as e:
            st.session_state.db_ok = False
            st.session_state.db_error = str(e)
            st.session_state.last_error_trace = traceback.format_exc()

def invalidate_cache():
    for k in ("questions_cache", "progress_cache", "total_answers", "total_correct"):
        if k in st.session_state: del st.session_state[k]
    load_all_to_cache()

# --- 异步写入（带重试）---
def _async_write_answer(qid, user_answer, is_correct):
    def worker():
        for attempt in range(3):
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
                        correct += 1; consec += 1
                        if consec >= 2: wrong = 0
                    else:
                        consec = 0; wrong = 1
                    c.execute("UPDATE progress SET times_seen=%s, times_correct=%s, consecutive_correct=%s, in_wrong_book=%s WHERE question_id=%s", (seen, correct, consec, wrong, qid))
                    c.execute("INSERT INTO records (question_id, user_answer, is_correct) VALUES (%s, %s, %s)", (qid, user_answer, 1 if is_correct else 0))
                return
            except Exception as e:
                print(f"write attempt {attempt+1} failed: {e}")
                time.sleep(1)
    threading.Thread(target=worker, daemon=True).start()

# --- 缓存读取函数（省略，保持原样）---
# ... [此处省略 get_question_by_id_cached 等函数，与原版相同] ...

# --- 解析函数（省略，保持原样）---
# ... [此处省略 parse_text, parse_excel, parse_word, parse_pdf, parse_file] ...

# --- 数据库操作函数（省略，保持原样）---
# ... [此处省略 insert_questions, delete_batch, reset_answer_records, clear_all] ...

# --- 判分函数（省略，保持原样）---
# ... [此处省略 extract_answer, judge, format_answer_with_text, _do_judge] ...

# --- CSS 样式（省略，保持原样）---
# ... [此处省略 inject_css] ...

# ============ 界面 ============
ensure_cache()
inject_css()
st.title("海口边检辅警刷题系统")

# --- 自检面板（新增）---
with st.sidebar:
    st.header("菜单")
    page = st.radio("选择页面", ["上传题库", "管理题库", "开始刷题", "简答/案例分析", "错题本", "统计", "系统自检"])
    st.divider()
    # 显示数据库状态
    if st.session_state.db_ok is False:
        st.error("🔴 数据库连接异常")
    elif st.session_state.db_ok is True:
        st.success("🟢 数据库连接正常")
    else:
        st.info("⏳ 数据库状态未知")
    # 字体设置
    cl = [k for k, v in FONT_SIZES.items() if v == st.session_state.font_size]
    ci = list(FONT_SIZES.keys()).index(cl[0]) if cl else 1
    sl = st.radio("字体大小", list(FONT_SIZES.keys()), index=ci, horizontal=True)
    st.session_state.font_size = FONT_SIZES[sl]

# --- 系统自检页面（新增）---
if page == "系统自检":
    st.subheader("🔧 系统自检面板")
    st.write("检测应用与数据库的健康状态，出错时提供可复制的代码片段。")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔍 检测数据库连接", use_container_width=True):
            ok = check_database()
            if ok: st.success("数据库连接正常")
            else: st.error(f"数据库连接失败：{st.session_state.db_error}")
    with col2:
        if st.button("📋 检测表结构", use_container_width=True):
            ok, msg = check_tables()
            if ok: st.success("三张表均存在")
            else: st.error(msg)

    st.divider()
    st.subheader("📊 当前状态")
    st.write(f"- 数据库连接：{'正常' if st.session_state.db_ok else '异常' if st.session_state.db_ok is False else '未检测'}")
    st.write(f"- 题库缓存：{'已加载' if 'questions_cache' in st.session_state else '未加载'}")
    st.write(f"- 题库总数：{len(st.session_state.get('questions_cache', {}))}")

    if st.session_state.db_ok is False:
        st.divider()
        st.error("检测到数据库错误，以下是可复制的代码片段：")
        snippet = get_error_code_snippet()
        st.code(snippet, language="python")
        st.info("👆 复制上面这段代码，发给AI即可帮你排查问题。")

# --- 其他页面（上传题库、管理题库、开始刷题等）保持原样 ---
# ... [此处省略原有页面代码] ...
