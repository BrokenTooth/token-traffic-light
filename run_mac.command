#!/bin/bash
# 더블클릭으로 macOS에서 토큰 신호등을 실행하기 위한 런처.
# (macOS는 .py 파일에 대한 실행 연결이 없어서 .command로 감싼 것)
cd "$(dirname "$0")"

if ! python3 -c "import pystray, PIL" 2>/dev/null; then
    echo "필요한 패키지를 설치합니다 (pystray, Pillow)..."
    python3 -m pip install --quiet pystray Pillow
fi

python3 token_tray_monitor_mac.py
