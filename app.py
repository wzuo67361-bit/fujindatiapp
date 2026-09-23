import streamlit as st
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
import re, json, html, threading, random, os, time, traceback
import pandas as pd
from datetime import datetime

st.set_page_config(page_title="海口边检辅警刷题系统", layout="wide", initial_sidebar_state="collapsed")

for k, v in [("font_size",20),("current_qid",None),("answered",False),("last_correct",None),
             ("last_user_answer",""),("all_done_choice",None),("case_idx",0),
             ("show_case_answer",False),("db_ok",None),("db_error",""),("last_error_trace","")]:
    if k not in st.session_state: st.session_state[k] = v


def get_db_url():
    try:
        if "DATABASE_URL" in st.secrets: return st.secrets["DATABASE_URL"]
    except Exception: pass
    return os.environ.get("DATABASE_URL","")


@st.cache_resource
def get_pool():
    return pool.SimpleConnectionPool(1,5,get_db_url(),
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5, connect_timeout=15)


@contextmanager
def db_cursor():
    p = get_pool(); conn = None
    try:
        conn = p.getconn()
        if conn.closed:
            p.putconn(conn, close=True); conn = p.getconn()
        yield conn, conn.cursor()
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.DatabaseError):
        if conn is not None:
            try: p.putconn(conn, close=True)
            except Exception: pass
        try: get_pool.clear()
        except Exception: pass
        p2 = get_pool(); conn = p2.getconn()
        try:
            yield conn, conn.cursor(); conn.commit()
        except Exception:
            conn.rollback(); raise
        finally: p2.putconn(conn)
    except Exception:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
        raise
    finally:
        if conn is not None:
            try: p.putconn(conn)
            except Exception: pass


def check_database():
    try:
        with db_cursor() as (conn,c):
            c.execute("SELECT 1"); c.fetchone()
        st.session_state.db_ok = True; st.session_state.db_error = ""
        return True
    except Exception as e:
        st.session_state.db_ok = False
        st.session_state.db_error = str(e)
        st.session_state.last_error_trace = traceback.format_exc()
        return False


def check_tables():
    try:
        with db_cursor() as (conn,c):
            for t in ["questions","records","progress"]:
                c.execute(f"SELECT COUNT(*) FROM {t}"); c.fetchone()
        return True, ""
    except Exception as e:
        return False, f"表结构异常: {e}"


def get_error_snippet():
    return f"""
# ===== 错误报告（复制给AI） =====
# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# 错误: {st.session_state.db_error}
# 堆栈:
{st.session_state.last_error_trace}
# ===== 结束 =====
"""


def normalize_stem_for_dedup(stem):
    if not stem: return ""
    s = re.sub(r'\s+','',str(stem).strip())
    s = re.sub(r'[，。、；：？！,\.;:\?!"\'""''（）()【】\[\]《》<>—\-_]','',s)
    return s.lower()


def clean_stem(text):
    if not text: return text
    for p in [r'[（(]\s*[本共每]?题?\s*\d+(\.\d+)?\s*分\s*[)）]',
              r'[\[【]\s*[本共每]?题?\s*\d+(\.\d+)?\s*分\s*[\]】]',
              r'[（(]\s*第\s*\d+\s*题\s*[，,]?\s*\d+(\.\d+)?\s*分\s*[)）]',
              r'[（(]\s*[本共每]?题?\s*\d+(\.\d+)?\s*分\s*[，,].*?[)）]']:
        text = re.sub(p,'',text)
    return text.strip()


def clean_parse_text(text):
    if not text: return ""
    text = text.replace('\r\n','\n').replace('\r','\n')
    for p in [r'={2,}\s*Page\s*\d+\s*={2,}', r'={3,}', r'-{3,}', r'\n{3,}']:
        text = re.sub(p,'\n',text)
    return text


def to_halfwidth_letter(ch):
    if not ch: return ''
    ch = ch.upper()
    if 'Ａ' <= ch <= 'Ｅ': return chr(ord(ch)-ord('Ａ')+ord('A'))
    if 'A' <= ch <= 'E': return ch
    return ''


def _extract_with_pattern(content, pattern, allow_ocr_shift=False):
    raw = list(pattern.finditer(content))
    if not raw: return content, []
    cands = []; last = ord('A')-1
    for m in raw:
        gs = m.groups(); letter = ''
        if allow_ocr_shift and len(gs)>=3 and gs[2] is not None:
            o = last+1
            if o > ord('E'): continue
            letter = chr(o)
        else:
            for g in gs:
                if g: letter = to_halfwidth_letter(g); break
            if not letter: continue
        cands.append((m,letter)); last = ord(letter)
    if len(cands) < 2: return content, []
    best = []
    for i in range(len(cands)):
        seq = [cands[i]]; exp = chr(ord(cands[i][1])+1); used = {cands[i][1]}
        for j in range(i+1,len(cands)):
            cl = cands[j][1]
            if cl == exp and cl not in used:
                seq.append(cands[j]); used.add(cl); exp = chr(ord(exp)+1)
                if exp > 'E': break
        if len(seq) > len(best): best = seq
    if len(best) < 2: return content, []
    stem = content[:best[0][0].start()].strip()
    opts = []
    for i,(m,l) in enumerate(best):
        s = m.end(); e = best[i+1][0].start() if i+1 < len(best) else len(content)
        o = content[s:e].strip()
        o = re.sub(r'^[\.、．)）:：。，,\s]+','',o)
        opts.append(o)
    return stem, opts


def parse_options_from_content(content):
    if not content: return content, []
    for pat, shift in [
        (r'(?:[（(\[【]\s*([A-Ea-eＡ-Ｅａ-ｅ])\s*[）)\]】]|([A-Ea-eＡ-Ｅａ-ｅ])\s*[\.、．)）:：。，,\]]|[（(]\s*[\.、．])', True),
        (r'(?:^|[\s。，,;；:：）)\]】])([A-EＡ-Ｅ])\s+', False),
        (r'(?:^|[\s。，,;；:：）)\]】])([A-EＡ-Ｅ])(?=[\u4e00-\u9fa5])', False),
    ]:
        r = _extract_with_pattern(content, re.compile(pat), allow_ocr_shift=shift)
        if r[1]: return r
    return content, []


