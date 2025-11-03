# main.py - v2.5.2 (DIR 建立时间增加到 50ms)

import machine
import time
import uasyncio
import sh1106
import sys

# --- 1. 自定义异步队列类 ---
class AsyncQueue:
    def __init__(self): self.items = []; self.event = uasyncio.Event()
    async def put(self, item): self.items.append(item); self.event.set()
    async def get(self):
        while not self.items: await self.event.wait()
        item = self.items.pop(0)
        if not self.items: self.event.clear()
        return item

# --- 2. 硬件设定区 (已移除 ENA 引脚) ---
Z_STEP_PIN, Z_DIR_PIN = 26, 25
A_STEP_PIN, A_DIR_PIN = 14, 27  # A 轴 DIR 已改为 27
B_STEP_PIN, B_DIR_PIN = 19, 18
C_STEP_PIN, C_DIR_PIN = 17, 16
A_LIMIT_HOME_PIN = 32
A_LIMIT_END_PIN = 33

# --- 3. OLED 显示设定 ---
I2C_SCL_PIN = 22; I2C_SDA_PIN = 21; OLED_WIDTH = 128; OLED_HEIGHT = 64
OLED_AVAILABLE = False
try:
    i2c = machine.I2C(0, scl=machine.Pin(I2C_SCL_PIN), sda=machine.Pin(I2C_SDA_PIN), freq=400000)
    display = sh1106.SH1106_I2C(OLED_WIDTH, OLED_HEIGHT, i2c, addr=0x3c)
    display.fill(0); display.text('4-Axis System', 0, 0); display.text('Booting...', 0, 10); display.show()
    OLED_AVAILABLE = True
except Exception as e: print(f"OLED Init Failed: {e}")

def update_display(line1="", line2="", line3="", line4=""):
    if OLED_AVAILABLE:
        try:
            display.fill(0); display.text(line1, 0, 0); display.text(line2, 0, 10); display.text(line3, 0, 20); display.text(line4, 0, 30); display.show()
        except OSError as e: print(f"OLED Update Failed: {e}. Operation will continue.")

# --- 4. 步进马达驱动类 (已移除 ENA 逻辑) ---
class Stepper:
    def __init__(self, step_pin_num, dir_pin_num, is_dm_driver=False):
        self.step_pin_num = step_pin_num; self.step = machine.Pin(self.step_pin_num, machine.Pin.OUT); self.dir = machine.Pin(dir_pin_num, machine.Pin.OUT); self.use_ena = not is_dm_driver
        self.steps_per_mm = 200.0; self.pwm = machine.PWM(self.step, freq=1, duty=0); self.dir.value(0); self.disable()
    
    def enable(self):
        if self.use_ena: pass
    
    def disable(self):
        if self.use_ena: pass
    
    async def move_rel(self, distance_mm, speed_mm_s, accel_mm_s2):
        if self.steps_per_mm <= 0 or speed_mm_s <= 0: return
        total_steps = int(abs(distance_mm) * self.steps_per_mm)
        if total_steps == 0: return
        self.enable()
        if self.use_ena: await uasyncio.sleep_ms(5)
        self.dir.value(1 if distance_mm < 0 else 0)
        
        # --- (已修改) DIR 建立时间 ---
        # 再次增加延时以确保驱动器有足够的时间在 STEP 脉冲前识别 DIR 信号
        await uasyncio.sleep_ms(50) # t2 delay (原为 20ms)
        
        frequency = speed_mm_s * self.steps_per_mm
        if frequency > 40000: frequency = 40000
        if frequency <= 0: return
        duration_s = total_steps / frequency
        try:
            self.pwm.freq(int(frequency)); self.pwm.duty(512); await uasyncio.sleep_ms(int(duration_s * 1000))
        finally:
            self.pwm.duty(0); self.step.value(0)
    
    async def move_until_trigger(self, is_forward, speed_mm_s, trigger_pin, timeout_ms=30000):
        if self.steps_per_mm <= 0 or speed_mm_s <= 0: return False
        self.enable()
        if self.use_ena: await uasyncio.sleep_ms(5)
        self.dir.value(0 if is_forward else 1)

        # --- (已修改) DIR 建立时间 ---
        # 再次增加延时以确保驱动器有足够的时间在 STEP 脉冲前识别 DIR 信号
        await uasyncio.sleep_ms(50) # t2 delay (原为 20ms)
        
        frequency = speed_mm_s * self.steps_per_mm
        if frequency > 40000: frequency = 40000
        if frequency <= 0: return False
        self.pwm.freq(int(frequency))
        self.pwm.duty(512)
        start_time = time.ticks_ms()
        triggered = False
        try:
            while time.ticks_diff(time.ticks_ms(), start_time) < timeout_ms:
                if trigger_pin.value() == 0:
                    triggered = True
                    print(f"Triggered on pin {trigger_pin}, stopping motor.")
                    break
                await uasyncio.sleep_ms(1)
            if not triggered:
                print(f"ERROR: Timeout waiting for trigger on pin {trigger_pin} after {timeout_ms}ms!")
                update_display("Status: ERROR", "Limit Timeout", f"Pin: {trigger_pin}")
                return False
            return True
        finally:
            self.pwm.duty(0)
            self.step.value(0)


