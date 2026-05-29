import argparse
import queue
from multiprocessing import Process, Queue
import serial
import time
from datetime import datetime
import base64
import sys
import os
import csv
from io import StringIO
import re
import json
import math
import threading
from scipy.signal import butter, filtfilt
from collections import deque
import numpy as np
import pandas as pd


# --- Butterworth bandpass filter helper ---
FILTER_ENABLED = True
FILTER_LOW_HZ = 0.1
FILTER_HIGH_HZ = 10
FILTER_ORDER = 4
SAMPLE_RATE = 100

HISTORY_DEPTH = 200 

# --- Константы ---
# Буферы для хранения состояния предыдущего успешного пакета (для интерполяции)
last_state_p1 = {"ts": None, "amp": None, "phase": None}
last_state_p2 = {"ts": None, "amp": None, "phase": None}

reference_states = {
    "csi_processed1": {"calib_ref": None, "active_ref": None, "phase_offset": 0.0},
    "csi_processed2": {"calib_ref": None, "active_ref": None, "phase_offset": 0.0}
}

# Буферы для первого и второго порта
history_p1 = {"amp": [deque(maxlen=HISTORY_DEPTH) for _ in range(128)],
              "phase": [deque(maxlen=HISTORY_DEPTH) for _ in range(128)],
              "current_ref": 26,       # Начальный жестко заданный референс
              "phase_offset": 0.0,     # Кумулятивное смещение фазы для этого порта
              "packet_count": 0}

history_p2 = {"amp": [deque(maxlen=HISTORY_DEPTH) for _ in range(128)],
              "phase": [deque(maxlen=HISTORY_DEPTH) for _ in range(128)],
              "current_ref": 26,       # Начальный жестко заданный референс
              "phase_offset": 0.0,     # Кумулятивное смещение фазы для этого порта
              "packet_count": 0}


CSI_VAID_SUBCARRIER_INTERVAL = 5
csi_vaid_subcarrier_index = [i for i in range(0, 26, CSI_VAID_SUBCARRIER_INTERVAL)]

DEVICE_INFO_COLUMNS = ["type", "timestamp", "compile_time", "chip_name", "chip_revision",
                       "app_revision", "idf_revision", "total_heap", "free_heap", "router_ssid", "ip", "port"]

CSI_DATA_COLUMNS = ["type", "seq", "timestamp", "taget_seq", "taget", "mac", "rssi", "rate", "sig_mode", "mcs",
                    "cwb", "smoothing", "not_sounding", "aggregation", "stbc", "fec_coding", "sgi", "noise_floor",
                    "ampdu_cnt", "channel_primary", "channel_secondary", "local_timestamp", "ant", "sig_len",
                    "rx_state", "agc_gain", "fft_gain", "len", "first_word_invalid", "data"]

RADAR_DATA_COLUMNS = ["type", "seq", "timestamp", "waveform_wander", "wander_average", 
                      "waveform_wander_threshold", "someone_status", "waveform_jitter", 
                      "jitter_midean", "waveform_jitter_threshold", "move_status"]

def base64_decode_bin(str_data):
    try:
        bin_data = base64.b64decode(str_data)
        return [b - 256 if b > 127 else b for b in bin_data]
    except Exception as e:
        print(f"Ошибка декодирования base64: {e}")
        return []

