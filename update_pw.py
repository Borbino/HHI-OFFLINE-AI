import sqlite3
import os
from werkzeug.security import generate_password_hash

# DB 경로 설정
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "hhi_system.db")

def update_admin_password():
    if not os.path.exists(DB_PATH):
        print("[오류] DB 파일을 찾을 수 없습니다.")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 관리자 계정의 비밀번호를 안전한 해시 값으로 강제 업데이트
    hashed_pw = generate_password_hash('A544364')
    c.execute("UPDATE users SET password=? WHERE emp_id='A544364'", (hashed_pw,))

    conn.commit()
    conn.close()
    print("[성공] 관리자(A544364)의 평문 비밀번호가 해시로 정상 업데이트되었습니다. 이제 로그인 가능합니다.")

if __name__ == "__main__":
    update_admin_password()
