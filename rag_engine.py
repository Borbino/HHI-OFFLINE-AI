import os
import shutil
import sqlite3
import logging
import threading
import time
import traceback
import json
import urllib.parse
import tiktoken
import subprocess
import atexit
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI
import chromadb
import pymupdf4llm
import fitz  # PyMuPDF (PDF 페이지 수 사전 검사용)

# 동시 인덱싱 중복 실행 방지 락 (ChromaDB가 R/W 동시성을 네이티브 지원 → rw_lock/llm_inference_lock 불필요)
indexing_lock = threading.Lock()
is_indexing = False
# Z드라이브 파일 I/O 전용 락 (자동/수동 동시 실행 시 DB 코럽션 방지)
sync_lock = threading.Lock()
from langchain_community.document_loaders import TextLoader, CSVLoader, Docx2txtLoader, UnstructuredExcelLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain.schema import Document

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
MODEL_PATH = os.path.join(BASE_DIR, "models", "qwen2.5-1.5b-instruct-q4_k_m.gguf")
EMBED_DIR = os.path.join(BASE_DIR, "local_embeddings")
FAISS_DIR = os.path.join(DATA_DIR, "faiss_index")  # 레거시 경로 (미사용)
CHROMA_DIR = os.path.join(DATA_DIR, "chroma_db")
DB_PATH = os.path.join(DATA_DIR, "hhi_system.db")

# Z드라이브 공유 폴더 경로 (실제 환경에 맞게 수정)
SHARED_FOLDER_PATH = "Z:\\"

# KST(한국 표준시) 타임존 및 동기화 상태 파일 경로
KST = timezone(timedelta(hours=9))
STATE_FILE_PATH = os.path.join(DATA_DIR, "sync_state.json")

os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

_embeddings, _vector_store = None, None
llm_process = None  # LLM API 서버 서브프로세스 핸들 (atexit 정리용)
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)