@st.cache_resource
def init_db_once():
    try:
        with db_cursor() as (conn,c):
            c.execute("""CREATE TABLE IF NOT EXISTS questions (id BIGSERIAL PRIMARY KEY, qtype TEXT, stem TEXT, options TEXT, answer TEXT, explanation TEXT, batch TEXT DEFAULT '', tag TEXT DEFAULT '', stem_key TEXT DEFAULT '')""")
            c.execute("""CREATE TABLE IF NOT EXISTS records (id BIGSERIAL PRIMARY KEY, question_id BIGINT, user_answer TEXT, is_correct INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS progress (question_id BIGINT PRIMARY KEY, times_seen INTEGER DEFAULT 0, times_correct INTEGER DEFAULT 0, consecutive_correct INTEGER DEFAULT 0, in_wrong_book INTEGER DEFAULT 0)""")
            c.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS batch TEXT DEFAULT ''")
            c.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS stem_key TEXT DEFAULT ''")
            c.execute("SELECT id, stem FROM questions WHERE stem_key = '' OR stem_key IS NULL")
            for qid, stem in c.fetchall():
                c.execute("UPDATE questions SET stem_key=%s WHERE id=%s",(normalize_stem_for_dedup(stem),qid))
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_stem_key ON questions(stem_key) WHERE stem_key != ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_records_qid ON records(question_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_progress_wrong ON progress(in_wrong_book)")
        return True,""
    except Exception as e:
        return False, str(e)


db_init_ok, db_init_err = init_db_once()


def load_all_to_cache():
    with db_cursor() as (conn,c):
        c.execute("SELECT id, qtype, stem, options, answer, explanation, batch FROM questions")
        qrows = c.fetchall()
        c.execute("SELECT question_id, times_seen, times_correct, consecutive_correct, in_wrong_book FROM progress")
        prows = c.fetchall()
        c.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END), 0) FROM records")
        rrow = c.fetchone()
    qs = {}
    for r in qrows:
        qs[r[0]] = {"id":r[0],"qtype":r[1],"stem":r[2],"options":json.loads(r[3]) if r[3] else [],"answer":r[4],"explanation":r[5],"batch":r[6] or ""}
    ps = {r[0]:[r[1],r[2],r[3],r[4]] for r in prows}
    st.session_state.questions_cache = qs
    st.session_state.progress_cache = ps
    st.session_state.total_answers = rrow[0] or 0
    st.session_state.total_correct = rrow[1] or 0


def ensure_cache():
    if "questions_cache" not in st.session_state:
        try: load_all_to_cache()
        except Exception as e:
            st.session_state.db_ok = False
            st.session_state.db_error = str(e)
            st.session_state.last_error_trace = traceback.format_exc()


def invalidate_cache():
    for k in ("questions_cache","progress_cache","total_answers","total_correct"):
        if k in st.session_state: del st.session_state[k]
    load_all_to_cache()


def _async_write_answer(qid, ua, ok):
    def worker():
        for attempt in range(3):
            try:
                with db_cursor() as (conn,c):
                    c.execute("SELECT times_seen, times_correct, consecutive_correct, in_wrong_book FROM progress WHERE question_id=%s",(qid,))
                    row = c.fetchone()
                    if row is None:
                        c.execute("INSERT INTO progress (question_id) VALUES (%s) ON CONFLICT (question_id) DO NOTHING",(qid,))
                        row = (0,0,0,0)
                    seen, correct, consec, wrong = row
                    seen += 1
                    if ok:
                        correct += 1; consec += 1
                        if consec >= 2: wrong = 0
                    else:
                        consec = 0; wrong = 1
                    c.execute("UPDATE progress SET times_seen=%s, times_correct=%s, consecutive_correct=%s, in_wrong_book=%s WHERE question_id=%s",(seen,correct,consec,wrong,qid))
                    c.execute("INSERT INTO records (question_id, user_answer, is_correct) VALUES (%s, %s, %s)",(qid,ua,1 if ok else 0))
                return
            except Exception as e:
                print(f"write {attempt+1} failed: {e}"); time.sleep(1)
    threading.Thread(target=worker, daemon=True).start()


def get_question_by_id_cached(qid): return st.session_state.questions_cache.get(qid)


def get_next_unseen_cached():
    qs = st.session_state.questions_cache; ps = st.session_state.progress_cache
    for qid in sorted([q for q,v in qs.items() if v["qtype"] in ("单选","多选","判断")]):
        p = ps.get(qid)
        if not p or p[0] == 0: return qid
    return None


def get_next_wrong_cached():
    qs = st.session_state.questions_cache; ps = st.session_state.progress_cache
    c = []
    for qid,v in qs.items():
        if v["qtype"] not in ("单选","多选","判断"): continue
        p = ps.get(qid)
        if p and p[3] == 1: c.append((p[0],qid))
    if not c: return None
    c.sort(); return c[0][1]


def get_next_random_cached():
    qs = st.session_state.questions_cache
    ids = [q for q,v in qs.items() if v["qtype"] in ("单选","多选","判断")]
    return random.choice(ids) if ids else None


def get_next_mixed_cached():
    qs = st.session_state.questions_cache; ps = st.session_state.progress_cache
    un = []
    for qid,v in qs.items():
        if v["qtype"] not in ("单选","多选","判断"): continue
        p = ps.get(qid)
        if not p or p[0] == 0: un.append(qid)
    return random.choice(un) if un else None


