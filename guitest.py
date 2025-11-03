# main_gui.py (v4.5.4 - 简体中文版)

import sys
import os
import time
import zipfile
import socket
import subprocess
import traceback
from multiprocessing.connection import Client
from PIL import Image

from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
                             QLabel, QLineEdit, QPushButton, QPlainTextEdit, QDoubleSpinBox, QScrollArea)
from PyQt5.QtCore import QThread, QObject, pyqtSignal, pyqtSlot, Qt
from PyQt5.QtGui import QPainter, QPixmap, QColor

try:
    from pywinauto.application import Application

    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False
    print("警告：未找到 pywinauto 库，无法控制光引擎软件。请使用 'pip install pywinauto' 安装。")


# --- 1. 配置设定 ---
class PrintConfig:
    ZIP_FILE_PATH = "layers.zip"
    CONTROLLER_EXE_PATH = "Full-HD UV LE Controller v2.1.exe"
    TEMP_EXTRACT_DIR = "temp_layers"
    BLACK_IMAGE_PATH = os.path.join(TEMP_EXTRACT_DIR, "black.png")
    PROJECTOR_VIEW_SCRIPT = "projector_view.py"
    PROJECTOR_MONITOR_INDEX = 1
    ESP32_IP_ADDRESS = "10.10.17.102"  # 请替换为您的 ESP32 IP
    ESP32_PORT = 8899
    SOCKET_TIMEOUT = 60.0

    # 轴参数
    Z_PULSE_PER_REV = 12800.0;
    Z_LEAD = 5.0;
    Z_PEEL_SPEED = 10.0;
    Z_JOG_SPEED = 10.0
    A_PULSE_PER_REV = 12800.0;
    A_LEAD = 75.0;
    A_WIPE_SPEED_FAST = 80.0;
    A_WIPE_SPEED_SLOW = 20.0;
    A_JOG_SPEED = 40.0
    B_PULSE_PER_REV = 3200.0;
    B_LEAD = 1.0;
    B_JOG_SPEED = 5.0
    C_PULSE_PER_REV = 12800.0;
    C_LEAD = 5.0;
    C_JOG_DISTANCE = 10.0;
    C_JOG_SPEED = 20.0

    # 打印参数
    NORMAL_EXPOSURE_TIME_S = 2.5
    FIRST_LAYER_EXPOSURE_TIME_S = 5.0
    TRANSITION_LAYERS = 5

    # 获取当前脚本文件所在的绝对目录
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    # 使用绝对路径拼接 (使用 .png)
    BACKGROUND_IMAGE_PATH = os.path.join(SCRIPT_DIR, "preview.png")


# --- 2. 后端通信与控制类 ---

class MotionController:
    """与 ESP32 进行 TCP 通信"""

    def __init__(self, host, port, timeout=PrintConfig.SOCKET_TIMEOUT):
        self.host = host;
        self.port = port;
        self.timeout = timeout;
        self.sock = None;
        self.reader = None;
        self._is_connected = False

    def connect(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM);
            self.sock.settimeout(self.timeout);
            self.sock.connect((self.host, self.port));
            self.reader = self.sock.makefile('r');
            self._is_connected = True;
            return True, "连接成功"
        except socket.timeout:
            self._is_connected = False; return False, f"连接超时 ({self.timeout}s)"
        except Exception as e:
            self._is_connected = False; return False, f"连接失败: {e}"

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None;
        self.reader = None;
        self._is_connected = False

    def is_connected(self):
        return self._is_connected

    def send_command(self, cmd):
        if not self.is_connected(): return False, "未连接"
        try:
            full_cmd = cmd + "\n";
            self.sock.sendall(full_cmd.encode());
            response = self.reader.readline().strip()
            if "OK" in response or "DONE" in response:
                return True, response
            else:
                return False, response
        except socket.timeout:
            self.disconnect(); return False, f"命令 '{cmd}' 超时 ({self.timeout}s)"
        except Exception as e:
            self.disconnect(); return False, f"命令 '{cmd}' 失败: {e}\n{traceback.format_exc()}"

    def config_axis(self, axis, pulse_per_rev, lead):
        return self.send_command(f"CONFIG_AXIS,{axis},{pulse_per_rev},{lead}")

    def config_z_peel(self, params):
        return self.send_command(
            f"CONFIG_Z_PEEL,{params['peel_lift_z1']},{params['peel_return_z2']},{params['z_speed_down']},{params['z_speed_up']}")

    def config_a_wipe(self, params):
        return self.send_command(f"CONFIG_A_WIPE,{params['a_fast_speed']},{params['a_slow_speed']}")

    def move_to_next_layer(self):
        return self.send_command("NEXT_LAYER")

    def move_relative(self, axis, distance, speed):
        accel = speed * 2; return self.send_command(f"MOVE_REL,{axis},{distance},{speed},{accel}")