# --- 5. 全域變數 (已移除 ENA 引脚) ---
command_queue = AsyncQueue()
steppers = {
    'z': Stepper(Z_STEP_PIN, Z_DIR_PIN, is_dm_driver=True),
    'a': Stepper(A_STEP_PIN, A_DIR_PIN, is_dm_driver=True),
    'b': Stepper(B_STEP_PIN, B_DIR_PIN, is_dm_driver=True), 
    'c': Stepper(C_STEP_PIN, C_DIR_PIN, is_dm_driver=True)
}
a_limit_home = machine.Pin(A_LIMIT_HOME_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
a_limit_end = machine.Pin(A_LIMIT_END_PIN, machine.Pin.IN, machine.Pin.PULL_UP)

# --- 6. 异步任务 ---
async def tcp_server(host, port):
    print(f"TCP 伺服器啟動於 {host}:{port}")
    async def handle_client(reader, writer):
        print("客戶端已連接"); update_display("Status: Online", f"IP: {host}", "Client Connected")
        while True:
            try:
                data = await reader.readline()
                if data: await command_queue.put((data.decode().strip(), writer))
                else: print("客戶端斷開連接"); update_display("Status: Online", f"IP: {host}", "Client Disconn."); break
            except Exception as e:
                print(f"讀取錯誤: {e}")
                sys.print_exception(e) # 打印詳細錯誤
                update_display("Status: ERROR", "Client Read Err")
                break
        writer.close(); await writer.wait_closed()
    await uasyncio.start_server(handle_client, host, port)

async def command_processor():
    print("指令處理器已啟動。")
    params = {
        'peel_lift_z1': 5.05, 'peel_return_z2': 5.0,
        'z_speed_down': 20.0, 'z_speed_up': 20.0,
        'wipe_speed_fast': 80.0, 'wipe_speed_slow': 10.0,
    }
    while True:
        cmd, writer = await command_queue.get()
        cmd_short = (cmd[:14] + '..') if len(cmd) > 16 else cmd; update_display("Status: Running", f"CMD: {cmd_short}"); response = ""; parts = cmd.split(','); command = parts[0].upper()
        move_success = True 
        try:
            if command == "CONFIG_AXIS":
                axis, pulse_per_rev, lead = parts[1].lower(), float(parts[2]), float(parts[3])
                if axis in steppers: steppers[axis].steps_per_mm = pulse_per_rev / lead; response = f"OK: Axis {axis} configured.\n"
                else: response = "ERROR: Invalid axis.\n"
            elif command == "CONFIG_Z_PEEL":
                params['peel_lift_z1'], params['peel_return_z2'], params['z_speed_down'], params['z_speed_up'] = map(float, parts[1:]); response = "OK: Z peel params configured.\n"
            elif command == "CONFIG_A_WIPE":
                params['wipe_speed_fast'], params['wipe_speed_slow'] = map(float, parts[1:]); response = "OK: A wipe params configured.\n"

            # --- 核心修改：NEXT_LAYER 移除并发，改为顺序执行 ---
            elif command == "NEXT_LAYER":
                print("[NL] NEW Sequence Started.")
                
                # --- 1. Z轴向下 (Return) [顺序执行] ---
                print("[NL] Step 1: Z-Down (Return)...")
                update_display("Status: Printing", "Action: Return", "Z-Down...")
                
                # (Z 轴方向修正)
                await steppers['z'].move_rel(-params['peel_return_z2'], params['z_speed_up'], 0)
                print("[NL] Step 1 Complete.")
                await uasyncio.sleep_ms(100) 


                # --- 2. A轴移动到远端 (Wipe) [顺序执行] ---
                print("[NL] Step 2: A-to-End (Wipe)...")
                update_display("Status: Printing", "Action: Wiping", "A-to-End")

                move_success_a_end = await steppers['a'].move_until_trigger(is_forward=True, speed_mm_s=params['wipe_speed_fast'], trigger_pin=a_limit_end)
                
                if not move_success_a_end:
                    raise RuntimeError("A to End failed (Limit Timeout?)")

                # --- (重要) 增加 "A to End" 后的回退 ---
                print("[NL] Backing off END switch...")
                await steppers['a'].move_rel(-2.0, params['wipe_speed_slow'], 0) # 向后移动 2mm
                await uasyncio.sleep_ms(100)
                print("[NL] Step 2 Complete.")


                # --- 3. Z轴上升 (Lift/Peel) [顺序执行] ---
                print("[NL] Step 3: Z-Up (Peel)...")
                update_display("Status: Printing", "Action: Peeling", "Z-Up...")
                # (Z 轴方向修正)
                await steppers['z'].move_rel(params['peel_lift_z1'], params['z_speed_down'], 0)
                print("[NL] Step 3 Complete.")
                await uasyncio.sleep_ms(1000) 


                # --- 4. A轴回到 Home 端 [顺序执行] ---
                print("[NL] Step 4: A-to-Home...")
                update_display("Status: Printing", "Action: Wiping", "A-to-Home...")
                move_success_a_home = await steppers['a'].move_until_trigger(is_forward=False, speed_mm_s=params['wipe_speed_slow'], trigger_pin=a_limit_home)

                # (重试逻辑)
                if not move_success_a_home:
                    print("[NL] A to Home failed on first attempt. Retrying...")
                    update_display("Status: Printing", "Action: Wiping", "Retry A Home")
                    move_success_a_home = await steppers['a'].move_until_trigger(is_forward=False, speed_mm_s=params['wipe_speed_slow'], trigger_pin=a_limit_home)
                    if not move_success_a_home: # 如果再次失败
                        raise RuntimeError("A to Home failed after retry (Limit Timeout?)")

                # --- (重要) 增加 "A to Home" 后的回退 ---
                print("[NL] Backing off HOME switch...")
                await steppers['a'].move_rel(2.0, params['wipe_speed_slow'], 0) # 向前移动 2mm
                await uasyncio.sleep_ms(100)
                
                print("[NL] Step 4 Complete.")
                print("[NL] NEW NEXT_LAYER sequence complete.")
                response = "DONE\n"
            # --- 修改结束 ---

            elif command == "MOVE_REL":
                axis, distance, speed, accel = parts[1].lower(), float(parts[2]), float(parts[3]), float(parts[4])
                if axis in steppers: await steppers[axis].move_rel(distance, speed, accel); response = "DONE\n"
                else: response = "ERROR: Invalid axis.\n"
            else: response = "ERROR: Unknown command.\n"
        except Exception as e:
            print(f"處理指令 '{cmd}' 時發生錯誤:")
            sys.print_exception(e) # 打印詳細錯誤
            update_display("Status: ERROR", "Processing err", str(e)); response = f"ERROR: Processing command failed: {e}\n"

        if response and ("DONE" in response or "OK" in response): update_display("Status: Online", "Last OK", f"CMD: {cmd_short}")
        if response and writer:
            try:
                print(f"Sending response: {response.strip()}") # 打印發送的回應
                writer.write(response.encode()); await writer.drain()
            except OSError as e: print(f"發送回應失败，客戶端可能已斷開: {e}")

async def main():
    import network; host_ip = "0.0.0.0"
    try:
        wlan = network.WLAN(network.STA_IF);
        if wlan.isconnected(): host_ip = wlan.ifconfig()[0]
    except Exception as e: print(f"無法獲取 IP: {e}")
    update_display("Status: Ready", f"IP: {host_ip}", "Waiting Client..")
    server_task = uasyncio.create_task(tcp_server(host_ip, 8899)); processor_task = uasyncio.create_task(command_processor())
    print("ESP32 4-Axis Controller Ready."); await uasyncio.gather(server_task, processor_task)

# --- 7. 主程式入口 ---
if __name__ == "__main__":
    try:
        uasyncio.run(main())
    except Exception as e:
        print("主循環發生致命錯誤:")
        sys.print_exception(e)
        update_display("FATAL ERROR", str(e))
        time.sleep(10) # GND?