def get_stats_cached():
    qs = st.session_state.questions_cache; ps = st.session_state.progress_cache
    total = seen = wrong = 0
    for qid,v in qs.items():
        if v["qtype"] in ("单选","多选","判断"):
            total += 1
            p = ps.get(qid)
            if p and p[0] > 0: seen += 1
            if p and p[3] == 1: wrong += 1
    ans = st.session_state.get("total_answers",0)
    cor = st.session_state.get("total_correct",0)
    return {"total":total,"seen":seen,"wrong":wrong,"answers":ans,"rate":round(cor/ans*100,1) if ans else 0}


def get_count_by_type_cached():
    counts = {}
    for v in st.session_state.questions_cache.values():
        counts[v["qtype"]] = counts.get(v["qtype"],0) + 1
    return counts


def count_bad_options_cached():
    bad = 0
    for v in st.session_state.questions_cache.values():
        if len(v["options"]) <= 1 and v["stem"]:
            n = len(re.findall(r'(?:^|[\s。，,;；:：）)\]】])[A-EＡ-Ｅ]\s*[\.、．)）:：。，,]',v["stem"]))
            if n >= 2: bad += 1
    return bad


SECTION_HEADER = re.compile(r'(?:^|\n)\s*(?:[一二三四五六七八九十]+、\s*([^\n]+?)|((?:单选|多选|判断|填空|简答|分析|综合应用|综合|论述|讨论|案例分析|案例)\s*题?))\s*(?:[（(][^）)]*[）)])?\s*(?:\n|$)')


def _section_to_qtype(h):
    if not h: return None
    if "多" in h and "选" in h: return "多选"
    if "单" in h and "选" in h: return "单选"
    if "判断" in h or "对错" in h: return "判断"
    if any(k in h for k in ["填空","简答","案例","论述","名词解释","分析","综合应用","综合题","问答","讨论"]): return "简答/案例"
    return None


def _split_sections(text):
    res = []; last_end = 0; last_h = None
    for m in SECTION_HEADER.finditer(text):
        c = text[last_end:m.start()].strip()
        if c or last_h is not None: res.append((last_h,c))
        last_h = (m.group(1) or m.group(2) or "").strip()
        last_end = m.end()
    res.append((last_h, text[last_end:].strip()))
    return res


def _split_answer_text(body, qtype):
    m = re.search(r'[\[【]\s*解\s*析\s*[\]】]|\n?\s*解\s*析\s*[：:]',body)
    if m: ans_p = body[:m.start()].strip(); exp = body[m.end():].strip()
    else: ans_p = body; exp = ""
    if qtype == "判断":
        s = ans_p.strip("【】[]()（） \n\r\t")
        if any(c in s for c in ["×","✗","错"]) or s in ["F","f"]: return "错误", exp
        if any(c in s for c in ["√","✓","对","正确"]) or s in ["T","t"]: return "正确", exp
        return "", exp
    if qtype == "简答/案例": return ans_p.strip("【】[]()（） \n\r\t"), exp
    letters = re.findall(r'[A-Ea-e]',ans_p)
    return "".join(sorted(set(L.upper() for L in letters))), exp


def _parse_inline_qa(text):
    qs = []
    parts = re.split(r'(?<![0-9])(\d{1,3})\s*[\.、]\s*',text)
    blocks = []
    if parts and parts[0].strip(): blocks.append((None,parts[0].strip()))
    for i in range(1,len(parts),2):
        blocks.append((parts[i], (parts[i+1] if i+1<len(parts) else "").strip()))
    for num,content in blocks:
        if not content: continue
        am = re.search(r'(?:参考)?答案\s*[\]】\)）]?\s*[:：]?\s*(.+?)(?=【\s*解析\s*】|解析\s*[：:]|\n\s*\d{1,3}\s*[\.、]|$)',content,re.S)
        if not am: continue
        ans_text = am.group(1).strip()
        ca = content[am.end():].strip()
        cb = content[:am.start()].strip()
        exp = ""
        em = re.search(r'【\s*解析\s*】\s*|解析\s*[：:]\s*',ca)
        if em: exp = ca[em.end():].strip()
        stem, opts = parse_options_from_content(cb)
        stem = clean_stem(stem).strip("【】[]()（） \n\r\t")
        opts = [clean_stem(o).strip("【】[]()（） \n\r\t") for o in opts]
        stem = re.sub(r'[一二三四五六七八九十]+、[^\n]{2,40}$','',stem).strip()
        if not stem: continue
        au = ans_text.strip().upper().strip("【】[]()（） \n\r\t")
        letters = re.findall(r'[A-E]',au)
        if opts and letters:
            qtype = "多选" if len(letters)>1 else "单选"
            answer = "".join(sorted(set(letters)))
        elif not opts:
            if any(c in ans_text for c in ["×","✗","错"]) or au=="F": qtype="判断"; answer="错误"
            elif any(c in ans_text for c in ["√","✓","对","正确"]) or au=="T": qtype="判断"; answer="正确"
            else: qtype="简答/案例"; answer=ans_text
        else: continue
        qs.append({"qtype":qtype,"stem":stem,"options":opts,"answer":answer,"explanation":exp})
    return qs


def _parse_answer_section(text):
    res = {}
    for h,c in _split_sections(text):
        qtype = _section_to_qtype(h) or "单选"
        items = re.split(r'(?<![0-9])(\d{1,3})\s*[\.、]\s*',c)
        for j in range(1,len(items),2):
            try: num = int(items[j])
            except: continue
            body = items[j+1].strip() if j+1 < len(items) else ""
            if not body: continue
            res[(qtype,num)] = _split_answer_text(body,qtype)
    return res


