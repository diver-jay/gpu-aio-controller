#!/usr/bin/env python3
import subprocess
import time
import logging
import argparse
import os
import signal
import sys
import re

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("gpu_cooling_controller.log")
    ]
)
logger = logging.getLogger(__name__)

class GPUCoolingController:
    def __init__(self, pump_device, fan_devices=None, update_interval=5, temp_threshold=70, 
                 min_pump_pwm=100, max_pump_pwm=255, min_fan_pwm=80, max_fan_pwm=255, pwm_step=20):
        """
        GPU 온도 기반 냉각 시스템 제어기 초기화
        
        Args:
            pump_device: AIO 펌프 PWM 장치 경로 (예: '/sys/class/hwmon/hwmon1/pwm2')
            fan_devices: 팬 PWM 장치 경로 목록 (예: ['/sys/class/hwmon/hwmon1/pwm1'])
            update_interval: 업데이트 간격(초)
            temp_threshold: 펌프/팬 속도 증가를 시작할 온도 임계값(°C)
            min_pump_pwm: 펌프 최소 PWM 값 (0-255)
            max_pump_pwm: 펌프 최대 PWM 값 (0-255)
            min_fan_pwm: 팬 최소 PWM 값 (0-255)
            max_fan_pwm: 팬 최대 PWM 값 (0-255)
            pwm_step: PWM 조정 단계
        """
        self.pump_device = pump_device
        self.fan_devices = fan_devices or []
        
        # PWM 장치 경로에서 pwm_enable 파일 경로 추출
        pump_base = os.path.basename(pump_device)
        pump_dir = os.path.dirname(pump_device)
        self.pump_enable = os.path.join(pump_dir, f"{pump_base}_enable")
        
        self.fan_enables = []
        for fan_device in self.fan_devices:
            fan_base = os.path.basename(fan_device)
            fan_dir = os.path.dirname(fan_device)
            self.fan_enables.append(os.path.join(fan_dir, f"{fan_base}_enable"))
        
        self.update_interval = update_interval
        self.temp_threshold = temp_threshold
        self.min_pump_pwm = min_pump_pwm
        self.max_pump_pwm = max_pump_pwm
        self.min_fan_pwm = min_fan_pwm
        self.max_fan_pwm = max_fan_pwm
        self.pwm_step = pwm_step
        
        self.current_pump_pwm = min_pump_pwm
        self.current_fan_pwms = [min_fan_pwm] * len(self.fan_devices)
        
        self.running = False
        self.original_settings = {}
        
        # 요구 사항 확인
        self._check_requirements()
        
    def _check_requirements(self):
        """필요한 도구와 접근 권한 확인"""
        try:
            # nvidia-smi 확인
            subprocess.run(["nvidia-smi"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            
            # PWM 장치 존재 확인
            devices_to_check = [self.pump_device] + self.fan_devices
            
            for device in devices_to_check:
                if not os.path.exists(device):
                    logger.error(f"PWM 장치를 찾을 수 없습니다: {device}")
                    logger.error("올바른 PWM 장치 경로를 지정했는지 확인하세요.")
                    sys.exit(1)
                    
                # 쓰기 권한 확인
                if not os.access(device, os.W_OK):
                    logger.error(f"PWM 장치에 쓰기 권한이 없습니다: {device}")
                    logger.error("이 스크립트를 root 권한으로 실행하세요.")
                    sys.exit(1)
                
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error("nvidia-smi가 설치되지 않았거나 GPU를 찾을 수 없습니다.")
            sys.exit(1)
    
    def get_gpu_temp(self):
        """nvidia-smi를 사용하여 GPU 온도 가져오기"""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            temps = [int(temp.strip()) for temp in result.stdout.split('\n') if temp.strip()]
            
            if not temps:
                logger.error("GPU 온도를 읽을 수 없습니다.")
                return None
            
            # 여러 GPU가 있는 경우 최고 온도 반환
            return max(temps)
        
        except subprocess.CalledProcessError as e:
            logger.error(f"GPU 온도를 가져오는 동안 오류 발생: {e}")
            return None
    
    def backup_original_settings(self):
        """원래 PWM 설정 백업"""
        try:
            # 펌프 설정 백업
            if os.path.exists(self.pump_enable):
                with open(self.pump_enable, 'r') as f:
                    self.original_settings[self.pump_enable] = f.read().strip()
            
            with open(self.pump_device, 'r') as f:
                self.original_settings[self.pump_device] = f.read().strip()
            
            # 팬 설정 백업
            for i, fan_device in enumerate(self.fan_devices):
                with open(fan_device, 'r') as f:
                    self.original_settings[fan_device] = f.read().strip()
                
                fan_enable = self.fan_enables[i]
                if os.path.exists(fan_enable):
                    with open(fan_enable, 'r') as f:
                        self.original_settings[fan_enable] = f.read().strip()
                        
            logger.info(f"원래 PWM 설정 백업 완료")
        except Exception as e:
            logger.error(f"원래 설정을 백업하는 동안 오류 발생: {e}")
    
    def restore_original_settings(self):
        """원래 PWM 설정 복원"""
        try:
            for device, value in self.original_settings.items():
                try:
                    with open(device, 'w') as f:
                        f.write(value)
                except Exception as e:
                    logger.error(f"{device} 설정을 복원하는 동안 오류 발생: {e}")
            
            logger.info("원래 PWM 설정이 복원되었습니다.")
        except Exception as e:
            logger.error(f"설정을 복원하는 동안 오류 발생: {e}")
    
    def set_pump_pwm(self, value):
        """AIO 펌프 PWM 값 설정"""
        if value < self.min_pump_pwm:
            value = self.min_pump_pwm
        elif value > self.max_pump_pwm:
            value = self.max_pump_pwm
        
        value = int(value)  # 정수 값 확인
        
        try:
            # PWM을 수동 모드로 설정 (1 = 수동 제어)
            if os.path.exists(self.pump_enable):
                with open(self.pump_enable, 'w') as f:
                    f.write("1")
            
            # PWM 값 설정
            with open(self.pump_device, 'w') as f:
                f.write(str(value))
            
            logger.info(f"AIO 펌프 PWM 값이 {value}으로 설정되었습니다 ({int(value/255*100)}% 속도)")
            self.current_pump_pwm = value
            return True
        
        except Exception as e:
            logger.error(f"펌프 PWM 값을 설정하는 동안 오류 발생: {e}")
            return False
    
    def set_fan_pwm(self, fan_index, value):
        """팬 PWM 값 설정"""
        if value < self.min_fan_pwm:
            value = self.min_fan_pwm
        elif value > self.max_fan_pwm:
            value = self.max_fan_pwm
        
        value = int(value)  # 정수 값 확인
        
        try:
            fan_device = self.fan_devices[fan_index]
            fan_enable = self.fan_enables[fan_index]
            
            # PWM을 수동 모드로 설정 (1 = 수동 제어)
            if os.path.exists(fan_enable):
                with open(fan_enable, 'w') as f:
                    f.write("1")
            
            # PWM 값 설정
            with open(fan_device, 'w') as f:
                f.write(str(value))
            
            logger.info(f"팬 {fan_index + 1} PWM 값이 {value}으로 설정되었습니다 ({int(value/255*100)}% 속도)")
            self.current_fan_pwms[fan_index] = value
            return True
        
        except Exception as e:
            logger.error(f"팬 {fan_index + 1} PWM 값을 설정하는 동안 오류 발생: {e}")
            return False
    
    def adjust_cooling(self, temp):
        """온도에 따라 펌프와 팬 속도 조정"""
        if temp is None:
            return
        
        # 온도에 따른 PWM 조정 값 계산
        if temp > self.temp_threshold:
            # 온도가 임계값을 초과하는 정도에 따라 속도 증가
            excess_temp = temp - self.temp_threshold
            # 초과 온도 5도당 PWM 값 증가
            pwm_increase = (excess_temp // 5 + 1) * self.pwm_step
            
            # 펌프 속도 조정
            new_pump_pwm = min(self.current_pump_pwm + pwm_increase, self.max_pump_pwm)
            if new_pump_pwm > self.current_pump_pwm:
                self.set_pump_pwm(new_pump_pwm)
            
            # 팬 속도 조정 - 더 급격하게 증가 (1.5배)
            fan_increase = int(pwm_increase * 1.5)
            for i in range(len(self.fan_devices)):
                new_fan_pwm = min(self.current_fan_pwms[i] + fan_increase, self.max_fan_pwm)
                if new_fan_pwm > self.current_fan_pwms[i]:
                    self.set_fan_pwm(i, new_fan_pwm)
        
        # 온도가 임계값보다 10도 이상 낮으면 속도 감소
        elif temp < (self.temp_threshold - 10):
            # 펌프 속도 감소
            if self.current_pump_pwm > self.min_pump_pwm:
                new_pump_pwm = max(self.current_pump_pwm - self.pwm_step, self.min_pump_pwm)
                self.set_pump_pwm(new_pump_pwm)
            
            # 팬 속도 감소
            for i in range(len(self.fan_devices)):
                if self.current_fan_pwms[i] > self.min_fan_pwm:
                    new_fan_pwm = max(self.current_fan_pwms[i] - self.pwm_step, self.min_fan_pwm)
                    self.set_fan_pwm(i, new_fan_pwm)
    
    def get_available_pwm_devices(self):
        """시스템에서 사용 가능한 PWM 장치 목록 가져오기"""
        devices = []
        hwmon_dirs = [d for d in os.listdir('/sys/class/hwmon') if d.startswith('hwmon')]
        
        for hwmon in hwmon_dirs:
            hwmon_path = os.path.join('/sys/class/hwmon', hwmon)
            pwm_files = [f for f in os.listdir(hwmon_path) if re.match(r'pwm\d+$', f)]
            
            for pwm in pwm_files:
                devices.append(os.path.join(hwmon_path, pwm))
        
        return devices
    
    def start(self):
        """제어 시작"""
        self.running = True
        logger.info(f"GPU 온도 기반 냉각 시스템 제어 시작")
        logger.info(f"AIO 펌프 PWM 장치: {self.pump_device}")
        
        if self.fan_devices:
            logger.info(f"팬 PWM 장치: {', '.join(self.fan_devices)}")
        
        logger.info(f"온도 임계값: {self.temp_threshold}°C")
        logger.info(f"펌프 PWM 범위: {self.min_pump_pwm}-{self.max_pump_pwm}")
        logger.info(f"팬 PWM 범위: {self.min_fan_pwm}-{self.max_fan_pwm}")
        logger.info(f"업데이트 간격: {self.update_interval}초")
        
        # 원래 설정 백업
        self.backup_original_settings()
        
        # 초기 PWM 값 설정
        self.set_pump_pwm(self.min_pump_pwm)
        for i in range(len(self.fan_devices)):
            self.set_fan_pwm(i, self.min_fan_pwm)
        
        try:
            while self.running:
                temp = self.get_gpu_temp()
                if temp is not None:
                    logger.info(f"현재 GPU 온도: {temp}°C, 펌프 PWM: {self.current_pump_pwm} ({int(self.current_pump_pwm/255*100)}%)")
                    
                    if self.fan_devices:
                        fan_speeds = ", ".join([f"팬{i+1}: {pwm} ({int(pwm/255*100)}%)" for i, pwm in enumerate(self.current_fan_pwms)])
                        logger.info(f"팬 PWM: {fan_speeds}")
                    
                    self.adjust_cooling(temp)
                
                time.sleep(self.update_interval)
        
        except KeyboardInterrupt:
            logger.info("프로그램이 중단되었습니다.")
        finally:
            self.stop()
    
    def stop(self):
        """제어 중지 및 원래 설정 복원"""
        self.running = False
        self.restore_original_settings()
        logger.info("냉각 시스템 제어가 중지되었습니다")

def signal_handler(sig, frame):
    """시그널 핸들러"""
    logger.info("종료 신호를 받았습니다.")
    controller.stop()
    sys.exit(0)

def list_pwm_devices():
    """사용 가능한 PWM 장치 목록 출력"""
    print("사용 가능한 PWM 장치:")
    
    hwmon_dirs = [d for d in os.listdir('/sys/class/hwmon') if d.startswith('hwmon')]
    found = False
    
    for hwmon in hwmon_dirs:
        hwmon_path = os.path.join('/sys/class/hwmon', hwmon)
        
        # hwmon 이름 가져오기
        name = "알 수 없음"
        try:
            with open(os.path.join(hwmon_path, 'name'), 'r') as f:
                name = f.read().strip()
        except:
            pass
            
        pwm_files = [f for f in os.listdir(hwmon_path) if re.match(r'pwm\d+$', f)]
        
        if pwm_files:
            found = True
            print(f"\n{hwmon_path} ({name}):")
            
            for pwm in sorted(pwm_files):
                device_path = os.path.join(hwmon_path, pwm)
                
                # PWM 라벨 (있는 경우)
                label = "설명 없음"
                label_file = device_path.replace('pwm', 'pwm_label')
                if os.path.exists(label_file):
                    try:
                        with open(label_file, 'r') as f:
                            label = f.read().strip()
                    except:
                        pass
                
                # 현재 PWM 값
                current = "읽을 수 없음"
                try:
                    with open(device_path, 'r') as f:
                        current = f.read().strip()
                except:
                    pass
                
                # PWM 모드
                mode = "읽을 수 없음"
                enable_file = os.path.basename(device_path) + "_enable"
                enable_path = os.path.join(os.path.dirname(device_path), enable_file)
                
                if os.path.exists(enable_path):
                    try:
                        with open(enable_path, 'r') as f:
                            mode_code = f.read().strip()
                            if mode_code == "0":
                                mode = "자동 제어"
                            elif mode_code == "1":
                                mode = "수동 제어"
                            else:
                                mode = f"알 수 없는 모드: {mode_code}"
                    except:
                        pass
                
                print(f"  {device_path}")
                print(f"    설명: {label}")
                print(f"    현재 값: {current}")
                print(f"    모드: {mode}")
    
    if not found:
        print("사용 가능한 PWM 장치를 찾을 수 없습니다.")
        print("lm-sensors 패키지가 설치되어 있는지 확인하세요: sudo apt install lm-sensors")
        print("다음 명령어로 센서를 검색하세요: sudo sensors-detect")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='GPU 온도 기반 냉각 시스템 제어 프로그램')
    parser.add_argument('--list', action='store_true', help='사용 가능한 PWM 장치 목록 출력')
    parser.add_argument('-p', '--pump', type=str, help='AIO 펌프 PWM 장치 경로 (예: /sys/class/hwmon/hwmon1/pwm2)')
    parser.add_argument('-f', '--fans', type=str, nargs='+', help='팬 PWM 장치 경로 목록 (예: /sys/class/hwmon/hwmon1/pwm1)')
    parser.add_argument('-i', '--interval', type=int, default=5, help='업데이트 간격(초), 기본값: 5')
    parser.add_argument('-t', '--threshold', type=int, default=70, help='온도 임계값(°C), 기본값: 70')
    parser.add_argument('--min-pump', type=int, default=100, help='펌프 최소 PWM 값 (0-255), 기본값: 100')
    parser.add_argument('--max-pump', type=int, default=255, help='펌프 최대 PWM 값 (0-255), 기본값: 255')
    parser.add_argument('--min-fan', type=int, default=80, help='팬 최소 PWM 값 (0-255), 기본값: 80')
    parser.add_argument('--max-fan', type=int, default=255, help='팬 최대 PWM 값 (0-255), 기본값: 255')
    parser.add_argument('-s', '--step', type=int, default=20, help='PWM 조정 단계, 기본값: 20')
    
    args = parser.parse_args()
    
    # PWM 장치 목록 출력 모드
    if args.list:
        list_pwm_devices()
        sys.exit(0)
    
    # AIO 펌프 PWM 장치 경로 확인
    if not args.pump:
        parser.error("AIO 펌프 PWM 장치 경로를 지정해야 합니다. --list 옵션으로 사용 가능한 장치를 확인하세요.")
    
    # 관리자 권한 확인
    if os.geteuid() != 0:
        logger.error("이 스크립트는 관리자 권한으로 실행해야 합니다.")
        logger.error("sudo python3 gpu_cooling_controller.py 명령으로 다시 실행해 주세요.")
        sys.exit(1)
    
    # 시그널 핸들러 등록
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 컨트롤러 초기화 및 시작
    controller = GPUCoolingController(
        pump_device=args.pump,
        fan_devices=args.fans,
        update_interval=args.interval,
        temp_threshold=args.threshold,
        min_pump_pwm=args.min_pump,
        max_pump_pwm=args.max_pump,
        min_fan_pwm=args.min_fan,
        max_fan_pwm=args.max_fan,
        pwm_step=args.step
    )
    
    controller.start()