# HHI-OFFLINE-AI (기술교육원 오프라인 LLM 시스템)

본 시스템은 외부 인터넷망과 단절된 폐쇄망 환경에서 작동하도록 설계된 기업용 마이크로서비스 아키텍처(MSA) 기반 AI 지식 관리 시스템입니다.

## 💻 시스템 요구사항 (Hardware Constraints)
- **OS:** Windows 11 Enterprise
- **CPU:** Intel i5-12500 (6 Core) 이상
- **RAM:** 최소 16GB (단일 LLM 추론 전용)
- **제한 사항:** 최대 업로드 파일 크기 15MB 제한 / PDF 최대 50페이지 파싱 제한 (OOM 방지)

## 🏗️ 아키텍처 구성 (Microservices)
1. **Frontend / API Router:** Flask (포트 5000) - Waitress 멀티스레드 서빙
2. **LLM Inference Engine:** Llama.cpp API Server (포트 8000) - 연속 배치(Continuous Batching) 처리
3. **Vector Database:** ChromaDB (PersistentClient)
4. **Data Parser:** PyMuPDF4LLM (마크다운 변환)

## 🚀 실행 방법 (How to Run)
반드시 프로젝트 최상단에 위치한 **`▶ 시스템 구동.bat`** 파일을 관리자 권한으로 실행하십시오.
- 해당 배치 스크립트는 5000, 8000번 포트의 좀비 프로세스를 자동 사살(`taskkill`)한 후 서버를 구동합니다.
- 파이썬 콘솔 창을 닫으면 모든 백그라운드 AI 프로세스가 `atexit` 모듈에 의해 자동 종료됩니다.

## 🔄 핵심 자동화 기능 (Z-Drive Sync)
- **자동 스케줄링:** 매일 **한국 시간(KST) 오전 09:00** 정각에 백그라운드 스레드가 실행됩니다.
- **스마트 업데이트:** 파일의 수정 시간(`mtime`)을 비교하여 변경된 문서만 선별적으로 재학습합니다.
- **수동 강제 동기화 (Admin):** 긴급 규정 배포 시, 관리자는 웹 UI의 `[🔄 Z드라이브 강제 동기화]` 버튼을 클릭하여 스케줄러 대기 없이 즉시 동기화 및 벡터 DB 인덱싱을 강제 실행할 수 있습니다. (내부 `sync_lock` Thread-Lock을 통해 자동/수동 동시 실행 시 발생하는 DB 충돌을 방어합니다.)