def _parse_question_section_with_answers(text, amap):
    qs = []; fb = "单选"
    for h,c in _split_sections(text):
        qt = _section_to_qtype(h)
        if qt: fb = qt
        else: qt = fb
        items = re.split(r'(?<![0-9])(\d{1,3})\s*[\.、]\s*',c)
        for j in range(1,len(items),2):
            try: num = int(items[j])
            except: continue
            body = items[j+1].strip() if j+1 < len(items) else ""
            if not body: continue
            stem, opts = parse_options_from_content(body)
            stem = clean_stem(stem).strip("【】[]()（） \n\r\t")
            opts = [clean_stem(o).strip("【】[]()（） \n\r\t") for o in opts]
            stem = re.sub(r'[一二三四五六七八九十]+、[^\n]{2,40}$','',stem).strip()
            if not stem: continue
            ans, exp = "", ""
            if (qt,num) in amap: ans,exp = amap[(qt,num)]
            else:
                for alt in ["单选","多选","判断","简答/案例"]:
                    if (alt,num) in amap: ans,exp = amap[(alt,num)]; break
            ft = qt
            if ft=="单选" and opts and len((ans or "").upper())>1 and all(c in "ABCDE" for c in (ans or "").upper()): ft="多选"
            if ft=="判断" and ans and ans not in ["正确","错误"]: ft="简答/案例"
            qs.append({"qtype":ft,"stem":stem,"options":opts,"answer":ans,"explanation":exp})
    return qs


def _parse_no_number(text):
    qs = []
    ap = re.compile(r'(?:^|\n)\s*(?:参考)?答案\s*[\]】\)）]?\s*[:：]?\s*([A-Ea-e√×✓✗正确错误对错TFtf]+)',re.MULTILINE)
    ms = list(ap.finditer(text))
    if len(ms) < 2: return []
    pe = 0
    for i,m in enumerate(ms):
        block = text[pe:m.start()].strip()
        ar = m.group(1).strip()
        ns = ms[i+1].start() if i+1<len(ms) else len(text)
        aa = text[m.end():ns].strip()
        em = re.search(r'解析\s*[：:]\s*',aa)
        exp = aa[em.end():].strip() if em else ""
        pe = m.end()
        if not block: continue
        stem, opts = parse_options_from_content(block)
        stem = clean_stem(stem).strip("【】[]()（） \n\r\t")
        opts = [clean_stem(o).strip("【】[]()（） \n\r\t") for o in opts]
        if not stem: continue
        s = ar.strip("【】[]()（） \n\r\t")
        letters = re.findall(r'[A-E]',s.upper())
        if opts and letters:
            qt = "多选" if len(letters)>1 else "单选"
            answer = "".join(sorted(set(letters)))
        elif not opts:
            if any(c in s for c in ["×","✗","错","F"]): qt="判断"; answer="错误"
            elif any(c in s for c in ["√","✓","对","正确","T"]): qt="判断"; answer="正确"
            else: qt="简答/案例"; answer=s
        else: continue
        qs.append({"qtype":qt,"stem":stem,"options":opts,"answer":answer,"explanation":exp})
    return qs


def parse_text(text):
    text = clean_parse_text(text)
    iq = _parse_inline_qa(text); ia = sum(1 for q in iq if q["answer"])
    sq = []
    m = re.search(r'(?:^|\n)\s*(?:标准)?\s*参[考考]?\s*答\s*案(?:\s*与\s*解\s*析|\s*及\s*解\s*析|\s*及\s*答\s*案)?\s*(?:\n|$)',text)
    if m:
        amap = _parse_answer_section(text[m.end():])
        if amap: sq = _parse_question_section_with_answers(text[:m.start()], amap)
    sa = sum(1 for q in sq if q["answer"])
    nq = _parse_no_number(text); na = sum(1 for q in nq if q["answer"])
    return max([(iq,ia),(sq,sa),(nq,na)],key=lambda x:x[1])[0]


def parse_excel(file):
    df = pd.read_excel(file)
    cols = {str(c).lower().strip():c for c in df.columns}
    def fc(*keys):
        for k in keys:
            if k.lower() in cols: return cols[k.lower()]
        return None
    cs=fc("题干","题目","问题","stem"); ct=fc("题型","类型","type")
    ca=fc("答案","正确答案","参考答案","answer"); ce=fc("解析","explanation")
    ocs = []
    for L in ["A","B","C","D","E"]:
        for k in [f"选项{L}",f"option{L}",L,L.lower()]:
            if k.lower() in cols: ocs.append(cols[k.lower()]); break
    qs = []
    for _,row in df.iterrows():
        stem = str(row[cs]).strip() if cs and pd.notna(row[cs]) else ""
        if not stem: continue
        stem = clean_stem(stem); opts = []
        for oc in ocs:
            if pd.notna(row[oc]): opts.append(clean_stem(str(row[oc]).strip()))
        if not opts:
            stem,opts = parse_options_from_content(stem)
            stem = clean_stem(stem); opts = [clean_stem(o) for o in opts]
        ans = str(row[ca]).strip() if ca and pd.notna(row[ca]) else ""
        exp = str(row[ce]).strip() if ce and pd.notna(row[ce]) else ""
        qt = normalize_type(row[ct] if ct and pd.notna(row[ct]) else "",stem,opts)
        if qt=="单选" and opts and len(ans)>1 and all(c in "ABCDE" for c in ans.upper()): qt="多选"
        if qt=="判断" and ans and ans not in ["正确","错误","对","错","√","×","T","F"]: qt="简答/案例"
        qs.append({"qtype":qt,"stem":stem,"options":opts,"answer":ans,"explanation":exp})
    return qs


def parse_word(file):
    from docx import Document
    doc = Document(file)
    return parse_text("\n".join(p.text for p in doc.paragraphs if p.text.strip()))


def parse_pdf(file):
    import pdfplumber
    tp = []
    with pdfplumber.open(file) as pdf:
        for pg in pdf.pages:
            t = pg.extract_text()
            if t: tp.append(t)
    return parse_text("\n".join(tp))


def parse_file(uf):
    n = uf.name.lower()
    if n.endswith(".xlsx") or n.endswith(".xls"): return parse_excel(uf)
    if n.endswith(".docx"): return parse_word(uf)
    if n.endswith(".pdf"): return parse_pdf(uf)
    if n.endswith(".txt"): return parse_text(uf.read().decode("utf-8",errors="ignore"))
    return []


