import os
import time
import threading
import traceback
from flask import Flask, request, jsonify, render_template, Response, stream_with_context, session, redirect, url_for, send_from_directory
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from werkzeug.exceptions import RequestEntityTooLarge
from waitress import serve
import rag_engine

app = Flask(__name__, static_folder='static')
app.secret_key = 'hhi_offline_secure_key_2026'
app.config['UPLOAD_FOLDER'] = rag_engine.DATA_DIR
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024  # 15MB 하드 리밋 (OOM 방지)

@app.errorhandler(RequestEntityTooLarge)
def handle_file_size_error(e):
    return jsonify({'error': '파일 크기가 너무 큽니다. 최대 15MB까지만 업로드 가능합니다.'}), 413

@app.route('/')
def index():
    if 'emp_id' not in session: return redirect(url_for('login'))
    return render_template('index.html', user=session)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # JSON(fetch) 방식과 form 방식 모두 지원
        if request.is_json:
            data = request.get_json()
            emp_id = data.get('emp_id')
            password = data.get('password')
        else:
            emp_id = request.form.get('emp_id')
            password = request.form.get('password')
        user = rag_engine.authenticate(emp_id, password)
        if user:
            session.update(user)
            if request.is_json:
                return jsonify({'status': 'ok'})
            return redirect(url_for('index'))
        if request.is_json:
            return jsonify({'error': 'ID 또는 PW가 올바르지 않습니다.'}), 401
        return render_template('login.html', error="ID 또는 PW 불일치")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/download/<path:filename>', methods=['GET'])