class LightEngineControl:
    """使用 pywinauto 控制光引擎软件"""

    def __init__(self):
        self.app = None;
        self.main_win = None;
        self.led_combo = None;
        self.set_button = None;
        self._is_connected = False

    def connect(self, exe_path, title="Full-HD UV LE Controller v2.1", timeout=10):
        if not PYWINAUTO_AVAILABLE: return False, "pywinauto 库未安装"
        try:
            try:
                self.app = Application(backend="uia").connect(title=title, timeout=5); print("已连接到现有光引擎实例。")
            except Exception:
                print(f"未找到光引擎实例，尝试启动: {exe_path}")
                if not os.path.exists(exe_path): return False, f"光引擎 EXE 未找到: {exe_path}"
                self.app = Application(backend="uia").start(exe_path);
                self.app.window(title=title).wait('ready', timeout=timeout);
                print("光引擎软件已启动。")
            self.main_win = self.app.window(title=title);
            self.main_win.wait('ready', timeout=timeout)
            self.led_combo = self.main_win.child_window(auto_id="ComboBoxLedEnable");
            self.set_button = self.main_win.child_window(auto_id="ButtonSetLedOnOff")
            if not self.led_combo.exists() or not self.set_button.exists(): raise RuntimeError(
                "未能在光引擎窗口中找到 LED 控制下拉框或设置按钮。")
            self._is_connected = True;
            return True, "光引擎连接成功"
        except Exception as e:
            self._is_connected = False; return False, f"连接光引擎失败: {e}\n{traceback.format_exc()}"

    def disconnect(self):
        self.app = None; self.main_win = None; self._is_connected = False

    def is_connected(self):
        return self._is_connected

    def _set_led_state(self, state):
        if not self.is_connected(): return False, "光引擎未连接"
        try:
            self.main_win.set_focus();
            self.led_combo.select(state);
            time.sleep(0.1);
            self.set_button.click();
            time.sleep(0.1);
            return True, f"LED 设置为 {state}"
        except Exception as e:
            return False, f"设置 LED 为 {state} 失败: {e}\n{traceback.format_exc()}"

    def led_on(self):
        return self._set_led_state("On")

    def led_off(self):
        return self._set_led_state("Off")


class ProjectorProcessManager:
    """管理投影仪视图子进程和通信 (早期简化版本, 增加等待时间)"""

    def __init__(self, script_path=PrintConfig.PROJECTOR_VIEW_SCRIPT,
                 monitor_index=PrintConfig.PROJECTOR_MONITOR_INDEX,
                 host='localhost', port=6000, authkey=b'secret-key-for-projector'):
        self.script_path = script_path
        self.monitor_index = monitor_index
        self.address = (host, port)
        self.authkey = authkey
        self.process = None
        self.connection = None
        self._is_running = False

    def start(self):
        try:
            python_exe = sys.executable
            script_full_path = os.path.join(PrintConfig.SCRIPT_DIR, self.script_path)
            if not os.path.exists(script_full_path):
                return False, f"投影脚本未找到: {script_full_path}"

            cmd = [python_exe, script_full_path, str(self.monitor_index),
                   self.address[0], str(self.address[1]), self.authkey.decode()]

            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # 隐藏命令行窗口

            print(f"正在执行命令: {' '.join(cmd)}")
            self.process = subprocess.Popen(cmd, startupinfo=startupinfo)

            print("等待投影进程初始化 (5 秒)...")
            time.sleep(5.0)

            # 检查进程是否立刻退出 (任何退出码都视为失败)
            poll_result = self.process.poll()
            if poll_result is not None:
                raise RuntimeError(f"投影进程启动失败，返回值: {poll_result}")

            # 尝试连接客户端
            print("尝试连接到投影进程...")
            self.connection = Client(self.address, authkey=self.authkey)
            self._is_running = True
            print("投影进程连接成功。")
            return True, "投影进程启动并连接成功"

        except Exception as e:
            self.stop()  # 确保清理
            return False, f"启动或连接投影进程失败: {e}\n{traceback.format_exc()}"

    def stop(self):
        if self.connection:
            try:
                self.connection.send({'command': 'close'})
                self.connection.close()
            except Exception:
                pass
        self.connection = None
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
            except Exception:
                pass
        self.process = None
        self._is_running = False

    def send_command(self, command_dict):
        if not self._is_running or not self.connection: return False, "投影进程未运行或未连接"
        try:
            self.connection.send(command_dict); return True, "指令已发送"
        except Exception as e:
            self.stop(); return False, f"发送指令到投影进程失败: {e}\n{traceback.format_exc()}"

    def show_image(self, image_path):
        return self.send_command({'command': 'show', 'path': image_path})

    def show_black(self):
        return self.send_command({'command': 'show', 'path': PrintConfig.BLACK_IMAGE_PATH})


