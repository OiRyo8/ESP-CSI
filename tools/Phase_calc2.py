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
import threading
from io import StringIO
import re
import json
import math
import threading
from scipy.signal import butter, filtfilt
from collections import deque
import numpy as np
import pandas as pd
from scipy.signal import butter, sosfilt, sosfilt_zi, savgol_coeffs, lfilter, lfilter_zi
from csi_calibration import CSICalibrator
from csi_analyzer import CSIAnalyzer

# --- Butterworth bandpass filter helper (Потоковый SOS-вариант) ---
FILTER_ENABLED = True
FILTER_LOW_HZ = 0.1
FILTER_HIGH_HZ = 10
FILTER_ORDER = 4
SAMPLE_RATE = 50

# --- Параметры фильтра Савицкого-Голея (Потоковый / Каузальный) ---
SAVGOL_ENABLED = True
SAVGOL_WINDOW = 15  # Размер окна во времени (должен быть нечетным). Настраивайте под вашу динамику.
SAVGOL_POLY = 3     # Степень полинома
# Вычисляем FIR-коэффициенты. pos=SAVGOL_WINDOW-1 делает фильтр направленным строго в прошлое!
SG_COEFFS = savgol_coeffs(window_length=SAVGOL_WINDOW, polyorder=SAVGOL_POLY, pos=SAVGOL_WINDOW-1, use='conv')

# ---Медианный фильтр---
MEDIAN_FILTER_ENABLED = True
MEDIAN_WINDOW_SIZE = 5  # Размер окна во времени (3, 5, 7 пакетов)

HISTORY_DEPTH = 200 

# --- БЛОК ПРЕДРАСЧЕТА ДЛЯ САНИТАЦИИ ФАЗЫ ВО ВРЕМЕНИ ---
TIME_SAN_WINDOW = 100  # Длина скользящего окна во времени (в пакетах) для оценки дрейфа

# Заранее рассчитываем неизменяемые временные метки (ось X для регрессии)
_t_axis = np.arange(TIME_SAN_WINDOW)
_t_mean = (TIME_SAN_WINDOW - 1) / 2.0
_t_dev = _t_axis - _t_mean
_t_den = np.sum(_t_dev**2)  # Знаменатель для формулы МНК


# Набор инвалидных поднесущих
INVALID_SUBCARRIERS = set(range(0, 11)) | set(range(27, 38)) | set(range(62, 66)) | set(range(91, 102)) | set(range(117, 128))
# Булев массив маски (True для полезных, False для пустых)
VALID_MASK = np.array([i not in INVALID_SUBCARRIERS for i in range(128)])


if FILTER_ENABLED:
    # Используем output='sos' вместо output='ba'
    SOS_BAND = butter(FILTER_ORDER, [FILTER_LOW_HZ, FILTER_HIGH_HZ], btype='band', fs=SAMPLE_RATE, output='sos')
    N_SECTIONS = SOS_BAND.shape[0]
else:
    SOS_BAND = None
    N_SECTIONS = 0


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
    
    flush_counter = 0

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

                        flush_counter += 1
                        if flush_counter % 100 == 0:
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


def sanitize_phase_time_vectorized(window_matrix):
    """
    Санитация фазы ВО ВРЕМЕНИ (Time-Domain Detrending).
    Убирает линейный дрейф фазы по оси времени для всех 128 поднесущих одновременно.
    window_matrix: двумерный numpy-массив формы (128, TIME_SAN_WINDOW)
    """
    # 1. Находим среднее значение фазы во времени для каждой поднесущей (вектор длины 128)
    y_mean = np.mean(window_matrix, axis=1)
    
    # 2. Центрируем матрицу фаз относительно среднего
    y_dev = window_matrix - y_mean[:, np.newaxis]
    
    # 3. Векторно находим коэффициент наклона (a) для каждой из 128 поднесущих
    # Умножаем девиации фазы на временные девиации и суммируем по оси времени (axis=1)
    num = np.sum(y_dev * _t_dev, axis=1)
    a = num / _t_den
    
    # 4. Вычисляем значение линейного тренда для самого последнего (текущего) пакета в окне
    # Формула: trend = a * t_latest + b, что математически эквивалентно: a * _t_mean + y_mean
    current_trend = a * _t_mean + y_mean
    
    # 5. Вычитаем вычисленный временной тренд из текущего пакета (последний столбец окна)
    sanitized_packet = window_matrix[:, -1] - current_trend
    
    return sanitized_packet