def get_db_connection():
    """WAL 모드 + 20초 timeout으로 다중 접속 SQLite 충돌 원천 차단"""
    conn = sqlite3.connect(DB_PATH, timeout=20.0, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (emp_id TEXT PRIMARY KEY, password TEXT, name TEXT, rank TEXT, role TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT, emp_id TEXT, role TEXT, message TEXT)''')
    try: c.execute("ALTER TABLE chat ADD COLUMN session_id TEXT DEFAULT 'default'")
    except: pass
    c.execute("SELECT * FROM users WHERE emp_id='A544364'")
    if not c.fetchone():
        hashed_pw = generate_password_hash('A544364')
        c.execute("INSERT INTO users VALUES ('A544364', ?, '최보빈', '사원', 'admin')", (hashed_pw,))
    conn.commit()
    conn.close()

init_db()

# ---- 계정 관리 (Admin) ----
def authenticate(emp_id, password):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT emp_id, password, name, rank, role FROM users WHERE emp_id=?", (emp_id,))
    user = c.fetchone()
    conn.close()
    if user and check_password_hash(user[1], password):
        return {"emp_id": user[0], "name": user[2], "rank": user[3], "role": user[4]}
    return None

def get_all_users():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT emp_id, password, name, rank, role FROM users")
    users = [{"emp_id": r[0], "password": r[1], "name": r[2], "rank": r[3], "role": r[4]} for r in c.fetchall()]
    conn.close()
    return users

def upsert_user(emp_id, password, name, rank, role):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (emp_id, password, name, rank, role) VALUES (?, ?, ?, ?, ?)",
              (emp_id, password, name, rank, role))
    conn.commit()
    conn.close()

def delete_user(emp_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE emp_id=?", (emp_id,))
    conn.commit()
    conn.close()

# ---- 세션 및 메시지 관리 ----
def get_chat_sessions(emp_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT session_id, role, message, timestamp
        FROM chat
        WHERE emp_id=? AND (role='user' OR role='title')
        ORDER BY id ASC
    """, (emp_id,))

    sess_dict = {}
    for r in c.fetchall():
        sid, role, msg, ts = r[0], r[1], r[2], r[3]
        if sid not in sess_dict:
            sess_dict[sid] = {'title': msg[:20].replace('![image]', '[사진]'), 'ts': ts, 'has_custom_title': False}

        if role == 'title':
            sess_dict[sid]['title'] = msg
            sess_dict[sid]['has_custom_title'] = True
        elif role == 'user' and not sess_dict[sid]['has_custom_title']:
            sess_dict[sid]['title'] = msg[:20].replace('![image]', '[사진]')
            sess_dict[sid]['ts'] = ts

    sessions = [{"session_id": sid, "title": data['title'], "date": data['ts']} for sid, data in sess_dict.items()]
    sessions.sort(key=lambda x: x['date'], reverse=True)
    conn.close()
    return sessions

def delete_session(emp_id, session_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM chat WHERE emp_id=? AND session_id=?", (emp_id, session_id))
    conn.commit()
    conn.close()

def delete_message(msg_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM chat WHERE id=?", (msg_id,))
    conn.commit()
    conn.close()

def update_message(msg_id, text):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE chat SET message=? WHERE id=?", (text, msg_id))
    conn.commit()
    conn.close()

def save_chat(emp_id, session_id, role, message):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO chat (session_id, timestamp, emp_id, role, message) VALUES (?, ?, ?, ?, ?)",
              (session_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), emp_id, role, message))
    conn.commit()
    conn.close()

def get_chat_history(emp_id, session_id, limit=3):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, role, message FROM chat WHERE emp_id=? AND session_id=? AND role != 'title' ORDER BY id DESC LIMIT ?", (emp_id, session_id, limit * 2))
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))

def get_chat_history_with_tokens(emp_id, session_id, max_tokens=2000):
    """tiktoken 기반 정밀 토큰 계산으로 최대 허용 토큰 내에서 과거 대화를 역산해 반환"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, role, message FROM chat WHERE emp_id=? AND session_id=? AND role != 'title' ORDER BY id DESC", (emp_id, session_id))
    rows = c.fetchall()
    conn.close()

    try:
        encoder = tiktoken.get_encoding("cl100k_base")
    except Exception:
        # tiktoken 실패 시 폴백: 글자수 기반 근사치
        return list(reversed(rows[:6]))

    valid_history = []
    current_tokens = 0
    for r in rows:
        msg_text = f"{r[1]}: {r[2]}"
        tokens = len(encoder.encode(msg_text))
        if current_tokens + tokens > max_tokens:
            break
        current_tokens += tokens
        valid_history.append(r)

    return list(reversed(valid_history))

def get_all_admin_logs():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT c.id, c.timestamp, c.emp_id, u.name, u.rank, c.role, c.message, c.session_id
        FROM chat c LEFT JOIN users u ON c.emp_id = u.emp_id
        WHERE c.role != 'title'
        ORDER BY c.id DESC LIMIT 200
    """)
    logs = []
    for r in c.fetchall():
        role_disp = "AI" if r[5] == 'ai' else ("SYSTEM" if r[5] == 'system' else f"{r[3]} {r[4]}")
        logs.append({"id": r[0], "time": r[1], "emp_id": r[2], "role_display": role_disp, "msg": r[6], "sess_id": r[7]})
    conn.close()
    return logs

def update_session_title(emp_id, session_id, new_title):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM chat WHERE emp_id=? AND session_id=? AND role='title'", (emp_id, session_id))
    c.execute("INSERT INTO chat (session_id, timestamp, emp_id, role, message) VALUES (?, ?, ?, 'title', ?)",
              (session_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), emp_id, new_title))
    conn.commit()
    conn.close()

def generate_ai_title(user_input):
    try:
        llm = load_llm()
        prompt = (
            "<|im_start|>system\n"
            "다음 사용자의 질문을 분석하여 15자 이내의 짧고 명확한 대화방 제목을 생성하세요. "
            "(예: 기량평가 보고서 작성, 사내기능대회 요약 등). 불필요한 설명 없이 오직 제목만 출력하세요."
            "<|im_end|>\n<|im_start|>user\n"
            f"{user_input}"
            "<|im_end|>\n<|im_start|>assistant\n"
        )
        res = llm(prompt, max_tokens=20, stop=["<|im_end|>", "\n"])
        title = res['choices'][0]['text'].strip().replace('"', '').replace("'", "")
        return title[:20] if title else user_input[:20]
    except Exception as e:
        logging.error(f"타이틀 생성 에러: {e}")
        return user_input[:20]

# ---- 파일 및 RAG 엔진 ----
def delete_file(filename):
    filepath = os.path.join(DATA_DIR, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
        build_vector_db()
        return True
    return False

def get_uploaded_files():
    valid_exts = ('.pdf', '.txt', '.csv', '.xlsx', '.docx')
    return [f for f in os.listdir(DATA_DIR) if f.lower().endswith(valid_exts)]

def start_llm_server():
    """Llama.cpp API 서버를 백그라운드 서브프로세스로 구동 (포트 8000, 병렬 추론 활성화)"""
    global llm_process
    if llm_process is not None:
        return
    cmd = [
        "python3", "-m", "llama_cpp.server",
        "--model", MODEL_PATH,
        "--host", "127.0.0.1",
        "--port", "8000",
        "--n_ctx", "4096",
        "--n_threads", str(max(1, os.cpu_count() - 1))
    ]
    llm_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import urllib.request
    logging.info("[LLM 서버] API 서버 시작 중... (http://127.0.0.1:8000)")
    for _ in range(30):
        time.sleep(1)
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=2)
            logging.info("[LLM 서버] 독립형 API 서버 구동 완료 및 포트 8000 바인딩")
            return
        except Exception:
            pass
    logging.warning("[LLM 서버] 응답 대기 시간 초과. 서버가 준비 중일 수 있습니다.")

def cleanup_process():
    """Flask 프로세스 종료 시 서브프로세스(에 LLM 서버) 함께 종료 (좌비 프로세스 방지)"""
    global llm_process
    if llm_process:
        llm_process.terminate()
        llm_process.wait()
        logging.info("[종료] 독립형 LLM API 서버 프로세스 정상 종료")

atexit.register(cleanup_process)

def get_llm_client():
    """OpenAI 호환 클라이언트로 로여 LLM API 서버와 통신"""
    return OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-offline")

def load_embeddings():
    global _embeddings
    if _embeddings is None: _embeddings = HuggingFaceEmbeddings(model_name="jhgan/ko-sroberta-multitask", cache_folder=EMBED_DIR, model_kwargs={'device': 'cpu'})
    return _embeddings

def build_vector_db():
    global _vector_store, is_indexing

    if is_indexing:
        logging.info("[인덱싱] 현재 다른 문서가 학습 중입니다. 중복 실행 무시.")
        return False

    with indexing_lock:
        is_indexing = True
        try:
            files = get_uploaded_files()
            if not files:
                _vector_store = None
                return False

            docs = []
            for file in files:
                path = os.path.join(DATA_DIR, file)
                ext = file.lower().split('.')[-1]
                try:
                    if ext == 'pdf':
                        # [방어 로직] 50페이지 초과 PDF 사전 차단 (PyMuPDF4LLM RAM 초과 방지)
                        with fitz.open(path) as pdf_doc:
                            if len(pdf_doc) > 50:
                                error_msg = f"학습 거부: '{file}' (페이지 수 {len(pdf_doc)}/50 초과). RAM 보호를 위해 학습에서 제외됩니다."
                                logging.warning(error_msg)
                                save_chat('system', 'sys_log', 'system', error_msg)
                                continue
                        # 50페이지 이하만 마크다운 파싱 진행
                        md_text = pymupdf4llm.to_markdown(path)
                        docs.append(Document(page_content=md_text, metadata={"source": file}))
                    elif ext == 'txt':
                        docs.extend(TextLoader(path, encoding='utf-8').load())
                    elif ext == 'csv':
                        docs.extend(CSVLoader(path, encoding='utf-8').load())
                    elif ext == 'docx':
                        docs.extend(Docx2txtLoader(path).load())
                    elif ext == 'xlsx':
                        docs.extend(UnstructuredExcelLoader(path, mode="elements").load())
                    else:
                        continue
                except Exception as e:
                    error_msg = f"문서 로드 실패 ({file}): {str(e)}"
                    logging.error(error_msg)
                    save_chat('system', 'sys_log', 'system', error_msg)

            if not docs:
                return False

            # 마크다운 문단(##)/표(|) 것빁 최소화를 위한 최적화 separators
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=800,
                chunk_overlap=250,  # 16GB RAM 환경 콘텍스트 오버플로우 방지
                separators=["\n\n", "\n", "##", "|", "."]
            )
            splits = text_splitter.split_documents(docs)
            if not splits:
                return False

            # [MSA] ChromaDB: 기존 콜렉션 삭제 후 전체 재수집 (실시간 읽기 차단 없음)
            try:
                chroma_client.delete_collection("hhi_knowledge")
            except Exception:
                pass
            try:
                _vector_store = Chroma.from_documents(
                    documents=splits,
                    embedding=load_embeddings(),
                    persist_directory=CHROMA_DIR,
                    client=chroma_client,
                    collection_name="hhi_knowledge"
                )
                logging.info("[인덱싱] ChromaDB 백그라운드 학습 완료!")
                return True
            except Exception as e:
                logging.error(f"ChromaDB 인덱싱 실패: {str(e)}")
                return False
        finally:
            is_indexing = False

def get_vector_store():
    global _vector_store
    if _vector_store is None and os.path.exists(CHROMA_DIR):
        try:
            _vector_store = Chroma(
                collection_name="hhi_knowledge",
                embedding_function=load_embeddings(),
                persist_directory=CHROMA_DIR,
                client=chroma_client
            )
        except Exception as e:
            logging.warning(f"ChromaDB 로드 실패: {e}")
    return _vector_store

def generate_answer_stream(user_input, emp_id, session_id, image_url=None, user_name="사용자"):
    try:
        yield " "

        display_msg = user_input
        if image_url:
            display_msg = f"![image]({image_url})\n{user_input}"

        save_chat(emp_id, session_id, "user", display_msg)

        # OCR 제거: 텍스트 없이 이미지만 올렸을 경우 즉시 역질문 반환
        if image_url and not user_input.strip():
            direct_msg = "첨부해주신 이미지는 시스템 구조상(텍스트 전용 AI) 제가 직접 읽거나 분석할 수 없습니다.<br>어떤 내용인지 **텍스트로 간략히 설명해주시거나 구체적인 질문**을 남겨주시면, 관련 사내 문서를 찾아 상세히 답변해 드리겠습니다."
            yield direct_msg
            save_chat(emp_id, session_id, "ai", direct_msg)
            return

        # [MSA] ChromaDB 검색 (R/W 동시성 네이티브 지원 → Lock 불필요)
        context = ""
        source_files = []
        vs = get_vector_store()
        if vs:
            rag_docs = vs.similarity_search(user_input, k=6)  # 3 → 6: 유의어 누락 확률 감소
            context = "\n".join([d.page_content for d in rag_docs])
            # 중복 제거한 원본 파일명 리스트 추출
            seen = set()
            for doc in rag_docs:
                src = doc.metadata.get('source', '')
                if src and src not in seen:
                    seen.add(src)
                    source_files.append(src)

        file_list = get_uploaded_files()

        sys_prompt = f"""당신은 HD현대중공업 기술교육원의 보안망 내부에서 동작하는 '문서 분석 전용 AI'입니다.
반드시 아래 제공된 [사내 문서]의 내용만을 근거로 답변해야 합니다.

[사내 문서]
{context if context else "현재 학습된 사내 문서가 없습니다."}

[절대 준수 규칙]
1. 당신이 기존에 학습한 사전 지식(일반 상식, 외부 정보 등)은 절대 사용하지 마십시오.
2. 사용자의 질문에 대한 답이 [사내 문서] 내용에 포함되어 있지 않다면, 억지로 유추하거나 지어내지 말고 반드시 다음과 같이 답변하십시오: "제공된 사내 문서에서는 해당 내용에 대한 정보를 찾을 수 없습니다."
3. 사용자가 PPT 프롬프트 작성이나 문서 요약을 요청할 때에도, 오직 [사내 문서]에 포함된 데이터만 사용하여 작성하십시오.
4. 가능한 경우, 답변의 근거가 된 문서의 이름이나 출처를 간략히 명시하십시오."""

        if image_url:
            sys_prompt += "\n5. [시스템 강제 지시: 사용자가 이미지를 첨부했습니다. 당신은 이미지를 볼 수 없으므로 절대 이전 답변을 반복하지 마십시오. 반드시 사용자의 텍스트 질문에 대해서만 새롭게 답변하십시오.]"

        system_content = sys_prompt

        # tiktoken 기반 2000토큰 이내 히스토리를 OpenAI messages 포맷으로 변환
        raw_history = get_chat_history_with_tokens(emp_id, session_id, max_tokens=2000)
        formatted_messages = [{"role": "system", "content": system_content}]
        for r in raw_history[:-1]:  # 현재 질문 제외
            role_type = "user" if r[1] == "user" else "assistant"
            formatted_messages.append({"role": role_type, "content": r[2]})
        formatted_messages.append({"role": "user", "content": user_input})

        # [MSA] Lock 없이 API 서버로 전송 → 서버가 자체 Batching/큐잉 처리
        client = get_llm_client()
        response = client.chat.completions.create(
            model="local-model",
            messages=formatted_messages,
            stream=True,
            max_tokens=1024,
            timeout=120.0
        )

        full_answer = ""
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                full_answer += token
                yield token

        # Perplexity식 참조 문서 다운로드 링크 주입
        if source_files:
            source_footer = "\n\n---\n**📚 참고 문서 (클릭하여 다운로드):**\n"
            for src in source_files:
                safe_url = urllib.parse.quote(src)  # 한글/공백 파일명 URL 안전 인코딩
                source_footer += f"- [{src}](/download/{safe_url})\n"
            full_answer += source_footer
            yield source_footer

        save_chat(emp_id, session_id, "ai", full_answer)

    except Exception as e:
        error_trace = traceback.format_exc()
        logging.error(f"[API 통신 에러]: {error_trace}")
        yield f"\n\n[API 서버 에러] 응답을 생성할 수 없습니다. 시스템을 재시작해 주십시오.\n상세: {str(e)}"


def execute_z_drive_sync():
    """Z드라이브 동기화 핵심 로직 (Thread-Safe, 자동/수동 공통 호출)"""

    # Non-blocking: 이미 동기화 중이면 즉시 거부 (중복 실행 사전 차단)
    if not sync_lock.acquire(blocking=False):
        logging.warning("동기화 작업이 이미 진행 중입니다. 중복 요청이 무시되었습니다.")
        return False, "현재 다른 동기화 작업이 진행 중입니다. 잠시 후 다시 시도해주세요."

    try:
        if not os.path.exists(SHARED_FOLDER_PATH):
            logging.warning(f"Z드라이브({SHARED_FOLDER_PATH}) 접근 불가.")
            return False, "Z드라이브에 접근할 수 없습니다."

        if os.path.exists(STATE_FILE_PATH):
            with open(STATE_FILE_PATH, 'r', encoding='utf-8') as f:
                sync_state = json.load(f)
        else:
            sync_state = {}

        valid_exts = ('.pdf', '.txt', '.csv', '.xlsx', '.docx')
        z_files = [f for f in os.listdir(SHARED_FOLDER_PATH) if f.lower().endswith(valid_exts)]
        new_files_detected = False
        synced_files = []

        for file in z_files:
            source_path = os.path.join(SHARED_FOLDER_PATH, file)
            target_path = os.path.join(DATA_DIR, file)
            try:
                current_mtime = os.path.getmtime(source_path)
            except Exception:
                continue

            if file not in sync_state or sync_state[file] < current_mtime:
                try:
                    shutil.copy2(source_path, target_path)
                    sync_state[file] = current_mtime
                    new_files_detected = True
                    synced_files.append(file)
                    log_msg = f"Z드라이브 문서 업데이트 됨: {file}"
                    logging.info(log_msg)
                    save_chat('system', 'sys_sync', 'system', log_msg)
                except Exception as e:
                    logging.error(f"복사 실패 ({file}): {e}")

        if new_files_detected:
            with open(STATE_FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(sync_state, f, ensure_ascii=False, indent=4)
            logging.info("신규 감지: 벡터 DB 학습 큐에 등록합니다.")
            build_vector_db()
            return True, f"동기화 및 학습 대기열 등록 완료: {', '.join(synced_files)}"
        else:
            return True, "새로 업데이트된 파일이 없습니다."

    except Exception as e:
        logging.error(f"동기화 에러: {e}")
        return False, f"동기화 실패: {str(e)}"
    finally:
        sync_lock.release()  # 성공/실패 여부와 무관하게 항상 해제


def z_drive_watcher():
    """매일 KST 09:00에 Z드라이브를 스캔하고, 수정된 파일을 감지하여 동기화하는 스마트 워커"""
    logging.info("Z드라이브 자동 감시 스레드 가동 (매일 KST 09:00 스캔)")

    while True:
        # 1. 다음 KST 09:00까지의 대기 시간 계산
        now = datetime.now(KST)
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)

        # 이미 오늘 09시가 지났으면 내일 09시로 타겟 설정
        if now >= target:
            target += timedelta(days=1)

        sleep_seconds = (target - now).total_seconds()
        logging.info(f"다음 Z드라이브 동기화: {target.strftime('%Y-%m-%d %H:%M:%S')} KST ({int(sleep_seconds)}초 대기)")

        time.sleep(sleep_seconds)

        # 09:00 도달 시 분리된 핵심 로직 호출
        execute_z_drive_sync()


def start_background_sync():
    sync_thread = threading.Thread(target=z_drive_watcher, daemon=True)
    sync_thread.start()
    logging.info("Z드라이브 백그라운드 감시 스레드가 시작되었습니다. (매일 KST 09:00 실행)")