# --- 3. 后台打印工作线程 ---
class PrintWorker(QObject):
    log_message = pyqtSignal(str);
    error_occurred = pyqtSignal(str);
    finished = pyqtSignal()

    def __init__(self, params):
        super().__init__(); self.params = params; self._is_running = True

    @pyqtSlot()
    def run(self):
        motion_ctrl = None;
        light_engine_ctrl = None;
        projector_mgr = None
        try:
            self.log_message.emit("--- 打印任务初始化 ---");
            black_image_path = self.params['black_image_path']
            projector_mgr = ProjectorProcessManager();
            success, msg = projector_mgr.start();
            self.log_message.emit(msg);
            if not success: raise RuntimeError(msg)
            light_engine_ctrl = LightEngineControl();
            success, msg = light_engine_ctrl.connect(self.params['controller_exe_path']);
            self.log_message.emit(msg);
            if not success: raise RuntimeError(msg)
            motion_ctrl = MotionController(self.params['esp32_ip'], self.params['esp32_port']);
            success, msg = motion_ctrl.connect();
            self.log_message.emit(msg);
            if not success: raise RuntimeError(msg)
            self.log_message.emit(f"正在从 {self.params['zip_path']} 解压缩文件...");
            temp_dir = self.params['temp_dir'];
            if not os.path.exists(temp_dir): os.makedirs(temp_dir)
            with zipfile.ZipFile(self.params['zip_path'], 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            image_files = sorted(
                [f for f in os.listdir(temp_dir) if f.endswith('.png') and os.path.splitext(f)[0].isdigit()],
                key=lambda x: int(os.path.splitext(x)[0]))
            total_layers = len(image_files);
            image_paths = [os.path.join(temp_dir, f) for f in image_files];
            if total_layers == 0: raise RuntimeError(
                "未在压缩包中找到有效的切片文件 (数字.png)"); self.log_message.emit(f"找到 {total_layers} 个切片文件。")

            # --- 核心修改：使用 self.params ---
            self.log_message.emit("正在发送轴配置到 ESP32...");
            s, m = motion_ctrl.config_axis('z', self.params['z_pulse_rev'], self.params['z_lead']);
            self.log_message.emit(f"Z: {m}");
            if not s: raise RuntimeError(f"配置 Z 轴失败: {m}")
            s, m = motion_ctrl.config_axis('a', self.params['a_pulse_rev'], self.params['a_lead']);
            self.log_message.emit(f"A: {m}");
            if not s: raise RuntimeError(f"配置 A 轴失败: {m}")
            s, m = motion_ctrl.config_axis('b', self.params['b_pulse_rev'], self.params['b_lead']);
            self.log_message.emit(f"B: {m}");
            if not s: raise RuntimeError(f"配置 B 轴失败: {m}")
            s, m = motion_ctrl.config_axis('c', self.params['c_pulse_rev'], self.params['c_lead']);
            self.log_message.emit(f"C: {m}");
            if not s: raise RuntimeError(f"配置 C 轴失败: {m}")
            self.log_message.emit("正在发送打印参数到 ESP32...");
            s, m = motion_ctrl.config_z_peel(self.params);
            self.log_message.emit(f"Z Peel: {m}");
            if not s: raise RuntimeError(f"配置 Z 轴剥离失败: {m}")
            s, m = motion_ctrl.config_a_wipe(self.params);
            self.log_message.emit(f"A Wipe: {m}");
            if not s: raise RuntimeError(f"配置 A 轴擦拭失败: {m}")
            # --- 修改结束 ---
            self.log_message.emit("配置发送完成。")

            success, msg = projector_mgr.show_black();
            if not success: raise RuntimeError(f"初始黑屏失败: {msg}")
            self.log_message.emit("--- 所有硬件已初始化，打印循环开始 ---")
            for i, image_path in enumerate(image_paths):
                if not self._is_running: self.log_message.emit("打印任务被用户终止。"); break
                layer_num = i + 1;
                self.log_message.emit(f"\n--- 正在打印第 {layer_num} / {total_layers} 层 ---")
                if layer_num == 1:
                    exposure_time = self.params['first_layer_expo']
                elif layer_num <= self.params['transition_layers']:
                    progress = (layer_num - 1) / (self.params['transition_layers'] - 1); exposure_time = self.params[
                                                                                                             'first_layer_expo'] - (
                                                                                                                     self.params[
                                                                                                                         'first_layer_expo'] -
                                                                                                                     self.params[
                                                                                                                         'normal_expo']) * progress
                else:
                    exposure_time = self.params['normal_expo']
                self.log_message.emit(f"曝光时间: {exposure_time:.2f} 秒")
                success, msg = projector_mgr.show_image(image_path);
                if not success: raise RuntimeError(f"显示切片 {layer_num} 失败: {msg}")
                success, msg = light_engine_ctrl.led_on();
                if not success: raise RuntimeError(f"打开 LED 失败: {msg}")
                time.sleep(exposure_time)
                success, msg = projector_mgr.show_black();
                if not success: self.log_message.emit(f"警告：设置黑屏失败: {msg}")
                success, msg = light_engine_ctrl.led_off();
                if not success: raise RuntimeError(f"关闭 LED 失败: {msg}")
                if layer_num < total_layers:
                    self.log_message.emit("执行层间运动...");
                    success, msg = motion_ctrl.move_to_next_layer();
                    if not success: raise RuntimeError(f"层间运动失败: {msg}")
                    self.log_message.emit("层间运动完成。")
            else:
                self.log_message.emit("\n--- 打印完成！ ---")
        except Exception as e:
            error_msg = f"打印过程中发生错误: {e}\n{traceback.format_exc()}";
            self.log_message.emit(error_msg);
            self.error_occurred.emit(error_msg)
        finally:
            self.log_message.emit("正在关闭所有设备和连接...");
            if projector_mgr: projector_mgr.stop()
            if light_engine_ctrl: light_engine_ctrl.disconnect()
            if motion_ctrl: motion_ctrl.disconnect()
            self.log_message.emit("任务线程已结束。");
            self.finished.emit()

    def stop(self):
        self._is_running = False


# --- 4. PyQt5 主窗口 ---
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.motion_controller = None
        self.worker_thread = None
        self.print_worker = None

        # --- (v4.5.3 修正: 增强 QPixmap 加载) ---

        # 1. 设置变量和默认回退值
        self.image_path = PrintConfig.BACKGROUND_IMAGE_PATH
        self.bg_pixmap = QPixmap()  # 初始化为空 QPixmap
        self.bg_color = QColor(245, 222, 203)  # 设置默认回退背景色

        try:
            # 2. 检查文件是否存在
            if not os.path.exists(self.image_path):
                raise FileNotFoundError(f"未找到背景图片: {self.image_path}")

            # 3. 尝试使用 QPixmap 加载
            self.bg_pixmap = QPixmap(self.image_path)

            # 4. (关键) 检查 QPixmap 是否加载成功
            if self.bg_pixmap.isNull():
                raise RuntimeError(f"QPixmap 无法解码图片 (可能是文件损坏或格式不支持): {self.image_path}")

            # 5. 尝试使用 PIL 加载以获取背景色
            img = Image.open(self.image_path)
            rgb = img.getpixel((0, 0))
            self.bg_color = QColor(rgb[0], rgb[1], rgb[2])

            # 6. 只有 *所有* 步骤都成功，才打印成功日志
            print(f"成功加载背景图片 (Qt 和 PIL): {self.image_path}")

        except Exception as e:
            # 7. 如果任何一步失败，打印警告并使用回退值
            print(f"警告：无法加载背景图片或颜色: {e}")
            self.bg_pixmap = QPixmap()  # 确保 pixmap 为空
            self.bg_color = QColor(245, 222, 203)  # 确保使用回退色

        # --- 修正结束 ---

        self.initUI()
        self.setMinimumSize(800, 600)  # 设置一个最小尺寸

    def initUI(self):
        self.setWindowTitle('四轴 DLP 打印机控制器 v4.5 (简体中文版)')

        # --- GUI 美化: 更改主布局为 QHBoxLayout ---
        main_layout = QHBoxLayout(self)

        # --- GUI 美化: 创建控件容器和滚动区域 ---
        self.controls_widget = QWidget()  # 所有控件的容器
        self.controls_widget.setObjectName("ControlsWidget")
        controls_layout = QVBoxLayout(self.controls_widget)  # 控件容器使用垂直布局

        # --- 将所有原有的 UI 元素添加到 controls_layout ---
        conn_group = QGroupBox("连接设定")
        conn_layout = QHBoxLayout()
        conn_layout.addWidget(QLabel("ESP32 IP:"))
        self.esp32_ip_edit = QLineEdit(PrintConfig.ESP32_IP_ADDRESS)
        conn_layout.addWidget(self.esp32_ip_edit)
        self.connect_button = QPushButton("连接 & 初始化 ESP32")
        self.connect_button.clicked.connect(self.connect_esp32)
        conn_layout.addWidget(self.connect_button)
        conn_group.setLayout(conn_layout)
        controls_layout.addWidget(conn_group)  # 添加到 controls_layout

        params_group = QGroupBox("打印参数设定")
        params_layout = QGridLayout()
        params_layout.addWidget(QLabel("层高 (mm):"), 0, 0)
        self.layer_height_edit = QDoubleSpinBox();
        self.layer_height_edit.setDecimals(3);
        self.layer_height_edit.setValue(0.050)
        params_layout.addWidget(self.layer_height_edit, 0, 1)
        params_layout.addWidget(QLabel("Z 剥离基础距离 (mm):"), 0, 2)
        self.peel_base_dist_edit = QDoubleSpinBox();
        self.peel_base_dist_edit.setValue(5.0)
        params_layout.addWidget(self.peel_base_dist_edit, 0, 3)
        params_layout.addWidget(QLabel("底层曝光 (s):"), 1, 0)
        self.first_expo_edit = QDoubleSpinBox();
        self.first_expo_edit.setValue(PrintConfig.FIRST_LAYER_EXPOSURE_TIME_S)
        params_layout.addWidget(self.first_expo_edit, 1, 1)
        params_layout.addWidget(QLabel("正常曝光 (s):"), 1, 2)
        self.normal_expo_edit = QDoubleSpinBox();
        self.normal_expo_edit.setValue(PrintConfig.NORMAL_EXPOSURE_TIME_S)
        params_layout.addWidget(self.normal_expo_edit, 1, 3)
        params_group.setLayout(params_layout)
        controls_layout.addWidget(params_group)  # 添加到 controls_layout

        speed_group = QGroupBox("速度设定 (mm/s)")
        speed_layout = QGridLayout()
        speed_layout.addWidget(QLabel("Z 轴下移速度:"), 0, 0)
        self.z_speed_down_edit = QDoubleSpinBox();
        self.z_speed_down_edit.setValue(PrintConfig.Z_PEEL_SPEED)
        speed_layout.addWidget(self.z_speed_down_edit, 0, 1)
        speed_layout.addWidget(QLabel("Z 轴上移速度:"), 0, 2)
        self.z_speed_up_edit = QDoubleSpinBox();
        self.z_speed_up_edit.setValue(PrintConfig.Z_PEEL_SPEED)
        speed_layout.addWidget(self.z_speed_up_edit, 0, 3)
        speed_layout.addWidget(QLabel("A 轴擦拭速度 (快):"), 1, 0)
        self.a_speed_fast_edit = QDoubleSpinBox();
        self.a_speed_fast_edit.setValue(PrintConfig.A_WIPE_SPEED_FAST)
        speed_layout.addWidget(self.a_speed_fast_edit, 1, 1)
        speed_layout.addWidget(QLabel("A 轴擦拭速度 (慢):"), 1, 2)
        self.a_speed_slow_edit = QDoubleSpinBox();
        self.a_speed_slow_edit.setValue(PrintConfig.A_WIPE_SPEED_SLOW)
        speed_layout.addWidget(self.a_speed_slow_edit, 1, 3)
        speed_layout.addWidget(QLabel("C 轴恒定速度:"), 2, 0)
        self.c_jog_speed_edit = QDoubleSpinBox();
        self.c_jog_speed_edit.setValue(PrintConfig.C_JOG_SPEED)
        speed_layout.addWidget(self.c_jog_speed_edit, 2, 1)
        speed_group.setLayout(speed_layout)
        controls_layout.addWidget(speed_group)  # 添加到 controls_layout

        self.jog_group = QGroupBox("手动控制")
        jog_layout = QGridLayout()
        jog_layout.addWidget(QLabel("Z 轴距离(mm):"), 0, 0);
        self.z_jog_dist_edit = QDoubleSpinBox();
        self.z_jog_dist_edit.setValue(10.0);
        jog_layout.addWidget(self.z_jog_dist_edit, 0, 1);
        self.z_up_button = QPushButton("Z 轴向上");
        jog_layout.addWidget(self.z_up_button, 0, 2);
        self.z_down_button = QPushButton("Z 轴向下");
        jog_layout.addWidget(self.z_down_button, 0, 3)
        jog_layout.addWidget(QLabel("A 轴距离(mm):"), 1, 0);
        self.a_jog_dist_edit = QDoubleSpinBox();
        self.a_jog_dist_edit.setValue(10.0);
        jog_layout.addWidget(self.a_jog_dist_edit, 1, 1);
        self.a_fwd_button = QPushButton("A 轴向前(Jog)");
        jog_layout.addWidget(self.a_fwd_button, 1, 2);
        self.a_back_button = QPushButton("A 轴向后(Jog)");
        jog_layout.addWidget(self.a_back_button, 1, 3)
        jog_layout.addWidget(QLabel("B 轴距离(mm):"), 2, 0);
        self.b_jog_dist_edit = QDoubleSpinBox();
        self.b_jog_dist_edit.setValue(10.0);
        jog_layout.addWidget(self.b_jog_dist_edit, 2, 1);
        self.b_up_button = QPushButton("B 轴向上 (刮刀)");
        jog_layout.addWidget(self.b_up_button, 2, 2);
        self.b_down_button = QPushButton("B 轴向下 (刮刀)");
        jog_layout.addWidget(self.b_down_button, 2, 3)
        jog_layout.addWidget(QLabel("C 轴距离(mm):"), 3, 0);
        self.c_jog_dist_edit = QDoubleSpinBox();
        self.c_jog_dist_edit.setValue(PrintConfig.C_JOG_DISTANCE);
        jog_layout.addWidget(self.c_jog_dist_edit, 3, 1);
        self.c_up_button = QPushButton("C 轴向上");
        jog_layout.addWidget(self.c_up_button, 3, 2);
        self.c_down_button = QPushButton("C 轴向下");
        jog_layout.addWidget(self.c_down_button, 3, 3)
        self.jog_group.setLayout(jog_layout)
        controls_layout.addWidget(self.jog_group)  # 添加到 controls_layout

        self.z_up_button.clicked.connect(lambda: self.jog_axis('z', 1));
        self.z_down_button.clicked.connect(lambda: self.jog_axis('z', -1));
        self.a_fwd_button.clicked.connect(lambda: self.jog_axis('a', 1));
        self.a_back_button.clicked.connect(lambda: self.jog_axis('a', -1));
        self.b_up_button.clicked.connect(lambda: self.jog_axis('b', 1));
        self.b_down_button.clicked.connect(lambda: self.jog_axis('b', -1));
        self.c_up_button.clicked.connect(lambda: self.jog_axis('c', 1));
        self.c_down_button.clicked.connect(lambda: self.jog_axis('c', -1))

        control_layout = QHBoxLayout()
        self.start_button = QPushButton("开始打印")
        self.start_button.clicked.connect(self.start_print)
        self.stop_button = QPushButton("终止打印")
        self.stop_button.clicked.connect(self.stop_print)
        control_layout.addWidget(self.start_button)
        control_layout.addWidget(self.stop_button)
        controls_layout.addLayout(control_layout)  # 添加到 controls_layout

        self.log_widget = QPlainTextEdit()
        self.log_widget.setReadOnly(True)
        controls_layout.addWidget(self.log_widget)  # 添加到 controls_layout

        # --- GUI 美化: 创建 QScrollArea 并设置控件 ---
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("ControlsScrollArea")
        self.scroll_area.setWidget(self.controls_widget)  # 将 controls_widget 放入滚动区域
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)  # 移除边框

        # --- GUI 美化: 将 QScrollArea 和 弹性空间 添加到主布局 ---
        main_layout.addWidget(self.scroll_area, 2)  # 左侧控件区域，占 2 份
        main_layout.addStretch(1)  # 右侧弹性空间 (用于绘制图像)，占 1 份

        # --- (QSS 样式表) ---
        self.setStyleSheet(f"""
            /* 1. 主窗口本身不绘制背景, 由 paintEvent 处理 */
            MainWindow {{
                background-color: {self.bg_color.name()};
            }}

            /* 2. 滚动区域本身设置为完全透明 */
            QScrollArea#ControlsScrollArea {{
                background-color: transparent;
                border: none;
            }}

            /* 3. 滚动区域的 *内容* (controls_widget) 设置为半透明卡片 */
            QWidget#ControlsWidget {{
                background-color: rgba(255, 255, 255, 0.25); 
                border-radius: 12px;
                padding: 10px; 
            }}

            /* 确保文字清晰 */
            QLabel {{
                background-color: transparent; 
                color: #000000; 
            }}

            /* 4. 使 QGroupBox 透明, 并使边框和标题更清晰 */
            QGroupBox {{
                background-color: transparent;
                border: 1px solid #999999; 
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
                left: 10px;
                color: #111111; 
            }}

            /* 5. 日志窗口样式, 同样增加透明度 */
            QPlainTextEdit {{
                background-color: rgba(245, 245, 245, 0.4);
                border: 1px solid #BBBBBB;
                border-radius: 5px;
                color: #000000; 
            }}

            /* 6. 按钮样式, 确保文字清晰 */
            QPushButton {{
                background-color: #E0E0E0;
                border: 1px solid #ADADAD;
                padding: 5px 10px;
                border-radius: 4px;
                color: #000000; 
            }}
            QPushButton:hover {{
                background-color: #E8E8E8;
            }}
            QPushButton:pressed {{
                background-color: #D0D0D0;
            }}
            QPushButton:disabled {{
                background-color: #F0F0F0;
                color: #AAA;
            }}
            /* 特殊按钮颜色 */
            QPushButton[text="开始打印"] {{
                background-color: #D5E8D4; /* 绿色 */
                border: 1px solid #82B366;
            }}
            QPushButton[text="终止打印"] {{
                background-color: #F8CECC; /* 红色 */
                border: 1px solid #B85450;
            }}

        """)
        # --- 美化结束 ---

        self.update_ui_state(connected=False, printing=False)

    # --- GUI 美化: 新增 paintEvent ---
    def paintEvent(self, event):
        """ 重写绘制事件以绘制背景色和图片 """
        painter = QPainter(self)

        # 1. 绘制背景色
        painter.fillRect(self.rect(), self.bg_color)

        # 2. 绘制图片
        if not self.bg_pixmap.isNull():
            # 按窗口高度缩放, 保持宽高比, 使用平滑变换
            scaled_pix = self.bg_pixmap.scaledToHeight(self.height(), Qt.SmoothTransformation)

            # 计算绘制位置 (使其靠右, 并垂直居中)
            x = self.width() - scaled_pix.width()
            y = (self.height() - scaled_pix.height()) / 2

            painter.drawPixmap(int(x), int(y), scaled_pix)

    # --- GUI 美化: 新增 resizeEvent ---
    def resizeEvent(self, event):
        """ 窗口大小改变时, 强制重绘 (触发 paintEvent) """
        self.update()
        super().resizeEvent(event)

    def update_ui_state(self, connected, printing):
        self.connect_button.setEnabled(not printing)
        self.jog_group.setEnabled(connected and not printing)
        self.start_button.setEnabled(connected and not printing)
        self.stop_button.setEnabled(printing)

        # 仅禁用非 jog_group 的 QGroupBox
        for widget in self.controls_widget.findChildren(QGroupBox):  # 在 controls_widget 中查找
            if widget != self.jog_group:
                widget.setEnabled(not printing)

        self.esp32_ip_edit.setEnabled(not printing)

    def log(self, message):
        if isinstance(message, str):
            if QThread.currentThread() != self.thread(): pass
            self.log_widget.appendPlainText(message)
            self.log_widget.ensureCursorVisible()
            QApplication.processEvents()  # 保持响应

    def get_params(self):
        peel_base = self.peel_base_dist_edit.value();
        layer_height = self.layer_height_edit.value()
        return {'esp32_ip': self.esp32_ip_edit.text(), 'esp32_port': PrintConfig.ESP32_PORT,
                'zip_path': PrintConfig.ZIP_FILE_PATH, 'temp_dir': PrintConfig.TEMP_EXTRACT_DIR,
                'black_image_path': PrintConfig.BLACK_IMAGE_PATH,
                'controller_exe_path': PrintConfig.CONTROLLER_EXE_PATH,
                'monitor_index': PrintConfig.PROJECTOR_MONITOR_INDEX, 'first_layer_expo': self.first_expo_edit.value(),
                'normal_expo': self.normal_expo_edit.value(), 'transition_layers': PrintConfig.TRANSITION_LAYERS,
                'z_pulse_rev': PrintConfig.Z_PULSE_PER_REV, 'z_lead': PrintConfig.Z_LEAD,
                'a_pulse_rev': PrintConfig.A_PULSE_PER_REV, 'a_lead': PrintConfig.A_LEAD,
                'b_pulse_rev': PrintConfig.B_PULSE_PER_REV, 'b_lead': PrintConfig.B_LEAD,
                'c_pulse_rev': PrintConfig.C_PULSE_PER_REV, 'c_lead': PrintConfig.C_LEAD,
                'peel_lift_z1': peel_base + layer_height, 'peel_return_z2': peel_base,
                'z_speed_down': self.z_speed_down_edit.value(), 'z_speed_up': self.z_speed_up_edit.value(),
                'a_fast_speed': self.a_speed_fast_edit.value(), 'a_slow_speed': self.a_speed_slow_edit.value(),
                'c_jog_speed': self.c_jog_speed_edit.value(), 'z_jog_speed': PrintConfig.Z_JOG_SPEED,
                'a_jog_speed': PrintConfig.A_JOG_SPEED, 'b_jog_speed': PrintConfig.B_JOG_SPEED, }

    @pyqtSlot()
    def connect_esp32(self):
        if self.motion_controller and self.motion_controller.is_connected():
            self.motion_controller.disconnect();
            self.log("已断开与 ESP32 的连接。");
            self.connect_button.setText("连接 & 初始化 ESP32");
            self.update_ui_state(connected=False, printing=False);
            return
        params = self.get_params();
        self.log(f"正在连接并初始化 ESP32 于 {params['esp32_ip']}...");
        self.motion_controller = MotionController(params['esp32_ip'], params['esp32_port']);
        success, msg = self.motion_controller.connect();
        self.log(msg)
        if success:
            try:
                self.log("发送轴配置...");
                s, m = self.motion_controller.config_axis('z', params['z_pulse_rev'], params['z_lead']);
                self.log(f"Z: {m}");
                if not s: raise RuntimeError(f"配置 Z 失败: {m}")
                s, m = self.motion_controller.config_axis('a', params['a_pulse_rev'], params['a_lead']);
                self.log(f"A: {m}");
                if not s: raise RuntimeError(f"配置 A 失败: {m}")
                s, m = self.motion_controller.config_axis('b', params['b_pulse_rev'], params['b_lead']);
                self.log(f"B: {m}");
                if not s: raise RuntimeError(f"配置 B 失败: {m}")
                s, m = self.motion_controller.config_axis('c', params['c_pulse_rev'], params['c_lead']);
                self.log(f"C: {m}");
                if not s: raise RuntimeError(f"配置 C 失败: {m}")
                self.log("发送打印参数...");
                s, m = self.motion_controller.config_z_peel(params);
                self.log(f"Z Peel: {m}");
                if not s: raise RuntimeError(f"配置 Z Peel 失败: {m}")
                s, m = self.motion_controller.config_a_wipe(params);
                self.log(f"A Wipe: {m}");
                if not s: raise RuntimeError(f"配置 A Wipe 失败: {m}")
                self.log("ESP32 初始化成功。");
                self.connect_button.setText("断开连接");
                self.update_ui_state(connected=True, printing=False)
            except Exception as e:
                err_msg = f"初始化 ESP32 失败: {e}\n{traceback.format_exc()}";
                self.log(err_msg);
                self.motion_controller.disconnect();
                self.update_ui_state(connected=False, printing=False)
        else:
            self.update_ui_state(connected=False, printing=False)

    @pyqtSlot()
    def start_print(self):
        if self.worker_thread and self.worker_thread.isRunning(): self.log("错误：打印任务已在运行。"); return
        if not self.motion_controller or not self.motion_controller.is_connected(): self.log(
            "错误：请先连接到 ESP32。"); return
        self.log_widget.clear();
        params = self.get_params();
        self.log("准备开始打印任务...")
        self.worker_thread = QThread(self);
        self.print_worker = PrintWorker(params);
        self.print_worker.moveToThread(self.worker_thread)
        self.print_worker.log_message.connect(self.log);
        self.print_worker.error_occurred.connect(self.on_worker_error);
        self.print_worker.finished.connect(self.on_worker_finished);
        self.worker_thread.started.connect(self.print_worker.run);
        self.worker_thread.finished.connect(self.worker_thread.deleteLater);
        self.print_worker.finished.connect(self.print_worker.deleteLater)
        self.worker_thread.start();
        self.update_ui_state(connected=True, printing=True)

    @pyqtSlot()
    def stop_print(self):
        if self.print_worker: self.log("正在发送终止信号..."); self.print_worker.stop(); self.stop_button.setEnabled(
            False)

    @pyqtSlot()
    def on_worker_finished(self):
        self.worker_thread = None;
        self.print_worker = None;
        self.update_ui_state(connected=(self.motion_controller is not None and self.motion_controller.is_connected()),
                             printing=False)

    @pyqtSlot(str)
    def on_worker_error(self, error_msg):
        self.worker_thread = None;
        self.print_worker = None;
        self.update_ui_state(connected=(self.motion_controller is not None and self.motion_controller.is_connected()),
                             printing=False)

    def jog_axis(self, axis, direction):
        if not self.motion_controller or not self.motion_controller.is_connected(): self.log(
            "错误：请先连接到 ESP32。"); return
        dist_edit_map = {'z': self.z_jog_dist_edit, 'a': self.a_jog_dist_edit, 'b': self.b_jog_dist_edit,
                         'c': self.c_jog_dist_edit}
        try:
            params = self.get_params();
            dist = dist_edit_map[axis].value() * direction;
            speed = params.get(f'{axis}_jog_speed', 10.0);
            self.log(f"手动控制: {axis} 轴移动 {dist:.2f} mm @ {speed:.1f} mm/s...")
            self.jog_group.setEnabled(False);
            QApplication.processEvents()
            success, msg = self.motion_controller.move_relative(axis, dist, speed);
            self.log(f"手动控制完成: {msg}")
        except Exception as e:
            self.log(f"手动控制出错: {e}\n{traceback.format_exc()}")
        finally:
            if self.motion_controller and self.motion_controller.is_connected(): self.jog_group.setEnabled(True)

    def closeEvent(self, event):
        self.log("正在关闭应用程序...")
        if self.worker_thread and self.worker_thread.isRunning():
            self.log("检测到打印任务仍在运行，正在尝试停止...");
            self.stop_print()
            if not self.worker_thread.wait(5000): self.log("警告：后台任务未能及时结束，可能需要强制退出。")
        if self.motion_controller: self.motion_controller.disconnect()
        event.accept()


# --- 5. 应用程序入口 ---
if __name__ == '__main__':
    # 检查所需文件
    if not os.path.exists(PrintConfig.BACKGROUND_IMAGE_PATH):
        # 现在会打印绝对路径，方便调试
        print(f"错误：背景图片 '{PrintConfig.BACKGROUND_IMAGE_PATH}' 未找到！")

    temp_dir = PrintConfig.TEMP_EXTRACT_DIR
    black_image_path = PrintConfig.BLACK_IMAGE_PATH
    if not os.path.exists(temp_dir): os.makedirs(temp_dir)
    if not os.path.exists(black_image_path):
        try:
            print(f"'{black_image_path}' 未找到，正在创建...");
            black_img = Image.new('RGB', (1920, 1080), 'black');
            black_img.save(black_image_path);
            print("创建成功。")
        except Exception as e:
            print(f"错误：无法创建 black.png 文件: {e}")

    app = QApplication(sys.argv)
    ex = MainWindow()
    ex.show()
    sys.exit(app.exec_())