#!/usr/bin/env python3
import subprocess
import time
import logging
import argparse
import os
import signal
import sys
import re

# Logging setup
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
        Initialize GPU temperature-based cooling system controller
        
        Args:
            pump_device: AIO pump PWM device path (e.g., '/sys/class/hwmon/hwmon1/pwm2')
            fan_devices: List of fan PWM device paths (e.g., ['/sys/class/hwmon/hwmon1/pwm1'])
            update_interval: Update interval (seconds)
            temp_threshold: Temperature threshold to start increasing pump/fan speed (°C)
            min_pump_pwm: Minimum pump PWM value (0-255)
            max_pump_pwm: Maximum pump PWM value (0-255)
            min_fan_pwm: Minimum fan PWM value (0-255)
            max_fan_pwm: Maximum fan PWM value (0-255)
            pwm_step: PWM adjustment step
        """
        self.pump_device = pump_device
        self.fan_devices = fan_devices or []
        
        # Extract pwm_enable file path from PWM device path
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
        
        # Check requirements
        self._check_requirements()
        
    def _check_requirements(self):
        """Check required tools and access permissions"""
        try:
            # Check nvidia-smi
            subprocess.run(["nvidia-smi"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            
            # Check PWM device existence
            devices_to_check = [self.pump_device] + self.fan_devices
            
            for device in devices_to_check:
                if not os.path.exists(device):
                    logger.error(f"PWM device not found: {device}")
                    logger.error("Please check if you specified the correct PWM device path.")
                    sys.exit(1)
                    
                # Check write permissions
                if not os.access(device, os.W_OK):
                    logger.error(f"No write permission for PWM device: {device}")
                    logger.error("Please run this script with root privileges.")
                    sys.exit(1)
                
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error("nvidia-smi is not installed or GPU not found.")
            sys.exit(1)
    
    def get_gpu_temp(self):
        """Get GPU temperature using nvidia-smi"""
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
                logger.error("Unable to read GPU temperature.")
                return None
            
            # Return the highest temperature if multiple GPUs
            return max(temps)
        
        except subprocess.CalledProcessError as e:
            logger.error(f"Error while getting GPU temperature: {e}")
            return None
    
    def backup_original_settings(self):
        """Backup original PWM settings"""
        try:
            # Backup pump settings
            if os.path.exists(self.pump_enable):
                with open(self.pump_enable, 'r') as f:
                    self.original_settings[self.pump_enable] = f.read().strip()
            
            with open(self.pump_device, 'r') as f:
                self.original_settings[self.pump_device] = f.read().strip()
            
            # Backup fan settings
            for i, fan_device in enumerate(self.fan_devices):
                with open(fan_device, 'r') as f:
                    self.original_settings[fan_device] = f.read().strip()
                
                fan_enable = self.fan_enables[i]
                if os.path.exists(fan_enable):
                    with open(fan_enable, 'r') as f:
                        self.original_settings[fan_enable] = f.read().strip()
                        
            logger.info(f"Original PWM settings backup completed")
        except Exception as e:
            logger.error(f"Error occurred while backing up original settings: {e}")
    
    def restore_original_settings(self):
        """Restore original PWM settings"""
        try:
            for device, value in self.original_settings.items():
                try:
                    with open(device, 'w') as f:
                        f.write(value)
                except Exception as e:
                    logger.error(f"Error occurred while restoring {device} settings: {e}")
            
            logger.info("Original PWM settings have been restored.")
        except Exception as e:
            logger.error(f"Error occurred while restoring settings: {e}")
    
    def set_pump_pwm(self, value):
        """Set AIO pump PWM value"""
        if value < self.min_pump_pwm:
            value = self.min_pump_pwm
        elif value > self.max_pump_pwm:
            value = self.max_pump_pwm
        
        value = int(value)  # Ensure integer value
        
        try:
            # Set PWM to manual mode (1 = manual control)
            if os.path.exists(self.pump_enable):
                with open(self.pump_enable, 'w') as f:
                    f.write("1")
            
            # Set PWM value
            with open(self.pump_device, 'w') as f:
                f.write(str(value))
            
            logger.info(f"AIO pump PWM value set to {value} ({int(value/255*100)}% speed)")
            self.current_pump_pwm = value
            return True
        
        except Exception as e:
            logger.error(f"Error occurred while setting pump PWM value: {e}")
            return False
    
    def set_fan_pwm(self, fan_index, value):
        """Set fan PWM value"""
        if value < self.min_fan_pwm:
            value = self.min_fan_pwm
        elif value > self.max_fan_pwm:
            value = self.max_fan_pwm
        
        value = int(value)  # Ensure integer value
        
        try:
            fan_device = self.fan_devices[fan_index]
            fan_enable = self.fan_enables[fan_index]
            
            # Set PWM to manual mode (1 = manual control)
            if os.path.exists(fan_enable):
                with open(fan_enable, 'w') as f:
                    f.write("1")
            
            # Set PWM value
            with open(fan_device, 'w') as f:
                f.write(str(value))
            
            logger.info(f"Fan {fan_index + 1} PWM value set to {value} ({int(value/255*100)}% speed)")
            self.current_fan_pwms[fan_index] = value
            return True
        
        except Exception as e:
            logger.error(f"Error occurred while setting fan {fan_index + 1} PWM value: {e}")
            return False
    
    def adjust_cooling(self, temp):
        """Adjust pump and fan speeds based on temperature"""
        if temp is None:
            return
        
        # Calculate PWM adjustment value based on temperature
        if temp > self.temp_threshold:
            # Increase speed based on how much the temperature exceeds the threshold
            excess_temp = temp - self.temp_threshold
            # Increase PWM value for every 5 degrees above threshold
            pwm_increase = (excess_temp // 5 + 1) * self.pwm_step
            
            # Adjust pump speed
            new_pump_pwm = min(self.current_pump_pwm + pwm_increase, self.max_pump_pwm)
            if new_pump_pwm > self.current_pump_pwm:
                self.set_pump_pwm(new_pump_pwm)
            
            # Adjust fan speed - increase more aggressively (1.5x)
            fan_increase = int(pwm_increase * 1.5)
            for i in range(len(self.fan_devices)):
                new_fan_pwm = min(self.current_fan_pwms[i] + fan_increase, self.max_fan_pwm)
                if new_fan_pwm > self.current_fan_pwms[i]:
                    self.set_fan_pwm(i, new_fan_pwm)
        
        # Decrease speed if temperature is 10 degrees below threshold
        elif temp < (self.temp_threshold - 10):
            # Decrease pump speed
            if self.current_pump_pwm > self.min_pump_pwm:
                new_pump_pwm = max(self.current_pump_pwm - self.pwm_step, self.min_pump_pwm)
                self.set_pump_pwm(new_pump_pwm)
            
            # Decrease fan speed
            for i in range(len(self.fan_devices)):
                if self.current_fan_pwms[i] > self.min_fan_pwm:
                    new_fan_pwm = max(self.current_fan_pwms[i] - self.pwm_step, self.min_fan_pwm)
                    self.set_fan_pwm(i, new_fan_pwm)
    
    def get_available_pwm_devices(self):
        """Get a list of available PWM devices in the system"""
        devices = []
        hwmon_dirs = [d for d in os.listdir('/sys/class/hwmon') if d.startswith('hwmon')]
        
        for hwmon in hwmon_dirs:
            hwmon_path = os.path.join('/sys/class/hwmon', hwmon)
            pwm_files = [f for f in os.listdir(hwmon_path) if re.match(r'pwm\d+$', f)]
            
            for pwm in pwm_files:
                devices.append(os.path.join(hwmon_path, pwm))
        
        return devices
    
    def start(self):
        """Start the control"""
        self.running = True
        logger.info(f"Starting GPU temperature-based cooling system control")
        logger.info(f"AIO pump PWM device: {self.pump_device}")
        
        if self.fan_devices:
            logger.info(f"Fan PWM devices: {', '.join(self.fan_devices)}")
        
        logger.info(f"Temperature threshold: {self.temp_threshold}°C")
        logger.info(f"Pump PWM range: {self.min_pump_pwm}-{self.max_pump_pwm}")
        logger.info(f"Fan PWM range: {self.min_fan_pwm}-{self.max_fan_pwm}")
        logger.info(f"Update interval: {self.update_interval} seconds")
        
        # Backup original settings
        self.backup_original_settings()
        
        # Set initial PWM values
        self.set_pump_pwm(self.min_pump_pwm)
        for i in range(len(self.fan_devices)):
            self.set_fan_pwm(i, self.min_fan_pwm)
        
        try:
            while self.running:
                temp = self.get_gpu_temp()
                if temp is not None:
                    logger.info(f"Current GPU temperature: {temp}°C, Pump PWM: {self.current_pump_pwm} ({int(self.current_pump_pwm/255*100)}%)")
                    
                    if self.fan_devices:
                        fan_speeds = ", ".join([f"Fan{i+1}: {pwm} ({int(pwm/255*100)}%)" for i, pwm in enumerate(self.current_fan_pwms)])
                        logger.info(f"Fan PWM: {fan_speeds}")
                    
                    self.adjust_cooling(temp)
                
                time.sleep(self.update_interval)
        
        except KeyboardInterrupt:
            logger.info("Program interrupted.")
        finally:
            self.stop()
    
    def stop(self):
        """Stop the control and restore original settings"""
        self.running = False
        self.restore_original_settings()
        logger.info("Cooling system control has been stopped")

def signal_handler(sig, frame):
    """Signal handler"""
    logger.info("Received termination signal.")
    controller.stop()
    sys.exit(0)

def list_pwm_devices():
    """List available PWM devices"""
    print("Available PWM devices:")
    
    hwmon_dirs = [d for d in os.listdir('/sys/class/hwmon') if d.startswith('hwmon')]
    found = False
    
    for hwmon in hwmon_dirs:
        hwmon_path = os.path.join('/sys/class/hwmon', hwmon)
        
        # Get hwmon name
        name = "Unknown"
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
                
                # PWM label (if available)
                label = "No description"
                label_file = device_path.replace('pwm', 'pwm_label')
                if os.path.exists(label_file):
                    try:
                        with open(label_file, 'r') as f:
                            label = f.read().strip()
                    except:
                        pass
                
                # Current PWM value
                current = "Unable to read"
                try:
                    with open(device_path, 'r') as f:
                        current = f.read().strip()
                except:
                    pass
                
                # PWM mode
                mode = "Unable to read"
                enable_file = os.path.basename(device_path) + "_enable"
                enable_path = os.path.join(os.path.dirname(device_path), enable_file)
                
                if os.path.exists(enable_path):
                    try:
                        with open(enable_path, 'r') as f:
                            mode_code = f.read().strip()
                            if mode_code == "0":
                                mode = "Automatic control"
                            elif mode_code == "1":
                                mode = "Manual control"
                            else:
                                mode = f"Unknown mode: {mode_code}"
                    except:
                        pass
                
                print(f"  {device_path}")
                print(f"    Description: {label}")
                print(f"    Current value: {current}")
                print(f"    Mode: {mode}")
    
    if not found:
        print("No available PWM devices found.")
        print("Make sure lm-sensors package is installed: sudo apt install lm-sensors")
        print("Search for sensors with: sudo sensors-detect")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='GPU temperature-based cooling system control program')
    parser.add_argument('--list', action='store_true', help='List available PWM devices')
    parser.add_argument('-p', '--pump', type=str, help='AIO pump PWM device path (e.g., /sys/class/hwmon/hwmon1/pwm2)')
    parser.add_argument('-f', '--fans', type=str, nargs='+', help='List of fan PWM device paths (e.g., /sys/class/hwmon/hwmon1/pwm1)')
    parser.add_argument('-i', '--interval', type=int, default=5, help='Update interval (seconds), default: 5')
    parser.add_argument('-t', '--threshold', type=int, default=70, help='Temperature threshold (°C), default: 70')
    parser.add_argument('--min-pump', type=int, default=100, help='Minimum pump PWM value (0-255), default: 100')
    parser.add_argument('--max-pump', type=int, default=255, help='Maximum pump PWM value (0-255), default: 255')
    parser.add_argument('--min-fan', type=int, default=80, help='Minimum fan PWM value (0-255), default: 80')
    parser.add_argument('--max-fan', type=int, default=255, help='Maximum fan PWM value (0-255), default: 255')
    parser.add_argument('-s', '--step', type=int, default=20, help='PWM adjustment step, default: 20')
    
    args = parser.parse_args()
    
    # PWM device listing mode
    if args.list:
        list_pwm_devices()
        sys.exit(0)
    
    # Check AIO pump PWM device path
    if not args.pump:
        parser.error("You must specify an AIO pump PWM device path. Use the --list option to see available devices.")
    
    # Check administrator privileges
    if os.geteuid() != 0:
        logger.error("This script must be run with administrator privileges.")
        logger.error("Please run with: sudo python3 gpu_cooling_controller.py")
        sys.exit(1)
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Initialize and start the controller
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