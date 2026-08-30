#!/bin/bash
# 더블클릭으로 macOS에서 토큰 신호등을 실행하기 위한 런처.
# (macOS는 .py 파일에 대한 실행 연결이 없어서 .command로 감싼 것)
cd "$(dirname "$0")"

echo "===================================================="
echo " 토큰 신호등 실행 준비 중..."
echo "===================================================="

if ! command -v python3 >/dev/null 2>&1; then
    echo ""
    echo "[오류] python3를 찾을 수 없습니다."
    echo "https://www.python.org/downloads/ 에서 Python을 설치한 뒤 다시 실행해주세요."
    echo ""
    read -n 1 -s -r -p "아무 키나 누르면 창이 닫힙니다..."
    exit 1
fi

if ! python3 -c "import pystray, PIL" 2>/dev/null; then
    echo "필요한 패키지를 설치합니다 (pystray, Pillow)... 잠시만 기다려주세요."
    python3 -m pip install pystray Pillow
    if ! python3 -c "import pystray, PIL" 2>/dev/null; then
        echo ""
        echo "[오류] 패키지 설치에 실패했습니다. 위 메시지를 캡처해서 알려주세요."
        echo ""
        read -n 1 -s -r -p "아무 키나 누르면 창이 닫힙니다..."
        exit 1
    fi
fi

echo ""
echo "실행 중입니다. 화면 오른쪽 위 메뉴바를 확인해주세요."
echo "(이 창을 닫으면 신호등도 같이 꺼집니다)"
echo ""
python3 token_tray_monitor_mac.py
status=$?

echo ""
echo "===================================================="
if [ $status -ne 0 ]; then
    echo " 프로그램이 오류와 함께 종료됐습니다 (코드 $status). 위 메시지를 캡처해서 알려주세요."
else
    echo " 프로그램이 종료됐습니다."
fi
echo "===================================================="
read -n 1 -s -r -p "아무 키나 누르면 창이 닫힙니다..."
