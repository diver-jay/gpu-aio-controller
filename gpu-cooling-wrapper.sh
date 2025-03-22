#!/bin/bash

# Log file setup
LOG_FILE="/var/log/gpu-cooling-wrapper.log"

# Script path (please change to actual path)
SCRIPT_PATH="/usr/lib/gpu-aio-controller/gpu-temperature-aio-controller.py"

# Settings (modify as needed)
TEMP_THRESHOLD=50
MIN_PUMP_PWM=100
MAX_PUMP_PWM=255
MIN_FAN_PWM=100
MAX_FAN_PWM=255

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

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

main() {
    log "Starting GPU cooling control wrapper..."
    
    # Wait briefly before starting the service (for sensor initialization)
    sleep 10
    
    log "Looking for NCT6798 chip..."
    HWMON_PATH=$(find_nct6798)
    
    if [ -z "$HWMON_PATH" ]; then
        log "Error: Cannot find NCT6798 chip."
        exit 1
    fi
    
    log "NCT6798 chip path: $HWMON_PATH"
    
    # Check PWM devices
    PUMP_DEVICE="$HWMON_PATH/pwm2"
    FAN_DEVICE="$HWMON_PATH/pwm1"
    
    if [ ! -f "$PUMP_DEVICE" ]; then
        log "Error: Cannot find pump PWM device: $PUMP_DEVICE"
        exit 1
    fi
    
    log "Pump PWM device: $PUMP_DEVICE"
    
    # Check fan device and configure command
    FAN_CMD=""
    if [ -f "$FAN_DEVICE" ]; then
        FAN_CMD="--fans $FAN_DEVICE"
        log "Fan PWM device: $FAN_DEVICE"
    else
        log "Fan PWM device not found. Controlling pump only."
    fi
    
    # Execute main script
    CMD="python3 $SCRIPT_PATH --pump $PUMP_DEVICE $FAN_CMD --threshold $TEMP_THRESHOLD --min-pump $MIN_PUMP_PWM --max-pump $MAX_PUMP_PWM --min-fan $MIN_FAN_PWM --max-fan $MAX_FAN_PWM"
    
    log "Execution command: $CMD"
    log "Starting main script..."
    
    eval "$CMD"
    
    EXIT_CODE=$?
    log "Main script terminated. Exit code: $EXIT_CODE"
    
    return $EXIT_CODE
}

main