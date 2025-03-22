#!/bin/bash

# 로그 파일 설정
LOG_FILE="/var/log/gpu-cooling-wrapper.log"

# 스크립트 경로 설정 (실제 경로로 변경하세요)
SCRIPT_PATH="/usr/lib/gpu-aio-controller/gpu-temperature-aio-controller.py"

# 설정값 (필요에 따라 수정하세요)
TEMP_THRESHOLD=50
MIN_PUMP_PWM=100
MAX_PUMP_PWM=255
MIN_FAN_PWM=100
MAX_FAN_PWM=255

# 로그 함수
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# NCT6798 칩을 찾기
find_nct6798() {
    for hwmon in /sys/class/hwmon/hwmon*; do
        if [ -f "$hwmon/name" ]; then
            name=$(cat "$hwmon/name")
            if [ "$name" = "nct6798" ]; then
                echo "$hwmon"
                return 0
            fi
        fi
    done
    
    return 1
}

# 메인 실행 함수
main() {
    log "GPU 냉각 제어 래퍼 시작..."
    
    # 서비스 시작 전 잠시 대기 (센서 초기화를 위해)
    sleep 10
    
    # NCT6798 칩 찾기
    log "NCT6798 칩 찾는 중..."
    HWMON_PATH=$(find_nct6798)
    
    if [ -z "$HWMON_PATH" ]; then
        log "오류: NCT6798 칩을 찾을 수 없습니다."
        exit 1
    fi
    
    log "NCT6798 칩 경로: $HWMON_PATH"
    
    # PWM 장치 확인
    PUMP_DEVICE="$HWMON_PATH/pwm2"
    FAN_DEVICE="$HWMON_PATH/pwm1"
    
    if [ ! -f "$PUMP_DEVICE" ]; then
        log "오류: 펌프 PWM 장치를 찾을 수 없습니다: $PUMP_DEVICE"
        exit 1
    fi
    
    log "펌프 PWM 장치: $PUMP_DEVICE"
    
    # 팬 장치 확인 및 명령 구성
    FAN_CMD=""
    if [ -f "$FAN_DEVICE" ]; then
        FAN_CMD="--fans $FAN_DEVICE"
        log "팬 PWM 장치: $FAN_DEVICE"
    else
        log "팬 PWM 장치를 찾을 수 없습니다. 펌프만 제어합니다."
    fi
    
    # 메인 스크립트 실행
    CMD="python3 $SCRIPT_PATH --pump $PUMP_DEVICE $FAN_CMD --threshold $TEMP_THRESHOLD --min-pump $MIN_PUMP_PWM --max-pump $MAX_PUMP_PWM --min-fan $MIN_FAN_PWM --max-fan $MAX_FAN_PWM"
    
    log "실행 명령: $CMD"
    log "메인 스크립트 시작..."
    
    # 실행
    eval "$CMD"
    
    # 종료 코드 확인
    EXIT_CODE=$?
    log "메인 스크립트 종료. 종료 코드: $EXIT_CODE"
    
    return $EXIT_CODE
}

# 메인 함수 실행
main