def download_file(filename):
    if session.get('emp_id') is None:
        return jsonify({'error': '로그인이 필요합니다.'}), 401
    try:
        return send_from_directory(rag_engine.DATA_DIR, filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify({'error': '요청하신 파일을 찾을 수 없습니다.'}), 404

@app.route('/admin/force_sync', methods=['POST'])
def force_sync():
    if session.get('role') != 'admin':
        return jsonify({'error': '관리자 권한이 필요합니다.'}), 403
    success, message = rag_engine.execute_z_drive_sync()
    if success:
        return jsonify({'message': message}), 200
    else:
        return jsonify({'error': message}), 500

@app.route('/api/sessions', methods=['GET'])
def get_sessions(): return jsonify(rag_engine.get_chat_sessions(session.get('emp_id')))

@app.route('/api/sessions/delete', methods=['POST'])
def delete_session():
    rag_engine.delete_session(session.get('emp_id'), request.get_json()['session_id'])
    return jsonify({'status': 'success'})

@app.route('/api/files', methods=['GET'])
def get_files(): return jsonify(rag_engine.get_uploaded_files())

@app.route('/api/history', methods=['POST'])
def get_history(): return jsonify(rag_engine.get_chat_history(session.get('emp_id'), request.get_json().get('session_id'), limit=50))

@app.route('/chat', methods=['POST'])
def chat():
    if 'emp_id' not in session: return jsonify({'error': '로그인 필요'}), 401

    # 이미지 파일이 포함된 FormData 방식과 기존 JSON 방식을 모두 지원
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        msg = request.form.get('message', '')
        session_id = request.form.get('session_id')
        image = request.files.get('image')
        image_url = None
        if image and image.filename:
            upload_dir = os.path.join(app.root_path, 'static', 'uploads')
            os.makedirs(upload_dir, exist_ok=True)
            safe_filename = f"chat_img_{int(time.time())}_{secure_filename(image.filename)}"
            image.save(os.path.join(upload_dir, safe_filename))
            image_url = f"/static/uploads/{safe_filename}"
    else:
        data = request.get_json()
        msg = data.get('message', '')
        session_id = data.get('session_id')
        image_url = None

    def generate():
        try:
            user_name = f"{session.get('name', '')} {session.get('rank', '')}".strip() or "사용자"
            for chunk in rag_engine.generate_answer_stream(msg, session['emp_id'], session_id, image_url, user_name): yield chunk
        except Exception as e: yield f"<span style='color:red;'>시스템 에러: {str(e)}</span>"
    return Response(stream_with_context(generate()), mimetype='text/plain')

# ---- 채팅 수정/삭제 API ----
@app.route('/api/chat/edit', methods=['POST'])
def edit_msg():
    data = request.get_json()
    rag_engine.update_message(data['id'], data['text'])
    return jsonify({'status': 'success'})

@app.route('/api/chat/delete', methods=['POST'])
def delete_msg():
    rag_engine.delete_message(request.get_json()['id'])
    return jsonify({'status': 'success'})

@app.route('/api/sessions/gen_title', methods=['POST'])
def gen_title():
    if not session.get('emp_id'): return jsonify({'error': '로그인 필요'}), 401
    data = request.get_json()
    ai_title = rag_engine.generate_ai_title(data.get('message', ''))
    rag_engine.update_session_title(session.get('emp_id'), data['session_id'], ai_title)
    return jsonify({'status': 'success', 'title': ai_title})

@app.route('/api/sessions/update', methods=['POST'])
def update_session_title():
    if not session.get('emp_id'): return jsonify({'error': '로그인 필요'}), 401
    data = request.get_json()
    rag_engine.update_session_title(session.get('emp_id'), data['session_id'], data['title'])
    return jsonify({'status': 'success'})

# ---- 관리자 전용 API ----
@app.route('/api/admin/logs', methods=['GET'])
def admin_logs():
    if session.get('role') != 'admin': return jsonify({'error': '권한 없음'}), 403
    return jsonify(rag_engine.get_all_admin_logs())

@app.route('/api/admin/log_detail', methods=['POST'])
def get_log_detail():
    if session.get('role') != 'admin': return jsonify({'error': '권한 없음'}), 403
    data = request.get_json()
    return jsonify(rag_engine.get_chat_history(data['emp_id'], data['session_id'], limit=50))

@app.route('/api/admin/delete_file', methods=['POST'])
def admin_delete_file():
    if session.get('role') != 'admin': return jsonify({'error': '권한 없음'}), 403
    success = rag_engine.delete_file(request.get_json()['filename'])
    return jsonify({'status': 'success' if success else 'failed'})

@app.route('/api/admin/users', methods=['GET', 'POST', 'DELETE'])
def admin_users():
    if session.get('role') != 'admin': return jsonify({'error': '권한 없음'}), 403
    if request.method == 'GET':
        return jsonify(rag_engine.get_all_users())
    elif request.method == 'POST':
        d = request.get_json()
        hashed_pw = generate_password_hash(d['password'])
        rag_engine.upsert_user(d['emp_id'], hashed_pw, d['name'], d['rank'], d['role'])
        return jsonify({'status': 'success'})
    elif request.method == 'DELETE':
        rag_engine.delete_user(request.get_json()['emp_id'])
        return jsonify({'status': 'success'})

@app.route('/upload', methods=['POST'])
def upload_file():
    if session.get('role') != 'admin': return jsonify({'error': '관리자 권한 필요'}), 403
    try:
        f = request.files.get('file')
        if not f or not f.filename:
            return jsonify({'error': '파일이 없습니다.'}), 400
        safe_filename = os.path.basename(f.filename)
        file_ext = safe_filename.rsplit('.', 1)[-1].lower() if '.' in safe_filename else ''
        if file_ext not in ['pdf', 'txt', 'csv', 'xlsx', 'docx']:
            return jsonify({'error': '지원하지 않는 파일 형식입니다.'}), 400
        f.save(os.path.join(app.config['UPLOAD_FOLDER'], safe_filename))
        threading.Thread(target=rag_engine.build_vector_db, daemon=True).start()
        rag_engine.save_chat(session.get('emp_id', 'unknown'), 'system_upload', 'system', f'파일 업로드 됨: {safe_filename}')
        return jsonify({'message': f'파일 저장 완료!\n({safe_filename})\n순차적으로 시스템 학습 대기열에 추가되었습니다.'}), 200
    except RequestEntityTooLarge:
        return jsonify({'error': '파일이 15MB 제한을 초과합니다.'}), 413
    except Exception as e:
        logging.error(f'[업로드 에러]: {traceback.format_exc()}')
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    try:
        rag_engine.start_llm_server()  # [MSA] LLM API 서버 백그라운드 구동 (포트 8000, 자체 큐잉)
        rag_engine.start_background_sync()
    except Exception as e:
        print(e)
    print("HD현대중공업 AI 서버가 시작되었습니다 (Waitress MSA 모드) -> http://localhost:5000")
    serve(app, host='0.0.0.0', port=5000, threads=8)