def normalize_type(t, stem="", opts=None):
    t = str(t).strip(); opts = opts or []
    if "多" in t and "选" in t: return "多选"
    if "单" in t and "选" in t: return "单选"
    if "判" in t or "对错" in t: return "判断"
    if any(k in t for k in ["填空","简答","案例","论述","名词解释","分析","综合应用","问答","讨论"]): return "简答/案例"
    if any(k in stem for k in ["简答","案例分析","论述","名词解释"]): return "简答/案例"
    if not opts: return "判断"
    return "单选"


def check_duplicates(qs):
    ex = {normalize_stem_for_dedup(v["stem"]) for v in st.session_state.questions_cache.values()}
    seen = set(); flags = []; cnt = 0
    for q in qs:
        k = normalize_stem_for_dedup(q["stem"])
        d = (k in ex) or (k in seen)
        flags.append(d)
        if d: cnt += 1
        else: seen.add(k)
    return flags, cnt


def insert_questions(qs, bn):
    ins = 0; sk = 0
    with db_cursor() as (conn,c):
        c.execute("SELECT stem_key FROM questions WHERE stem_key != ''")
        ex = set(r[0] for r in c.fetchall()); seen = set()
        for q in qs:
            if not q["stem"]: continue
            k = normalize_stem_for_dedup(q["stem"])
            if k in ex or k in seen: sk += 1; continue
            seen.add(k)
            try:
                c.execute("""INSERT INTO questions (qtype, stem, options, answer, explanation, batch, stem_key) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (q["qtype"],q["stem"],json.dumps(q["options"],ensure_ascii=False),q["answer"],q["explanation"],bn,k))
                r = c.fetchone()
                if r: c.execute("INSERT INTO progress (question_id) VALUES (%s) ON CONFLICT (question_id) DO NOTHING",(r[0],))
                ins += 1
            except psycopg2.errors.UniqueViolation:
                conn.rollback(); sk += 1; continue
    return ins, sk


def list_batches_from_cache():
    b = {}
    for v in st.session_state.questions_cache.values():
        bb = v.get("batch","")
        if bb: b[bb] = b.get(bb,0) + 1
    return sorted(b.items(), key=lambda x: x[0], reverse=True)


def delete_batch(bn):
    with db_cursor() as (conn,c):
        c.execute("SELECT id FROM questions WHERE batch = %s",(bn,))
        qids = [r[0] for r in c.fetchall()]
        if qids:
            c.execute("DELETE FROM records WHERE question_id = ANY(%s)",(qids,))
            c.execute("DELETE FROM progress WHERE question_id = ANY(%s)",(qids,))
            c.execute("DELETE FROM questions WHERE id = ANY(%s)",(qids,))
    return len(qids)


def reset_answer_records():
    with db_cursor() as (conn,c):
        c.execute("DELETE FROM records"); c.execute("DELETE FROM progress")
        c.execute("INSERT INTO progress (question_id) SELECT id FROM questions ON CONFLICT (question_id) DO NOTHING")


def clear_all():
    with db_cursor() as (conn,c):
        c.execute("DELETE FROM questions"); c.execute("DELETE FROM records"); c.execute("DELETE FROM progress")


def extract_answer(s):
    if s is None: return ""
    s = str(s).strip()
    if not s: return ""
    if s in ["正确","错误","对","错","√","×","T","F","t","f","✓","✗"]:
        return "正确" if s in ["对","√","T","t","✓"] else "错误"
    return "".join(sorted(set(L.upper() for L in re.findall(r'[A-Ea-e]',s))))


def judge(qt, ca, ua):
    ca = extract_answer(ca); ua = extract_answer(ua)
    if not ca: return None
    if qt == "多选": return ca == ua and len(ca) > 0
    return ca == ua


def format_answer_with_text(qt, ans, opts):
    if not ans: return "（题库未提供）"
    if qt == "判断": return ans
    if not opts: return ans
    letters = re.findall(r'[A-Ea-e]',str(ans).upper())
    if not letters: return ans
    letters = sorted(set(L.upper() for L in letters))
    parts = []
    for L in letters:
        i = ord(L) - ord('A')
        if 0 <= i < len(opts): parts.append(f"{L}. {opts[i]}")
        else: parts.append(L)
    return "　".join(parts)


def _do_judge(q, ua):
    r = judge(q["qtype"], q["answer"], ua)
    st.session_state.last_user_answer = ua
    if r is None: st.session_state.last_correct = None
    else:
        st.session_state.last_correct = r
        qid = q["id"]; ps = st.session_state.progress_cache
        p = list(ps.get(qid,[0,0,0,0]))
        p[0] += 1
        if r:
            p[1] += 1; p[2] += 1
            if p[2] >= 2: p[3] = 0
        else: p[2] = 0; p[3] = 1
        ps[qid] = p
        st.session_state.total_answers = st.session_state.get("total_answers",0) + 1
        if r: st.session_state.total_correct = st.session_state.get("total_correct",0) + 1
        _async_write_answer(qid, ua, r)
    st.session_state.answered = True
    st.rerun()


FONT_SIZES = {"标准":18,"大":22,"很大":26,"特大":30}


def inject_css():
    fs = st.session_state.font_size
    st.markdown(f"""<style>
    html,body{{overflow-y:auto!important;height:auto!important;}}
    .stApp,[data-testid="stAppViewContainer"],.stMain,section.main{{overflow-y:auto!important;}}
    [data-testid="stSidebar"]>div:first-child{{overflow-y:auto!important;padding-bottom:3rem!important;}}
    .stMainBlockContainer,.block-container{{max-width:1100px!important;margin:0 auto!important;padding:1rem 1rem 10rem 1rem!important;}}
    html,body,[class*="css"]{{font-size:{fs}px!important;}}
    [data-testid="stMarkdownContainer"] p,[data-testid="stMarkdownContainer"] li,[data-testid="stMarkdownContainer"] span{{font-size:{fs}px!important;line-height:1.7!important;word-break:break-word!important;}}
    .stem-text{{font-size:{fs+3}px!important;line-height:1.7!important;font-weight:600!important;margin:0.6em 0 1em 0!important;color:#111!important;}}
    .answer-line{{font-size:{fs+1}px!important;line-height:1.9!important;padding:0.4em 0!important;}}
    .answer-correct{{color:#0a7a3a!important;font-weight:700!important;}}
    .answer-wrong{{color:#c0392b!important;font-weight:700!important;}}
    h1{{font-size:{fs+10}px!important;}}h2{{font-size:{fs+6}px!important;}}h3{{font-size:{fs+3}px!important;}}
    .stButton>button{{font-size:{fs}px!important;padding:0.8em 1em!important;min-height:3.4em!important;border-radius:10px!important;font-weight:600!important;text-align:left!important;white-space:normal!important;line-height:1.5!important;width:100%!important;}}
    [data-testid="stMetricValue"]{{font-size:{fs+6}px!important;}}
    [data-testid="stMetricLabel"]{{font-size:{max(fs-2,14)}px!important;}}
    [data-testid="stSidebar"] label,[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p{{font-size:{fs}px!important;}}
    [data-testid="stRadio"] label,[data-testid="stCheckbox"] label{{font-size:{fs+1}px!important;}}
    .dup-tag{{display:inline-block;background:#f39c12;color:#fff;font-size:{max(fs-3,13)}px;padding:2px 8px;border-radius:6px;margin-left:6px;}}
    .opt-count{{display:inline-block;background:#2e86de;color:#fff;font-size:{max(fs-3,13)}px;padding:2px 8px;border-radius:6px;margin-left:6px;}}
    .opt-count-bad{{display:inline-block;background:#e74c3c;color:#fff;font-size:{max(fs-3,13)}px;padding:2px 8px;border-radius:6px;margin-left:6px;}}
    @media (max-width:768px){{
        .stMainBlockContainer,.block-container{{padding:0.6rem 0.6rem 8rem 0.6rem!important;max-width:100%!important;}}
        html,body,[class*="css"]{{font-size:{max(fs-2,16)}px!important;}}
        .stem-text{{font-size:{max(fs+1,20)}px!important;}}
        .stButton>button{{font-size:{max(fs-1,17)}px!important;min-height:3.6em!important;}}
        [data-testid="column"]{{min-width:calc(50% - 0.5rem)!important;flex:1 1 calc(50% - 0.5rem)!important;}}
    }}</style>""", unsafe_allow_html=True)


ensure_cache()
inject_css()
st.title("海口边检辅警刷题系统")

with st.sidebar:
    st.header("菜单")
    page = st.radio("选择页面", ["上传题库","管理题库","开始刷题","简答/案例分析","错题本","统计","系统自检"])
    st.divider()
    if st.session_state.db_ok is False: st.error("🔴 数据库异常")
    elif st.session_state.db_ok is True: st.success("🟢 数据库正常")
    cl = [k for k,v in FONT_SIZES.items() if v == st.session_state.font_size]
    ci = list(FONT_SIZES.keys()).index(cl[0]) if cl else 1
    sl = st.radio("字体大小",list(FONT_SIZES.keys()),index=ci,horizontal=True)
    st.session_state.font_size = FONT_SIZES[sl]


if page == "系统自检":
    st.subheader("🔧 系统自检")
    c1,c2 = st.columns(2)
    with c1:
        if st.button("🔍 检测数据库连接", use_container_width=True):
            if check_database(): st.success("数据库连接正常")
            else: st.error(f"失败：{st.session_state.db_error}")
    with c2:
        if st.button("📋 检测表结构", use_container_width=True):
            ok,msg = check_tables()
            if ok: st.success("三张表均存在")
            else: st.error(msg)
    st.divider()
    st.write(f"- 数据库：{'正常' if st.session_state.db_ok else '异常' if st.session_state.db_ok is False else '未检测'}")
    st.write(f"- 缓存：{'已加载' if 'questions_cache' in st.session_state else '未加载'}")
    st.write(f"- 题库：{len(st.session_state.get('questions_cache',{}))} 道")
    if st.session_state.db_ok is False:
        st.divider(); st.error("检测到错误，以下是可复制代码：")
        st.code(get_error_snippet(), language="python")
        st.info("👆 复制上面代码，发给AI帮你排查。")


elif page == "上传题库":
    st.subheader("上传带答案的题库文档")
    st.write("支持：Excel / Word / PDF / TXT")
    st.info("📌 一次可选多个文件，自动合并、去重。")
    if count_bad_options_cached() > 0:
        st.error(f"🚨 检测到 {count_bad_options_cached()} 道题选项疑似被合并。请先到「管理题库」清空。")
    with st.expander("格式说明", expanded=False):
        st.markdown("""
**支持格式：**
1. 内联式：`【答案】D 【解析】xxx`、`答案：D`、`答案 D`
2. 分离式：题目在前，`参考答案与解析`区在后
3. 无题号：题干+选项+`答案：X`循环
4. 选项同行或分行都支持
5. 填空/简答/案例/论述/综合→归到「简答/案例分析」
        """)
    ufs = st.file_uploader("选择文件（可 Ctrl 或 Shift 多选）", type=["xlsx","xls","docx","pdf","txt"], accept_multiple_files=True)
    if ufs:
        allq = []; fstats = []; errs = []
        with st.spinner(f"解析 {len(ufs)} 个文件..."):
            for uf in ufs:
                try:
                    qs = parse_file(uf); fstats.append((uf.name,len(qs))); allq.extend(qs)
                except Exception as e: errs.append((uf.name,str(e)))
        for fn,e in errs: st.error(f"❌ {fn}：{e}")
        if allq:
            st.subheader("各文件解析结果")
            for fn,cnt in fstats: st.write(f"- **{fn}**：{cnt} 道")
            tp = len(allq); st.success(f"共解析 **{tp}** 道")
            counts = {}; bad = 0
            for q in allq:
                counts[q["qtype"]] = counts.get(q["qtype"],0) + 1
                if q["qtype"] in ("单选","多选") and len(q["options"]) < 2: bad += 1
            st.write("题型：" + "　".join([f"{k}：{v}" for k,v in counts.items()]))
            if bad: st.error(f"⚠️ {bad} 道选项没拆开")
            df,dc = check_duplicates(allq)
            if dc: st.warning(f"⚠️ {dc} 道重复，将跳过")
            dflt = f"批量_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            bn = st.text_input("批次名称", value=dflt)
            c1,c2 = st.columns(2)
            with c1:
                if st.button("✅ 确认导入", type="primary", use_container_width=True):
                    if not bn.strip(): st.error("批次名不能空")
                    else:
                        with st.spinner("导入中..."):
                            n,sk = insert_questions(allq, bn.strip()); invalidate_cache()
                        st.success(f"导入 {n} 道，跳过 {sk} 道重复" if sk else f"导入 {n} 道")
                        st.balloons()
            with c2:
                if st.button("🗑️ 放弃", use_container_width=True): st.rerun()
            with st.expander(f"查看全部 {tp} 道", expanded=False):
                for i,q in enumerate(allq,1):
                    dt = '<span class="dup-tag">重复</span>' if (i-1 < len(df) and df[i-1]) else ''
                    no = len(q["options"])
                    tag = f'<span class="opt-count-bad">选项：{no}（未拆）</span>' if (q["qtype"] in ("单选","多选") and no < 2) else f'<span class="opt-count">选项：{no}</span>'
                    st.markdown(f"**第 {i} 题【{q['qtype']}】**{dt}{tag}", unsafe_allow_html=True)
                    st.markdown(f'<div class="stem-text">{html.escape(q["stem"])}</div>', unsafe_allow_html=True)
                    for j,o in enumerate(q["options"]): st.write(f"　{chr(65+j)}. {o}")
                    st.write(f"　**答案：** {q['answer'] or '（未识别）'}")
                    if q["explanation"]: st.write(f"　**解析：** {q['explanation']}")
                    st.divider()
        else: st.warning("没有解析出题目。")


elif page == "管理题库":
    st.subheader("管理已导入的题库")
    bts = list_batches_from_cache()
    if not bts: st.info("当前没有已导入的批次。")
    else:
        st.write(f"共 {len(bts)} 个批次：")
        for bn,cnt in bts:
            c1,c2 = st.columns([3,1])
            with c1: st.write(f"**{bn}**"); st.caption(f"{cnt} 道")
            with c2:
                if st.button("删除", key=f"d_{bn}", type="secondary"):
                    with st.spinner("删除中..."): n = delete_batch(bn); invalidate_cache()
                    st.success(f"已删「{bn}」，{n} 道"); st.rerun()
            st.divider()
    st.divider(); st.subheader("当前题库汇总")
    counts = get_count_by_type_cached()
    if not counts: st.write("题库为空。")
    else:
        for k,v in counts.items(): st.write(f"- {k}：{v} 道")
        st.write(f"**合计：{sum(counts.values())} 道**")
        if count_bad_options_cached(): st.error(f"🚨 {count_bad_options_cached()} 道题选项疑似未拆开")
    st.divider(); st.subheader("重置答题记录（保留题库）")
    if st.button("🔄 重置答题记录", type="secondary"):
        with st.spinner("重置中..."): reset_answer_records(); invalidate_cache()
        st.success("已重置")
    st.divider(); st.subheader("危险操作")
    if st.button("🚨 清空全部题库和记录", type="secondary"):
        with st.spinner("清空中..."): clear_all(); invalidate_cache()
        st.success("已清空"); st.rerun()


elif page == "开始刷题":
    stats = get_stats_cached()
    if stats["total"] == 0: st.warning("选择题/判断题题库为空，请先导入。"); st.stop()
    mode = st.radio("刷题模式", ["顺序刷未做题","错题复习","随机抽题","实战混合"], horizontal=True)
    if st.session_state.current_qid is None:
        if mode == "顺序刷未做题":
            qid = get_next_unseen_cached()
            st.session_state.all_done_choice = mode if qid is None else None
        elif mode == "错题复习":
            qid = get_next_wrong_cached()
            st.session_state.all_done_choice = mode if qid is None else None
        elif mode == "实战混合":
            qid = get_next_mixed_cached()
            st.session_state.all_done_choice = mode if qid is None else None
        else:
            qid = get_next_random_cached(); st.session_state.all_done_choice = None
        if qid is not None:
            st.session_state.current_qid = qid; st.session_state.answered = False
            st.session_state.last_correct = None; st.session_state.last_user_answer = ""
    if st.session_state.all_done_choice is not None:
        st.success("🎉 恭喜！你已经把这批题全部做完了。")
        st.write(f"总题数：{stats['total']}　已做：{stats['seen']}　错题：{stats['wrong']}　正确率：{stats['rate']}%")
        st.divider(); st.subheader("是否重新开始刷题？")
        st.write("重新开始会清空答题记录和错题本，题库不会删除。")
        c1,c2 = st.columns(2)
        with c1:
            if st.button("🔄 是，重新开始", type="primary", use_container_width=True):
                with st.spinner("清空记录中..."): reset_answer_records(); invalidate_cache()
                st.session_state.current_qid = None; st.session_state.answered = False
                st.session_state.last_correct = None; st.session_state.last_user_answer = ""
                st.session_state.all_done_choice = None; st.rerun()
        with c2:
            if st.button("否", use_container_width=True):
                st.session_state.all_done_choice = None; st.rerun()
        st.stop()
    q = get_question_by_id_cached(st.session_state.current_qid)
    if q is None: st.session_state.current_qid = None; st.rerun()
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("总题数",stats["total"]); c2.metric("已做",stats["seen"])
    c3.metric("错题",stats["wrong"]); c4.metric("正确率",f"{stats['rate']}%")
    st.divider()
    st.markdown(f"**【{q['qtype']}】第 {q['id']} 题**")
    st.markdown(f'<div class="stem-text">{html.escape(q["stem"])}</div>', unsafe_allow_html=True)
    opts = q["options"]
    if q["qtype"] in ("单选","多选") and len(opts) < 2:
        st.error(f"⚠️ 这道题选项没拆开（只有 {len(opts)} 个）"); st.error("请到「管理题库」清空后重新上传"); st.stop()
    if not st.session_state.answered:
        if q["qtype"] == "判断":
            cy,cn = st.columns(2)
            if cy.button("✔ 正确", key=f"o_{q['id']}_T", use_container_width=True): _do_judge(q,"正确")
            if cn.button("✘ 错误", key=f"o_{q['id']}_F", use_container_width=True): _do_judge(q,"错误")
        elif q["qtype"] == "多选":
            st.write("（多选，漏选、多选、错选都算错。勾选后点「确认选择」）")
            sel = []
            for i,o in enumerate(opts):
                L = chr(65+i)
                if st.checkbox(f"{L}. {o}", key=f"cb_{q['id']}_{L}"): sel.append(L)
            if st.button("确认选择", type="primary", key=f"cf_{q['id']}", use_container_width=True):
                if not sel: st.warning("请至少勾选一个")
                else: _do_judge(q, "".join(sel))
        else:
            for i,o in enumerate(opts):
                L = chr(65+i)
                if st.button(f"{L}. {o}", key=f"o_{q['id']}_{L}", use_container_width=True): _do_judge(q,L)
    if st.session_state.answered:
        st.divider(); cor = st.session_state.last_correct
        if cor is True: st.success("✅ 答对了")
        elif cor is False: st.error("❌ 答错了")
        else: st.warning("⚠️ 待核对（题库无明确答案，不计分）")
        ua = format_answer_with_text(q["qtype"], st.session_state.last_user_answer, q["options"])
        ca = format_answer_with_text(q["qtype"], q["answer"], q["options"])
        st.markdown(f'<div class="answer-line">你的答案：<span class="answer-wrong">{html.escape(ua)}</span></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="answer-line">正确答案：<span class="answer-correct">{html.escape(ca)}</span></div>', unsafe_allow_html=True)
        st.write(f"**解析：** {q['explanation']}" if q["explanation"] else "**解析：**（题库未提供解析）")
        st.divider()
        if st.button("下一题 →", type="primary", use_container_width=True):
            st.session_state.current_qid = None; st.session_state.answered = False
            st.session_state.last_correct = None; st.session_state.last_user_answer = ""; st.rerun()


elif page == "简答/案例分析":
    st.subheader("简答题 / 填空 / 案例分析 / 综合题 / 论述题")
    st.write("这类题不判分，只看题和参考答案。")
    cases = [q for q in st.session_state.questions_cache.values() if q["qtype"] == "简答/案例"]
    cases.sort(key=lambda x: x["id"])
    if not cases: st.write("题库里还没有简答/案例题。")
    else:
        if st.session_state.case_idx >= len(cases): st.session_state.case_idx = 0
        q = cases[st.session_state.case_idx]
        st.markdown(f"**第 {st.session_state.case_idx+1} / {len(cases)} 题**")
        st.markdown(f'<div class="stem-text">{html.escape(q["stem"])}</div>', unsafe_allow_html=True)
        if st.button("查看参考答案", type="primary"): st.session_state.show_case_answer = True
        if st.session_state.show_case_answer:
            st.success(f"**参考答案：**\n\n{q['answer'] or '（文档未提供答案）'}")
            if q["explanation"]: st.info(f"**解析：**\n\n{q['explanation']}")
        st.divider(); c1,c2 = st.columns(2)
        if c1.button("← 上一题", use_container_width=True):
            st.session_state.case_idx = max(0,st.session_state.case_idx-1)
            st.session_state.show_case_answer = False; st.rerun()
        if c2.button("下一题 →", use_container_width=True):
            st.session_state.case_idx = min(len(cases)-1,st.session_state.case_idx+1)
            st.session_state.show_case_answer = False; st.rerun()


elif page == "错题本":
    st.subheader("错题本")
    qs = st.session_state.questions_cache; ps = st.session_state.progress_cache
    rows = []
    for qid,q in qs.items():
        if q["qtype"] not in ("单选","多选","判断"): continue
        p = ps.get(qid)
        if p and p[3] == 1:
            rows.append({"id":qid,"题型":q["qtype"],"题干":q["stem"],"答案":q["answer"],"做题次数":p[0],"对":p[1]})
    if not rows: st.write("错题本是空的。")
    else:
        df = pd.DataFrame(rows); st.write(f"共 {len(df)} 道错题。")
        st.dataframe(df, use_container_width=True)
        st.download_button("导出错题本 CSV", df.to_csv(index=False).encode("utf-8-sig"), "错题本.csv", "text/csv")


elif page == "统计":
    st.subheader("答题统计")
    stats = get_stats_cached()
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("选择/判断总数",stats["total"]); c2.metric("已做",stats["seen"])
    c3.metric("错题",stats["wrong"]); c4.metric("总答题次数",stats["answers"])
    c5.metric("正确率",f"{stats['rate']}%")
    st.divider(); st.subheader("题库构成")
    counts = get_count_by_type_cached()
    if counts:
        for k,v in counts.items(): st.write(f"- {k}：{v} 道")
    else: st.write("题库为空。")
    st.divider()
    if st.button("🔄 重置答题记录（保留题库）", type="secondary", use_container_width=True):
        with st.spinner("重置中..."): reset_answer_records(); invalidate_cache()
        st.success("已重置")
