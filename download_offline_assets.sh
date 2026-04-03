#!/usr/bin/env bash
set -e

echo "=========================================="
echo " Windows 64bit / Python 3.11 오프라인 패키지 수집"
echo "=========================================="

# 1. 폴더 생성
echo ""
echo "[1/4] ./libs 폴더 생성..."
mkdir -p ./libs

# 2. requirements.txt 기반 Windows/Python 3.11 전용 .whl 다운로드
#    llama-cpp-python은 아래 3단계에서 수동 다운로드하므로 제외
echo ""
echo "[2/4] pip download: Windows 64bit / Python 3.11 호환 .whl 다운로드..."
pip download \
    langchain==0.2.0 \
    langchain-community==0.2.0 \
    pypdf==4.2.0 \
    faiss-cpu==1.8.0 \
    Flask==3.0.3 \
    --platform win_amd64 \
    --python-version 311 \
    --only-binary=:all: \
    -d ./libs

# 3. llama-cpp-python v0.2.90 GitHub Release 직접 다운로드
LLAMA_WHL="llama_cpp_python-0.2.90-cp311-cp311-win_amd64.whl"
LLAMA_URL="https://github.com/abetlen/llama-cpp-python/releases/download/v0.2.90/${LLAMA_WHL}"

echo ""
echo "[3/4] llama-cpp-python v0.2.90 (Windows cp311) 다운로드..."
if [ -f "./libs/${LLAMA_WHL}" ]; then
    echo "  이미 존재함, 건너뜀: ./libs/${LLAMA_WHL}"
else
    wget --progress=bar:force:noscroll \
         --tries=3 \
         --timeout=60 \
         -P ./libs \
         "${LLAMA_URL}"
fi

# 4. 결과물 ZIP 압축
echo ""
echo "[4/4] offline_assets.zip 으로 압축..."
if [ -f "./offline_assets.zip" ]; then
    echo "  기존 offline_assets.zip 삭제..."
    rm ./offline_assets.zip
fi

zip -r offline_assets.zip ./libs

echo ""
echo "=========================================="
echo " 완료!"
echo "=========================================="
echo ""
echo "[libs 폴더 파일 목록]"
ls -lh ./libs/*.whl 2>/dev/null | awk '{print $5, $9}' || echo "(whl 없음)"

echo ""
WHL_COUNT=$(ls ./libs/*.whl 2>/dev/null | wc -l)
ZIP_SIZE=$(du -sh ./offline_assets.zip 2>/dev/null | cut -f1)
echo "  .whl 파일 수  : ${WHL_COUNT}개"
echo "  ZIP 파일 크기 : ${ZIP_SIZE}"
echo "=========================================="