def serial_handle(queue_read, queue_write, port):
    try:
        ser = serial.Serial(port=port, baudrate=2000000, bytesize=8, parity='N', stopbits=1, timeout=0.1)
    except Exception as e:
        print(f"Ошибка открытия порта {port}: {e}")
        queue_read.put({'type': 'FAIL_EVENT', 'data': f"Failed to open {port}"})
        return

    print(f"Порт {port} открыт.")
    ser.flushInput()

    for folder in ['log', 'data']:
        if not os.path.exists(folder):
            os.makedirs(folder)

    safe_port_name = port.replace('/', '_').replace('\\', '_')

    data_configs = [
        {"type": "CSI_DATA", "cols": CSI_DATA_COLUMNS, "path": f"log/csi_data_{safe_port_name}.csv"},
        {"type": "RADAR_DADA", "cols": RADAR_DATA_COLUMNS, "path": f"log/radar_data_{safe_port_name}.csv"},
        {"type": "DEVICE_INFO", "cols": DEVICE_INFO_COLUMNS, "path": f"log/device_info_{safe_port_name}.csv"}
    ]

    files_fds = {}
    writers = {}

    for cfg in data_configs:
        fd = open(cfg["path"], 'a', encoding='utf-8', newline='')
        writer = csv.writer(fd)
        writer.writerow(cfg["cols"])
        files_fds[cfg["type"]] = fd
        writers[cfg["type"]] = writer

    log_data_writer = open(f"log/log_data_{safe_port_name}.txt", 'a', encoding='utf-8')
    target_last = 'unknown'
    target_seq_last = 0
    target_csv_writer = None
    target_fd = None

    # Перезагружаем плату
    ser.write(b"restart\r\n")
    # КРИТИЧЕСКИЙ МОМЕНТ: Даем ESP32 полностью перезагрузиться (~3.5 сек) до промпта csi>,
    # чтобы последующие стартовые команды из очереди не были стерты ребутом.
    time.sleep(3.5)
    
    try:
        while True:
            # Обработка команд на запись в порт
            if not queue_write.empty():
                command = queue_write.get()
                if command == "exit": break
                ser.write(f"{command}\r\n".encode('utf-8'))
                continue

            # Чтение из порта
            line = ser.readline()
            if not line: continue
            
            try:
                line_str = line.decode('utf-8', errors='ignore').strip()
            except: continue
            
            if not line_str: continue
            #print(line_str)

            matched = False
            for cfg in data_configs:
                if cfg["type"] in line_str:
                    matched = True
                    start_idx = line_str.find(cfg["type"])
                    clean_line = line_str[start_idx:]
                    
                    csv_reader = csv.reader(StringIO(clean_line))
                    try:
                        row = next(csv_reader)
                    except StopIteration: continue

                    if len(row) == len(cfg["cols"]):
                        data_dict = dict(zip(cfg["cols"], row))
                        
                        ts = data_dict.get('timestamp', '')
                        try:
                            datetime.strptime(ts, '%Y-%m-%d %H:%M:%S.%f')
                        except:
                            data_dict['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

                        if cfg["type"] == 'CSI_DATA':
                            raw_csi = base64_decode_bin(data_dict['data'])
                            data_dict['data'] = raw_csi
                            
                            current_target = data_dict['taget']
                            current_seq = data_dict['taget_seq']
                            
                            if current_target != 'unknown':
                                if current_target != target_last or current_seq != target_seq_last:
                                    if target_fd: target_fd.close()
                                    
                                    folder = f"data/{current_target}"
                                    if not os.path.exists(folder): os.makedirs(folder)
                                    
                                    fname = f"{folder}/{datetime.now().strftime('%Y-%m-%d_%H-%M-%S-%f')[:-3]}_{data_dict['len']}_{current_seq}.csv"
                                    target_fd = open(fname, 'a', encoding='utf-8', newline='')
                                    target_csv_writer = csv.writer(target_fd)
                                    target_csv_writer.writerow(CSI_DATA_COLUMNS)
                                
                                row_to_write = [data_dict[col] for col in CSI_DATA_COLUMNS]
                                target_csv_writer.writerow(row_to_write)
                                target_last, target_seq_last = current_target, current_seq

                        row_to_write = [data_dict.get(col, '') for col in cfg["cols"]]
                        writers[cfg["type"]].writerow(row_to_write)
                        files_fds[cfg["type"]].flush()

                        if not queue_read.full():
                            queue_read.put(data_dict)
                    break

            if not matched:
                clean_log = re.sub(r'\x1b\[[0-9;]*m', '', line_str)
                log_data_writer.write(clean_log + "\n")
                
                log_match = re.match(r'.*([DIWE]) \((\d+)\) (.*)', clean_log, re.I)
                if log_match:
                    log_entry = {
                        'type': 'LOG_DATA',
                        'tag': log_match.group(1),
                        'timestamp': log_match.group(2),
                        'data': log_match.group(3)
                    }
                    if not queue_read.full():
                        queue_read.put(log_entry)

    finally:
        for fd in files_fds.values(): fd.close()
        if target_fd: target_fd.close()
        log_data_writer.close()
        ser.close()


# --- Расчет глобальных коэффициентов фильтра (Вычисляем ОДИН раз) ---
def butter_bandpass(lowcut, highcut, fs, order=4):
    nyq = 0.5 * fs
    low = lowcut / nyq
    high = highcut / nyq
    if low <= 0: low = 1e-6
    if high >= 1: high = 0.999999
    b, a = butter(order, [low, high], btype='band')
    return b, a

b_band, a_band = butter_bandpass(FILTER_LOW_HZ, FILTER_HIGH_HZ, SAMPLE_RATE, FILTER_ORDER)

def bandpass_filter_fast(data):
    """Быстрое применение фильтра с заранее рассчитанными коэффициентами."""
    if not data:
        return []
    try:
        y = filtfilt(b_band, a_band, data)
        return [float(x) for x in y]
    except Exception:
        return data


def unwrap_phase_deg(phs):
        # Простая реализация развёртки фаз в градусах без numpy
        if not phs:
            return []
        unwrapped = [phs[0]]
        cumulative_offset = 0.0
        prev_raw = phs[0]
        for i in range(1, len(phs)):
            phi = phs[i]
            delta = phi - prev_raw
            # приведение дельты к диапазону (-180, 180]
            delta_mod = (delta + 180.0) % 360.0 - 180.0
            # корректировка для накопления ступенчатых сдвигов
            correction = delta_mod - delta
            cumulative_offset += correction
            unwrapped.append(phi + cumulative_offset)
            prev_raw = phi
        return unwrapped


def sanitize_phase(phases, subcarrier_indices):
    """
    Очистка фазы от линейного наклона (Timing Offset) и постоянного смещения (CFO)
    phases: список развернутых фаз одного пакета (длина 128)
    subcarrier_indices: список реальных номеров поднесущих от -64 до 63
    """
    ph = np.array(phases)
    idx = np.array(subcarrier_indices)
    
    # Чтобы крайний шум не ломал регрессию, считаем наклон только по "хорошим" центральным поднесущим
    # Выбираем, например, поднесущие от -20 до -5 и от 5 до 20
    good_mask = ((idx >= -20) & (idx <= -5)) | ((idx >= 5) & (idx <= 20))
    
    if not np.any(good_mask):
        return phases # Если пакет битый, возвращаем как есть

    # Методом наименьших квадратов находим наклон (a) и смещение (b): ph = a * idx + b
    a, b = np.polyfit(idx[good_mask], ph[good_mask], 1)
    
    # Вычитаем линейный тренд из ВСЕХ поднесущих пакета
    sanitized_ph = ph - (a * idx + b)
    
    return sanitized_ph.tolist()


def raw_csi_to_amp_phase(msg, f_out):
    raw_data = msg['data']
    timestamp = msg.get('timestamp', 'No_Time') 
    
    # Определяем, с каким файлом/портом работаем
    port_key = "csi_processed1" if "csi_processed1" in f_out.name else "csi_processed2"
    p_state = reference_states[port_key]
    active_history = history_p1 if port_key == "csi_processed1" else history_p2

    I = []
    Q = []
    amplitudes = []
    amplitudessqr = []

    # 1. Извлекаем сырые I/Q и считаем честные сырые амплитуды
    for i in range(0, len(raw_data), 2):
        curr_i = raw_data[i]
        curr_q = raw_data[i+1]
        I.append(curr_i)
        Q.append(curr_q)
        amplitudes.append(math.sqrt(curr_i**2 + curr_q**2))
        amplitudessqr.append(curr_i**2 + curr_q**2)

    # 2. ПЕРВАЯ КАЛИБРОВКА: Если это самый первый пакет, ищем глобально лучший референс
    if p_state["calib_ref"] is None:
        best_amp = 0.0
        best_idx = 6
        # Ищем в стабильной зоне (пропускаем крайние затухающие поднесущие)
        for i in range(10, 115):
            if amplitudes[i] > best_amp:
                best_amp = amplitudes[i]
                best_idx = i
        p_state["calib_ref"] = best_idx
        p_state["active_ref"] = best_idx
        print(f"[{port_key.upper()} CALIBRATION]: Выбран базовый референс №{best_idx} (Amp: {best_amp:.2f})")

    base_ref = p_state["calib_ref"]
    act_ref = p_state["active_ref"]

    # 3. ДИНАМИЧЕСКИЙ МОНИТОРИНГ ЗАМИРАНИЙ
    # Ищем, какая поднесущая объективно лучшая в ЭТОМ пакете
    current_best_amp = 0.0
    current_best_idx = act_ref
    for i in range(10, 115):
        if amplitudes[i] > current_best_amp:
            current_best_amp = amplitudes[i]
            current_best_idx = i

    # Если текущий активный референс просел ниже порога замирания (например, упал ниже 10.0)
    # И при этом есть альтернатива, которая в 1.5 раза лучше него текущего
    if amplitudes[act_ref] < 10.0 and amplitudes[current_best_idx] > (amplitudes[act_ref] * 1.5):
        old_ref = act_ref
        new_ref = current_best_idx
        
        # Считаем геометрический скачок фазы между старым и новым референсом в сырых данных
        raw_phase_old = math.degrees(math.atan2(Q[old_ref], I[old_ref]))
        raw_phase_new = math.degrees(math.atan2(Q[new_ref], I[new_ref]))
        delta = raw_phase_new - raw_phase_old
        delta = (delta + 180.0) % 360.0 - 180.0 # Коррекция периода
        
        # Накапливаем смещение, чтобы график фазы не прыгал при смене опоры
        p_state["phase_offset"] += delta
        p_state["active_ref"] = new_ref
        act_ref = new_ref
        print(f"[{port_key.upper()} SWITCH]: Референс упал до {amplitudes[old_ref]:.1f}. Переключаемся {old_ref} -> {new_ref}")

    # 4. МАТЕМАТИКА CSI RATIO (Всегда делим на стабильный базовый base_ref!)
    In = []
    Qn = []
    ref_denom = I[base_ref]**2 + Q[base_ref]**2
    if ref_denom == 0: ref_denom = 1e-6 # Защита от деления на ноль

    for i in range(len(amplitudes)):
        Incurr = (I[i] * I[base_ref] + Q[i] * Q[base_ref]) / ref_denom
        Qncurr = (Q[i] * I[base_ref] - I[i] * Q[base_ref]) / ref_denom
        In.append(Incurr)
        Qn.append(Qncurr)

    amplitudes_f = []
    phases_f = []
    for i in range(len(amplitudes)):
        amp_calc = math.sqrt(In[i]**2 + Qn[i]**2)
        # Считаем фазу после CSI Ratio и ДОБАВЛЯЕМ наш накопленный offset для убирания ступенек
        phase_calc = math.degrees(math.atan2(Qn[i], In[i])) + p_state["phase_offset"]
        
        amplitudes_f.append(amp_calc)
        phases_f.append(phase_calc)

    # 5. СГЛАЖИВАНИЕ ВО ВРЕМЕНИ И ЗАПИСЬ
    ready_amplitudes = []
    ready_phases = []

    for idx in range(len(amplitudes_f)):
        active_history["amp"][idx].append(amplitudes_f[idx])
        active_history["phase"][idx].append(phases_f[idx])

        amp_series = list(active_history["amp"][idx])
        phase_series = list(active_history["phase"][idx])

        if len(phase_series) > 1:
            phase_series_unwrapped = np.unwrap(phase_series, period=360)
        else:
            phase_series_unwrapped = phase_series

        current_unwrapped_phase = phase_series_unwrapped[-1]

        if FILTER_ENABLED and len(amp_series) > 30:
            try:
                filtered_amps = bandpass_filter_fast(amp_series)
                filtered_phases = bandpass_filter_fast(list(phase_series_unwrapped))
                ready_amplitudes.append(filtered_amps[-1])
                ready_phases.append(filtered_phases[-1])
            except Exception:
                ready_amplitudes.append(amplitudes_f[idx])
                ready_phases.append(current_unwrapped_phase)
        else:
            ready_amplitudes.append(amplitudes_f[idx])
            ready_phases.append(current_unwrapped_phase)

    # Запись результатов в CSV
    line = f"{timestamp},"
    for subcarrier_index in range(len(amplitudes)):
        amp = ready_amplitudes[subcarrier_index]
        ph = ready_phases[subcarrier_index]
        line += f"{amp},{ph},"
    f_out.write(line + "\n")
    
    return amplitudes_f, ready_amplitudes, phase_series_unwrapped, ready_phases


class RadarController:
    def __init__(self, port1, port2):
        self.p1_name = port1.strip(", ")
        self.p2_name = port2.strip(", ")
        
        self.queue_write1 = Queue(maxsize=64)
        self.queue_write2 = Queue(maxsize=64)
        self.queue_read1 = Queue(maxsize=100) 
        self.queue_read2 = Queue(maxsize=100)
        
        self.p1 = Process(target=serial_handle, args=(self.queue_read1, self.queue_write1, self.p1_name))
        self.p2 = Process(target=serial_handle, args=(self.queue_read2, self.queue_write2, self.p2_name))

    def start(self):
        self.p1.start()
        self.p2.start()

    def send_command(self, cmd):
        self.queue_write1.put(cmd)
        self.queue_write2.put(cmd)

    def router_connect(self, ssid=None, password=None):
        if not ssid: return
        cmd = f"wifi_config --ssid \"{ssid}\""
        if password and len(password) >= 8:
            cmd += f" --password {password}"
        self.send_command(cmd)


def input_thread_func(controller):
    """Отдельный поток для чтения команд из консоли без блокировки основного цикла."""
    while True:
        try:
            cmd = sys.stdin.readline().strip()
            if not cmd: continue
            if cmd == "exit":
                print("Выход из программы...")
                controller.send_command("exit")
                break
            else:
                controller.send_command(cmd)
        except Exception:
            break


if __name__ == "__main__":
    ref = 0
    amp_ref = 1.0

    processed_file1 = 'log/csi_processed1.csv'
    processed_file2 = 'log/csi_processed2.csv'
    os.makedirs('log', exist_ok=True)


    headers = ["time_stamp"]
    
    # Генерируем подписи для каждой из 128 поднесущих (от -64 до 63)
    for i in range(128):
        subcarrier_num = i
        headers.append(f"amp_sub_{subcarrier_num}")
        headers.append(f"phase_sub_{subcarrier_num}")


    for f_name in [processed_file1, processed_file2]:
        with open(f_name, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(headers)

    parser = argparse.ArgumentParser()
    parser.add_argument('-p1', '--port1', required=True)
    parser.add_argument('-p2', '--port2', required=True)
    args = parser.parse_args()

    controller = RadarController(args.port1, args.port2)
    controller.start()

    # Запускаем интерактивный поток для обработки ввода пользователя из терминала
    t_in = threading.Thread(target=input_thread_func, args=(controller,), daemon=True)
    t_in.start()

    # Сразу ставим команды инициализации в очередь. 
    # Благодаря time.sleep(3.5) внутри процессов, команды дождутся загрузки плат.
    try:
        with open('./config/gui_config.json', 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            ssid = cfg.get('router_ssid', '').strip()
            pwd = cfg.get('router_password', '').strip()
            if ssid:
                controller.send_command("radar --csi_output_type LLFT --csi_output_format base64")
                controller.router_connect(ssid, pwd)
                print(f"Стартовая конфигурация для SSID '{ssid}' отправлена в очередь ожидания.")
    except Exception:
        pass

    print("--- Система запущена (без Pandas/Numpy) ---")
    print("Доступные команды: 'locate router', 'exit' или любые команды для CLI ESP32.")

    # Открываем дескрипторы файлов один раз на весь период работы программы
    f_out1 = open(processed_file1, 'a', newline='', encoding='utf-8')
    f_out2 = open(processed_file2, 'a', newline='', encoding='utf-8')

    try:
        while True:
            # Обработка данных первой очереди
            try:
                msg1 = controller.queue_read1.get(timeout=0.05)
                t = msg1.get('type', 'Unknown')
                
                if t == 'CSI_DATA':
                    raw_csi_to_amp_phase(msg1, f_out1)
                    #print(f"[P1]: {t} обработано и записано")
                elif t == 'LOG_DATA':
                    print(f"[P1]: LOG - {msg1.get('data')}")
                elif t == 'FAIL_EVENT':
                    print(f"[P1]: КРИТИЧЕСКАЯ ОШИБКА - {msg1.get('data')}")
            except queue.Empty:
                pass

            # Обработка данных второй очереди
            try:
                msg2 = controller.queue_read2.get(timeout=0.05)
                t = msg2.get('type', 'Unknown')
                
                if t == 'CSI_DATA':
                    raw_csi_to_amp_phase(msg2, f_out2)
                    #print(f"[P2]: {t} обработано и записано")
                elif t == 'LOG_DATA':
                    print(f"[P2]: LOG - {msg2.get('data')}")
                elif t == 'FAIL_EVENT':
                    print(f"[P2]: КРИТИЧЕСКАЯ ОШИБКА - {msg2.get('data')}")
            except queue.Empty:
                pass

    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        f_out1.close()
        f_out2.close()
        controller.p1.terminate()
        controller.p2.terminate()
    print("Запустите файл gui_visualizer.py для отображения интерфейса.")