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
FILTER_LOW_HZ = 0.5
FILTER_HIGH_HZ = 10
FILTER_ORDER = 4
SAMPLE_RATE = 100

HISTORY_DEPTH = 200 

# --- Константы ---
# Буферы для хранения состояния предыдущего успешного пакета (для интерполяции)
last_state_p1 = {"ts": None, "amp": None, "phase": None}
last_state_p2 = {"ts": None, "amp": None, "phase": None}

# Буферы для первого и второго порта
history_p1 = {"amp": [deque(maxlen=HISTORY_DEPTH) for _ in range(52)],
              "phase": [deque(maxlen=HISTORY_DEPTH) for _ in range(52)]}

history_p2 = {"amp": [deque(maxlen=HISTORY_DEPTH) for _ in range(52)],
              "phase": [deque(maxlen=HISTORY_DEPTH) for _ in range(52)]}


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


def raw_csi_to_amp_phase(msg, f_out):
        # Теперь функция просто обрабатывает готовый словарь
    raw_data = msg['data']
    timestamp = msg.get('timestamp', 'No_Time') 
    agc_gain = float(msg.get('agc_gain', 0))
    fft_gain = int(msg.get('fft_gain', 0))
    try:
        rssi = float(msg.get('rssi', -50))
    except (ValueError, TypeError):
        rssi = -50.0

    rssi_linear = 10 ** (rssi / 10.0)

    I = []
    Q = []
    In = []
    Qn = []
    Ia = []
    Qa = []
    Ian = []
    Qan = []
    amplitudes = []
    amplitudes_agc = []
    amplitudes_rssi = []
    amplitudessqr = []
    amplitudes_f = []
    phases = []
    phases_f = []


    for i in range(0, len(raw_data), 2):
        curr_i = raw_data[i]
        curr_q = raw_data[i+1]

        I.append(curr_i)
        Q.append(curr_q)

        amps = math.sqrt(curr_i**2 + curr_q**2)
        ampsqr = curr_i**2 + curr_q**2
        amplitudes.append(amps)
        amplitudessqr.append(ampsqr)
        #phases.append(math.degrees(math.atan2(Q[i], I[i])))

    sum_amp = sum(amplitudessqr)

    #Нейтрализуется CSI Ratio но можно реализовать отдельный вывод
    # for i in range(len(amplitudes)):
    #     Iacurr=I[i]
    #     Qacurr=Q[i]
    #     Ia.append((Iacurr/10**(agc_gain/20))*math.sqrt(rssi_linear/sum_amp))
    #     Qa.append((Qacurr/10**(agc_gain/20))*math.sqrt(rssi_linear/sum_amp))


    x=0.0
    ref = 9
    for i in range(9,39):
        if amplitudes[i] > x:
            x=amplitudes[i]
            ref = i
    

    for i in range(len(amplitudes)):
        Incurr=(I[i]*I[ref]+Q[i]*Q[ref])/(I[ref]**2+Q[ref]**2)
        Qncurr=(Q[i]*I[ref]-I[i]*Q[ref])/(I[ref]**2+Q[ref]**2)
        In.append(Incurr)
        Qn.append(Qncurr)

    #Нейтрализуется CSI Ratio но можно реализовать отдельный вывод
    # for i in range(len(amplitudes)):
    #     Iacurr=(Ia[i]*Ia[ref]+Qa[i]*Qa[ref])/(Ia[ref]**2+Qa[ref]**2)
    #     Qacurr=(Qa[i]*Ia[ref]-Ia[i]*Qa[ref])/(Ia[ref]**2+Qa[ref]**2)
    #     Ian.append(Iacurr)
    #     Qan.append(Qacurr)

    for i in range(len(amplitudes)):
        Incurr = In[i]
        Qncurr = Qn[i]
        #Iacurr = Ian[i]
        #Qacurr = Qan[i]
        amplitudes_f.append(math.sqrt(Incurr**2 + Qncurr**2))
        phases_f.append(math.degrees(math.atan2(Qncurr, Incurr)))

    # for i in range(len(amplitudes_agc)):
    #     amplitudes_rssi.append(amplitudes_agc[i]*math.sqrt(rssi_linear/sum(amplitudes_agc)**2))

    # # Развёртка фазы (в градусах) перед записью
    unwrapped_phases = unwrap_phase_deg(phases_f)



    global history_p1, history_p2
    # Используем свойство .name файлового дескриптора для определения активного буфера истории
    active_history = history_p1 if "csi_processed1" in f_out.name else history_p2

    # # --- БЛОК ИНТЕРПОЛЯЦИИ С NUMPY И PANDAS ---
    # global last_state_p1, last_state_p2
    
    # # Определяем активный буфер истории и состояние для текущего порта
    # is_p1 = "csi_processed1" in f_out.name
    # active_history = history_p1 if is_p1 else history_p2
    # state = last_state_p1 if is_p1 else last_state_p2

    # current_ts = pd.to_datetime(timestamp)
    # dt_expected = 1.0 / SAMPLE_RATE  # ~0.013333 сек

    # if state["ts"] is not None:
    #     delta_t = (current_ts - state["ts"]).total_seconds()
        
    #     # Если пропуск больше, чем 1.5 ожидаемых интервала, фиксируем потерю пакетов
    #     if delta_t > 1.5 * dt_expected:
    #         num_missing = int(round(delta_t / dt_expected)) - 1
            
    #         # Защита от "залипаний": если связь пропала надолго, не генерируем миллионы точек
    #         num_missing = min(num_missing, 150)  # максимум 2 секунды пропуска
            
    #         if num_missing > 0:
    #             # Сетка весов от 0 до 1 для линейной интерполяции векторов
    #             grid = np.linspace(0, 1, num_missing + 2)[1:-1]
                
    #             # Превращаем списки в массивы NumPy для векторных операций
    #             old_amp = np.array(state["amp"])
    #             new_amp = np.array(amplitudes_f)
    #             old_phase = np.array(state["phase"])
    #             new_phase = np.array(unwrapped_phases)

    #             for step in range(num_missing):
    #                 weight = grid[step]
                    
    #                 # Быстрая линейная интерполяция векторов для всех 52 поднесущих
    #                 interp_amp = (1 - weight) * old_amp + weight * new_amp
    #                 interp_phase = (1 - weight) * old_phase + weight * new_phase
                    
    #                 # 1. Добавляем виртуальную точку в историю дек (сохраняем равномерный шаг для фильтра)
    #                 for idx in range(len(interp_amp)):
    #                     active_history["amp"][idx].append(interp_amp[idx])
    #                     active_history["phase"][idx].append(interp_phase[idx])
                    
    #                 # 2. Вычисляем виртуальный таймстамп для записи в CSV
    #                 interp_ts_val = state["ts"] + pd.Timedelta(seconds=(step + 1) * dt_expected)
    #                 interp_ts_str = interp_ts_val.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                    
    #                 # Записываем интерполированную строку в файл (без фильтрации, просто чтобы не было дыр)
    #                 line_interp = f"{interp_ts_str},"
    #                 for subcarrier_index in range(len(interp_amp)):
    #                     line_interp += f"{interp_amp[subcarrier_index]},{interp_phase[subcarrier_index]},"
    #                 f_out.write(line_interp + "\n")

    # # Обновляем глобальное состояние последней "честной" точки
    # state["ts"] = current_ts
    # state["amp"] = amplitudes_f
    # state["phase"] = unwrapped_phases
    # # --- КОНЕЦ БЛОКА ИНТЕРПОЛЯЦИИ ---

    ready_amplitudes = []
    ready_phases = []

    for idx in range(len(amplitudes_f)):
        # 1. Сначала сохраняем текущее значение (частотно-развернутое) в историю времени
        active_history["amp"][idx].append(amplitudes_f[idx])
        active_history["phase"][idx].append(unwrapped_phases[idx])

        amp_series = list(active_history["amp"][idx])
        phase_series = list(active_history["phase"][idx])

        # 2. Делаем развёртку ВО ВРЕМЕНИ для накопленной истории этой поднесущей
        if len(phase_series) > 1:
            phase_series_unwrapped = np.unwrap(phase_series, period=360)
        else:
            phase_series_unwrapped = phase_series

        # Текущая развернутая фаза — это ПОСЛЕДНИЙ элемент временного ряда
        current_unwrapped_phase = phase_series_unwrapped[-1]

        # 3. Фильтрация (передаем весь развернутый вектор)
        if FILTER_ENABLED and len(amp_series) > 30:
            try:
                filtered_amps = bandpass_filter_fast(amp_series)
                # Превращаем ndarray от numpy обратно в list для вашего фильтра
                filtered_phases = bandpass_filter_fast(list(phase_series_unwrapped))
                
                # Забираем последние (актуальные для этого шага) отфильтрованные точки
                ready_amplitudes.append(filtered_amps[-1])
                ready_phases.append(filtered_phases[-1])
            except Exception:
                ready_amplitudes.append(amplitudes_f[idx])
                ready_phases.append(current_unwrapped_phase)
        else:
            ready_amplitudes.append(amplitudes_f[idx])
            ready_phases.append(current_unwrapped_phase)


    # Запись в уже открытый дескриптор файла f_out (без повторного open/close)
    line = f"{timestamp},"
    for subcarrier_index in range(len(amplitudes)):
        amp = ready_amplitudes[subcarrier_index]
        ph = ready_phases[subcarrier_index]
        line += f"{amp},{ph},"
    f_out.write(line + "\n")


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
    processed_file1 = 'log/csi_processed1.csv'
    processed_file2 = 'log/csi_processed2.csv'
    os.makedirs('log', exist_ok=True)

    for f_name in [processed_file1, processed_file2]:
        with open(f_name, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["time_stamp",])

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