def raw_csi_to_amp_phase(msg, f_out_butter, f_out_savgol):
    raw_data = msg['data']
    timestamp = msg.get('timestamp', 'No_Time') 

    try:
        dt_obj = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S.%f')
        t_curr = dt_obj.timestamp()
    except Exception:
        t_curr = time.time()
    
    port_key = "csi_processed1" if "csi_processed1" in f_out_butter.name else "csi_processed2"
    p_state = reference_states[port_key]
    active_history = history_p1 if port_key == "csi_processed1" else history_p2

    # 1. Векторизованное извлечение сырых I/Q
    raw_np = np.array(raw_data, dtype=np.int8)
    
    # Учитываем, что массив может быть короче 256 байт. Ограничиваем 128 поднесущими (256 байт)
    max_sub = min(len(raw_np) // 2, 128)
    I = np.zeros(128, dtype=np.float32)
    Q = np.zeros(128, dtype=np.float32)
    
    I[:max_sub] = raw_np[0:max_sub*2:2]
    Q[:max_sub] = raw_np[1:max_sub*2:2]

    # Векторный расчет амплитуд
    amplitudes = np.sqrt(I**2 + Q**2)
    
    # Исключение пустых/защитных поднесущих (обнуляем, чтобы не ломали референсы)
    amplitudes[~VALID_MASK] = 0.0

    # 2. ПЕРВАЯ КАЛИБРОВКА: Векторный поиск референса
    if p_state["calib_ref"] is None:
        # Ищем максимум только в стабильной зоне (10-115)
        search_zone = amplitudes[10:115]
        best_idx = int(np.argmax(search_zone) + 10)
        p_state["calib_ref"] = best_idx
        p_state["active_ref"] = best_idx
        p_state["calib_amp"] = amplitudes[best_idx]  # Запоминаем исходный уровень силы
        print(f"[{port_key.upper()} CALIBRATION]: Выбран базовый референс №{best_idx} (Amp: {amplitudes[best_idx]:.2f})")

    base_ref = p_state["calib_ref"]
    act_ref = p_state["active_ref"]

    # 3. ДИНАМИЧЕСКИЙ МОНИТОРИНГ ЗАМИРАНИЙ (Векторизованно)
 # 3. ДИНАМИЧЕСКИЙ МОНИТОРИНГ ЗАМИРАНИЙ (Векторизованно)
    current_best_idx = int(np.argmax(amplitudes[10:115]) + 10)
    
    # Считаем порог глубокого замирания (10% от калибровочного значения)
    deep_fade_threshold = p_state["calib_amp"] / 10.0
    
    # Переключаемся, если упали ниже порога И новая поднесущая хотя бы в 2 раза лучше текущей (гистерезис)
    if amplitudes[act_ref] < deep_fade_threshold and amplitudes[current_best_idx] > (amplitudes[act_ref] * 2.0):
        old_ref = act_ref
        new_ref = current_best_idx
        
        raw_phase_old = np.degrees(np.arctan2(Q[old_ref], I[old_ref]))
        raw_phase_new = np.degrees(np.arctan2(Q[new_ref], I[new_ref]))
        delta = raw_phase_new - raw_phase_old
        delta = (delta + 180.0) % 360.0 - 180.0 
        
        p_state["phase_offset"] += delta
        p_state["active_ref"] = new_ref
        act_ref = new_ref
        print(f"[{port_key.upper()} SWITCH]: Референс упал в 10 раз ниже калибровки (до {amplitudes[old_ref]:.1f}). Переключаемся {old_ref} -> {new_ref}")

    # 4. МАТЕМАТИКА CSI RATIO (Полная векторизация вместо цикла)
    ref_I = I[base_ref]
    ref_Q = Q[base_ref]
    ref_denom = ref_I**2 + ref_Q**2
    if ref_denom == 0: 
        ref_denom = 1e-6 

    In = (I * ref_I + Q * ref_Q) / ref_denom
    Qn = (Q * ref_I - I * ref_Q) / ref_denom

    amplitudes_f = np.sqrt(In**2 + Qn**2)
    phases_f = np.degrees(np.arctan2(Qn, In)) + p_state["phase_offset"]

    # Сброс пустых поднесущих после вычислений Ratio
    amplitudes_f[~VALID_MASK] = 0.0
    phases_f[~VALID_MASK] = 0.0

# 5. МГНОВЕННОЕ СГЛАЖИВАНИЕ ВО ВРЕМЕНИ (ВЕКТОРНЫЙ SOS-ФИЛЬТР + САНИТАЦИЯ + МЕДИАНА)
    if "is_first_packet" not in active_history:
        active_history["is_first_packet"] = True
        active_history["last_raw_phase"] = np.zeros(128, dtype=np.float32)
        active_history["last_unwrapped_phase"] = np.zeros(128, dtype=np.float32)

    if active_history["is_first_packet"]:
        unwrapped_phases_raw = phases_f.copy()
        active_history["last_raw_phase"] = phases_f.copy()
        active_history["last_unwrapped_phase"] = unwrapped_phases_raw.copy()
        
        # Инициализируем скользящее временное окно для санитации
        active_history["phase_time_window"] = np.repeat(unwrapped_phases_raw[:, np.newaxis], TIME_SAN_WINDOW, axis=1)
        
        unwrapped_phases = unwrapped_phases_raw.copy()
        
        # --- ИНИЦИАЛИЗАЦИЯ ОКНА МЕДИАННОГО ФИЛЬТРА ---
        if MEDIAN_FILTER_ENABLED:
            active_history["sanitized_phase_window"] = np.repeat(unwrapped_phases[:, np.newaxis], MEDIAN_WINDOW_SIZE, axis=1)
        
        phase_to_filter = unwrapped_phases.copy()

        if FILTER_ENABLED:
            zi_base = sosfilt_zi(SOS_BAND)
            active_history["zi_amp"] = zi_base[:, :, np.newaxis] * amplitudes_f[np.newaxis, np.newaxis, :]
            active_history["zi_phase"] = zi_base[:, :, np.newaxis] * phase_to_filter[np.newaxis, np.newaxis, :]
            
        active_history["is_first_packet"] = False
    else:
        # Мгновенная векторная развертка фазы ВО ВРЕМЕНИ
        delta_phase = phases_f - active_history["last_raw_phase"]
        delta_phase_wrapped = (delta_phase + 180.0) % 360.0 - 180.0
        unwrapped_phases_raw = active_history["last_unwrapped_phase"] + delta_phase_wrapped
        
        active_history["last_raw_phase"] = phases_f.copy()
        active_history["last_unwrapped_phase"] = unwrapped_phases_raw.copy()
        
        # Обновляем временное окно санитации
        active_history["phase_time_window"] = np.roll(active_history["phase_time_window"], -1, axis=1)
        active_history["phase_time_window"][:, -1] = unwrapped_phases_raw
        
        # === ВЕКТОРНАЯ САНИТАЦИЯ ФАЗЫ ВО ВРЕМЕНИ ===
        unwrapped_phases = sanitize_phase_time_vectorized(active_history["phase_time_window"])

        # === ВЕКТОРНАЯ МЕДИАННАЯ ФИЛЬТРАЦИЯ ФАЗЫ ===
        if MEDIAN_FILTER_ENABLED:
            # Сдвигаем окно медианного фильтра влево
            active_history["sanitized_phase_window"] = np.roll(active_history["sanitized_phase_window"], -1, axis=1)
            # Записываем свежее санированное значение в конец
            active_history["sanitized_phase_window"][:, -1] = unwrapped_phases
            # Считаем медиану по временной оси (axis=1) сразу для всех 128 поднесущих
            phase_to_filter = np.median(active_history["sanitized_phase_window"], axis=1)
        else:
            phase_to_filter = unwrapped_phases

# === ПАРАЛЛЕЛЬНАЯ ФИЛЬТРАЦИЯ ===
    # Нам нужны базовые данные в любом случае:
    ready_amplitudes_butter = amplitudes_f.copy()
    ready_phases_butter = phase_to_filter.copy()
    ready_amplitudes_sg = amplitudes_f.copy()
    ready_phases_sg = phase_to_filter.copy()

# Если включен любой из фильтров, запускаем логику 50Hz интерполяции
    if FILTER_ENABLED or SAVGOL_ENABLED:
        target_dt = 1.0 / SAMPLE_RATE  
        
        if "next_target_ts" not in active_history:
            active_history["next_target_ts"] = t_curr
            active_history["last_ts"] = t_curr
            active_history["last_amp_med"] = amplitudes_f.copy()
            active_history["last_phase_med"] = phase_to_filter.copy()
            
            amp_in = amplitudes_f.reshape(1, 128)
            phase_in = phase_to_filter.reshape(1, 128)
            
            # Инициализация и первый прогон Баттерворта
            if FILTER_ENABLED:
                amp_out_bw, active_history["zi_amp"] = sosfilt(SOS_BAND, amp_in, axis=0, zi=active_history.get("zi_amp"))
                phase_out_bw, active_history["zi_phase"] = sosfilt(SOS_BAND, phase_in, axis=0, zi=active_history.get("zi_phase"))
                ready_amplitudes_butter = amp_out_bw[0]
                ready_phases_butter = phase_out_bw[0]
                active_history["last_filtered_amp"] = ready_amplitudes_butter.copy()
                active_history["last_filtered_phase"] = ready_phases_butter.copy()

            # Инициализация и первый прогон Савицкого-Голея
            if SAVGOL_ENABLED:
                zi_sg_base = lfilter_zi(SG_COEFFS, [1.0])
                active_history["zi_amp_sg"] = zi_sg_base[:, np.newaxis] * amplitudes_f[np.newaxis, :]
                active_history["zi_phase_sg"] = zi_sg_base[:, np.newaxis] * phase_to_filter[np.newaxis, :]
                
                amp_out_sg, active_history["zi_amp_sg"] = lfilter(SG_COEFFS, [1.0], amp_in, axis=0, zi=active_history["zi_amp_sg"])
                phase_out_sg, active_history["zi_phase_sg"] = lfilter(SG_COEFFS, [1.0], phase_in, axis=0, zi=active_history["zi_phase_sg"])
                
                ready_amplitudes_sg = amp_out_sg[0]
                ready_phases_sg = phase_out_sg[0]
                active_history["last_sg_amp"] = ready_amplitudes_sg.copy()
                active_history["last_sg_phase"] = ready_phases_sg.copy()

        else:
            t_prev = active_history["last_ts"]
            amp_prev = active_history["last_amp_med"]
            phase_prev = active_history["last_phase_med"]
            
            interpolated_amps = []
            interpolated_phases = []
            
            while active_history["next_target_ts"] <= t_curr:
                t_target = active_history["next_target_ts"]
                alpha = np.clip((t_target - t_prev) / (t_curr - t_prev), 0.0, 1.0) if t_curr > t_prev else 1.0
                interpolated_amps.append(amp_prev + alpha * (amplitudes_f - amp_prev))
                interpolated_phases.append(phase_prev + alpha * (phase_to_filter - phase_prev))
                active_history["next_target_ts"] += target_dt
            
            active_history["last_ts"] = t_curr
            active_history["last_amp_med"] = amplitudes_f.copy()
            active_history["last_phase_med"] = phase_to_filter.copy()
            
            if len(interpolated_amps) > 0:
                amp_in = np.vstack(interpolated_amps)
                phase_in = np.vstack(interpolated_phases)
                
                # Потоковый прогон Баттерворта
                if FILTER_ENABLED:
                    amp_out_bw, active_history["zi_amp"] = sosfilt(SOS_BAND, amp_in, axis=0, zi=active_history["zi_amp"])
                    phase_out_bw, active_history["zi_phase"] = sosfilt(SOS_BAND, phase_in, axis=0, zi=active_history["zi_phase"])
                    ready_amplitudes_butter = amp_out_bw[-1]
                    ready_phases_butter = phase_out_bw[-1]
                    active_history["last_filtered_amp"] = ready_amplitudes_butter.copy()
                    active_history["last_filtered_phase"] = ready_phases_butter.copy()

                # Потоковый прогон Савицкого-Голея
                if SAVGOL_ENABLED:
                    amp_out_sg, active_history["zi_amp_sg"] = lfilter(SG_COEFFS, [1.0], amp_in, axis=0, zi=active_history["zi_amp_sg"])
                    phase_out_sg, active_history["zi_phase_sg"] = lfilter(SG_COEFFS, [1.0], phase_in, axis=0, zi=active_history["zi_phase_sg"])
                    ready_amplitudes_sg = amp_out_sg[-1]
                    ready_phases_sg = phase_out_sg[-1]
                    active_history["last_sg_amp"] = ready_amplitudes_sg.copy()
                    active_history["last_sg_phase"] = ready_phases_sg.copy()
            else:
                if FILTER_ENABLED:
                    ready_amplitudes_butter = active_history["last_filtered_amp"].copy()
                    ready_phases_butter = active_history["last_filtered_phase"].copy()
                if SAVGOL_ENABLED:
                    ready_amplitudes_sg = active_history["last_sg_amp"].copy()
                    ready_phases_sg = active_history["last_sg_phase"].copy()

    # Жестко обнуляем невалидные поднесущие в обоих массивах
    ready_amplitudes_butter[~VALID_MASK] = 0.0
    ready_phases_butter[~VALID_MASK] = 0.0
    ready_amplitudes_sg[~VALID_MASK] = 0.0
    ready_phases_sg[~VALID_MASK] = 0.0

    # Запись в файл Баттерворта
    vals_bw = [f"{ready_amplitudes_butter[i]:.3f},{ready_phases_butter[i]:.3f}" for i in range(128)]
    f_out_butter.write(f"{timestamp}," + ",".join(vals_bw) + "\n")

    # Запись в файл Савицкого-Голея
    vals_sg = [f"{ready_amplitudes_sg[i]:.3f},{ready_phases_sg[i]:.3f}" for i in range(128)]
    f_out_savgol.write(f"{timestamp}," + ",".join(vals_sg) + "\n")

    return amplitudes_f.tolist(), ready_amplitudes_butter.tolist(), phase_to_filter.tolist(), ready_phases_butter.tolist(), ready_amplitudes_sg.tolist(), ready_phases_sg.tolist()



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



def console_input_thread(calibrator):
    """Функция выполняется в отдельном потоке и ждет команд от пользователя"""
    print("\n[ИНФО] Консоль управления активна. Доступные команды:")
    print("  calibrate <секунды>  - Запустить калибровку пустого помещения (напр. calibrate 15)")
    print("  status               - Проверить статус калибратора\n")
    
    while True:
        try:
            # sys.stdin.readline() работает стабильнее в многопоточности, чем input()
            user_input = sys.stdin.readline().strip()
            if not user_input:
                continue
                
            if user_input.startswith("calibrate"):
                parts = user_input.split()
                duration = 15 # по умолчанию 15 секунд
                if len(parts) > 1:
                    try:
                        duration = int(parts[1])
                    except ValueError:
                        print("[ОШИБКА] Неверный формат времени. Используйте: calibrate 15")
                        continue
                
                # Запускаем калибровку через объект калибратора
                calibrator.start(duration_sec=duration)
                
            elif user_input == "status":
                if calibrator.is_active:
                    elapsed = time.time() - calibrator.start_time
                    print(f"[СТАТУС] Идет калибровка... Прошло {elapsed:.1f}/{calibrator.duration} сек.")
                else:
                    print("[СТАТУС] Калибратор ожидает команду.")
            else:
                print(f"[КОМАНДА] Неизвестная команда: '{user_input}'")
        except Exception as e:
            print(f"[КОМАНДА] Ошибка ввода: {e}")
            break


if __name__ == "__main__":
    ref = 0
    amp_ref = 1.0

    os.makedirs('log', exist_ok=True)

    file_p1_butter = 'log/csi_processed1_butter.csv'
    file_p1_savgol = 'log/csi_processed1_savgol.csv'
    file_p2_butter = 'log/csi_processed2_butter.csv'
    file_p2_savgol = 'log/csi_processed2_savgol.csv'



    headers = ["time_stamp"]
    for i in range(128):
        headers.extend([f"amp_sub_{i}", f"phase_sub_{i}"])


    # Инициализация заголовков во всех 4 файлах
    for f_name in [file_p1_butter, file_p1_savgol, file_p2_butter, file_p2_savgol]:
        with open(f_name, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(headers)


    parser = argparse.ArgumentParser()
    parser.add_argument('-p1', '--port1', required=True)
    parser.add_argument('-p2', '--port2', required=True)
    args = parser.parse_args()

    controller = RadarController(args.port1, args.port2)
    controller.start()

    # 1. Импортируем и создаем калибратор
    from csi_calibration import CSICalibrator
    calibrator = CSICalibrator(num_subcarriers=128)
    
    # Пытаемся загрузить старый бейзлайн, если он есть
    calibrator.load_from_file()
    
    # 2. Запускаем фоновый поток для приема команд "calibrate"
    input_thread = threading.Thread(target=console_input_thread, args=(calibrator,), daemon=True)
    input_thread.start()

    analyzer_p1 = CSIAnalyzer(port_id="p1", sample_rate=50, window_sec=10, update_interval_sec=1.0)
    analyzer_p2 = CSIAnalyzer(port_id="p2", sample_rate=50, window_sec=10, update_interval_sec=1.0)

    # Сразу ставим команды инициализации в очередь. 
    # Благодаря time.sleep(3.5) внутри процессов, команды дождутся загрузки плат.
    # try:
    #     with open('./config/gui_config.json', 'r', encoding='utf-8') as f:
    #         cfg = json.load(f)
    #         ssid = cfg.get('router_ssid', '').strip()
    #         pwd = cfg.get('router_password', '').strip()
    #         if ssid:
    #             controller.send_command("radar --csi_output_type LLFT --csi_output_format base64")
    #             controller.router_connect(ssid, pwd)
    #             print(f"Стартовая конфигурация для SSID '{ssid}' отправлена в очередь ожидания.")
    # except Exception:
    #     pass

    print("--- Система запущена ---")


    # Открываем 4 дескриптора на дозапись
    f_out1_bw = open(file_p1_butter, 'a', newline='', encoding='utf-8')
    f_out1_sg = open(file_p1_savgol, 'a', newline='', encoding='utf-8')
    f_out2_bw = open(file_p2_butter, 'a', newline='', encoding='utf-8')
    f_out2_sg = open(file_p2_savgol, 'a', newline='', encoding='utf-8')

    try:
        while True:
            # Обработка данных первой очереди
            try:
                msg1 = controller.queue_read1.get(timeout=0.05)
                t = msg1.get('type', 'Unknown')
                if t == 'CSI_DATA':
                    amp_raw, amp_bw, phase_raw, phase_bw, amp_sg, phase_sg = raw_csi_to_amp_phase(msg1, f_out1_bw, f_out1_sg)
                    calibrator.update(port_id=1, amp_bw=amp_bw, amp_sg=amp_sg)
                    analyzer_p1.update(amp_bw=amp_bw, amp_sg=amp_sg, phase_bw=phase_bw, phase_sg=phase_sg)
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
                    amp_raw, amp_bw, phase_raw, phase_bw, amp_sg, phase_sg = raw_csi_to_amp_phase(msg2, f_out2_bw, f_out2_sg)
                    calibrator.update(port_id=2, amp_bw=amp_bw, amp_sg=amp_sg)
                    analyzer_p2.update(amp_bw=amp_bw, amp_sg=amp_sg, phase_bw=phase_bw, phase_sg=phase_sg)
                elif t == 'LOG_DATA':
                    print(f"[P2]: LOG - {msg2.get('data')}")
                elif t == 'FAIL_EVENT':
                    print(f"[P2]: КРИТИЧЕСКАЯ ОШИБКА - {msg2.get('data')}")
            except queue.Empty:
                pass

    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        f_out1_bw.close()
        f_out1_sg.close()
        f_out2_bw.close()
        f_out2_sg.close()
        controller.p1.terminate()
        controller.p2.terminate()
    print("Запустите файл gui_visualizer.py для отображения интерфейса.")