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

# --- БЛОК ПРЕДРАСЧЕТА ДЛЯ САНИТАЦИИ ФАЗЫ ВО ВРЕМЕНИ ---
TIME_SAN_WINDOW = 100  # Длина скользящего окна во времени (в пакетах) для оценки дрейфа

HISTORY_DEPTH = 200 

# Набор инвалидных поднесущих
INVALID_SUBCARRIERS = set(range(0, 11)) | set(range(27, 38)) | set(range(62, 66)) | set(range(91, 102)) | set(range(117, 128))
# Булев массив маски (True для полезных, False для пустых)
VALID_MASK = np.array([i not in INVALID_SUBCARRIERS for i in range(128)])


# --- Константы ---

reference_states = {
    "csi_processed1": {"calib_ref": None, "active_ref": None, "phase_offset": 0.0},
    "csi_processed2": {"calib_ref": None, "active_ref": None, "phase_offset": 0.0}
}

# Буферы для первого и второго порта
history_p1 = {}
history_p2 = {}

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

# Заранее рассчитываем неизменяемые временные метки (ось X для регрессии)
_t_axis = np.arange(TIME_SAN_WINDOW)
_t_mean = (TIME_SAN_WINDOW - 1) / 2.0
_t_dev = _t_axis - _t_mean
_t_den = np.sum(_t_dev**2)  # Знаменатель для формулы МНК

if FILTER_ENABLED:
    # Используем output='sos' вместо output='ba'
    SOS_BAND = butter(FILTER_ORDER, [FILTER_LOW_HZ, FILTER_HIGH_HZ], btype='band', fs=SAMPLE_RATE, output='sos')
    N_SECTIONS = SOS_BAND.shape[0]
else:
    SOS_BAND = None
    N_SECTIONS = 0


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


def monitor_and_select_reference(amplitudes_raw, I, Q, p_state, port_key):
    """
    Первичная калибровка референса и динамический мониторинг глубоких замираний.
    Возвращает актуальный индекс базовой поднесущей.
    """
    # 1. Первичная калибровка (если еще не проводилась)
    if p_state["calib_ref"] is None:
        search_zone = amplitudes_raw[10:115]
        best_idx = int(np.argmax(search_zone) + 10)
        p_state["calib_ref"] = best_idx
        p_state["active_ref"] = best_idx
        p_state["calib_amp"] = amplitudes_raw[best_idx]
        print(f"[{port_key.upper()} CALIBRATION]: Выбран базовый референс №{best_idx} (Amp: {amplitudes_raw[best_idx]:.2f})")

    base_ref = p_state["calib_ref"]
    act_ref = p_state["active_ref"]

    # 2. Мониторинг глубокого замирания
    current_best_idx = int(np.argmax(amplitudes_raw[10:115]) + 10)
    deep_fade_threshold = p_state["calib_amp"] / 10.0  # Порог падения в 10 раз

    # Если текущий референс упал ниже порога, и есть кандидат в 2 раза лучше (гистерезис)
    if amplitudes_raw[act_ref] < deep_fade_threshold and amplitudes_raw[current_best_idx] > (amplitudes_raw[act_ref] * 2.0):
        old_ref = act_ref
        new_ref = current_best_idx
        
        # Считаем разницу фаз для бесшовной склейки сдвига (phase_offset)
        raw_phase_old = np.degrees(np.arctan2(Q[old_ref], I[old_ref]))
        raw_phase_new = np.degrees(np.arctan2(Q[new_ref], I[new_ref]))
        delta = raw_phase_new - raw_phase_old
        delta = (delta + 180.0) % 360.0 - 180.0 
        
        p_state["phase_offset"] += delta
        p_state["active_ref"] = new_ref
        
        print(f"[{port_key.upper()} SWITCH]: Референс упал в 10 раз ниже калибровки (до {amplitudes_raw[old_ref]:.1f}). Переключаемся {old_ref} -> {new_ref}")
    
    return p_state["active_ref"]


# =====================================================================
# ЭТАП 3: Вычисление CSI Ratio
# =====================================================================
def compute_csi_ratio(I, Q, base_ref, phase_offset):
    """
    Векторное вычисление CSI Ratio (деление всех поднесущих на референсную).
    """
    ref_I = I[base_ref]
    ref_Q = Q[base_ref]
    ref_denom = ref_I**2 + ref_Q**2
    if ref_denom == 0: 
        ref_denom = 1e-6 

    # Комплексное деление
    In = (I * ref_I + Q * ref_Q) / ref_denom
    Qn = (Q * ref_I - I * ref_Q) / ref_denom

    amplitudes_ratio = np.sqrt(In**2 + Qn**2)
    phases_ratio = np.degrees(np.arctan2(Qn, In)) + phase_offset

    # Обнуляем невалидные/шумные поднесущие
    amplitudes_ratio[~VALID_MASK] = 0.0
    phases_ratio[~VALID_MASK] = 0.0

    return amplitudes_ratio, phases_ratio


# =====================================================================
# ЭТАП 4: Развертка и санитация фазы
# =====================================================================
def unwrap_and_sanitize_phase(phases_ratio, active_history):
    """
    Развертка фазы (Unwrap) для устранения скачков через 360 градусов 
    и санитация (удаление временного линейного дрейфа).
    """
    # Инициализация буферов при первом пакете
    if "last_raw_phase" not in active_history:
        active_history["last_raw_phase"] = phases_ratio.copy()
        active_history["last_unwrapped_phase"] = phases_ratio.copy()
        active_history["phase_time_window"] = np.repeat(phases_ratio[:, np.newaxis], TIME_SAN_WINDOW, axis=1)
        return phases_ratio.copy()

    # 1. Мгновенная векторная развертка фазы
    delta_phase = phases_ratio - active_history["last_raw_phase"]
    delta_phase_wrapped = (delta_phase + 180.0) % 360.0 - 180.0
    unwrapped_phases_raw = active_history["last_unwrapped_phase"] + delta_phase_wrapped
    
    active_history["last_raw_phase"] = phases_ratio.copy()
    active_history["last_unwrapped_phase"] = unwrapped_phases_raw.copy()
    
    # 2. Обновление скользящего окна
    active_history["phase_time_window"] = np.roll(active_history["phase_time_window"], -1, axis=1)
    active_history["phase_time_window"][:, -1] = unwrapped_phases_raw
    
    # 3. Санитация (очистка от дрейфа)
    # Предполагается, что функция sanitize_phase_time_vectorized доступна глобально
    unwrapped_phases = sanitize_phase_time_vectorized(active_history["phase_time_window"])
    
    return unwrapped_phases


# =====================================================================
# ЭТАП 5: Медианная фильтрация (Амплитуда и Фаза)
# =====================================================================
def apply_median_filter(amplitudes, phases, active_history):
    """
    Медианный фильтр для сглаживания резких выбросов в амплитуде и фазе.
    """
    if not MEDIAN_FILTER_ENABLED:
        return amplitudes, phases

    # Инициализация окон медианного фильтра
    if "sanitized_phase_window" not in active_history:
        active_history["sanitized_phase_window"] = np.repeat(phases[:, np.newaxis], MEDIAN_WINDOW_SIZE, axis=1)
        active_history["amp_window"] = np.repeat(amplitudes[:, np.newaxis], MEDIAN_WINDOW_SIZE, axis=1)

    # Обновление окна фазы и расчет медианы
    active_history["sanitized_phase_window"] = np.roll(active_history["sanitized_phase_window"], -1, axis=1)
    active_history["sanitized_phase_window"][:, -1] = phases
    phase_med = np.median(active_history["sanitized_phase_window"], axis=1)

    # Обновление окна амплитуды и расчет медианы
    active_history["amp_window"] = np.roll(active_history["amp_window"], -1, axis=1)
    active_history["amp_window"][:, -1] = amplitudes
    amp_med = np.median(active_history["amp_window"], axis=1)

    return amp_med, phase_med


# =====================================================================
# ЭТАП 6 и 7: Интерполяция и Параллельная фильтрация
# =====================================================================
def interpolate_and_filter(t_curr, amp_med, phase_med, active_history):
    """
    Выравнивает пакеты по времени (сетка 50Гц) и пропускает их параллельно 
    через Баттерворта и Савицкого-Голея.
    """
    ready_amp_bw = amp_med.copy()
    ready_phase_bw = phase_med.copy()
    ready_amp_sg = amp_med.copy()
    ready_phase_sg = phase_med.copy()

    if not (FILTER_ENABLED or SAVGOL_ENABLED):
        return ready_amp_bw, ready_phase_bw, ready_amp_sg, ready_phase_sg

    target_dt = 1.0 / SAMPLE_RATE  

    # Инициализация состояния интерполятора и фильтров
    if "next_target_ts" not in active_history:
        active_history["next_target_ts"] = t_curr
        active_history["last_ts"] = t_curr
        active_history["last_amp_med"] = amp_med.copy()
        active_history["last_phase_med"] = phase_med.copy()
        
        amp_in = amp_med.reshape(1, 128)
        phase_in = phase_med.reshape(1, 128)
        
        if FILTER_ENABLED:
            zi_base = sosfilt_zi(SOS_BAND)
            active_history["zi_amp"] = zi_base[:, :, np.newaxis] * amp_med[np.newaxis, np.newaxis, :]
            active_history["zi_phase"] = zi_base[:, :, np.newaxis] * phase_med[np.newaxis, np.newaxis, :]
            
            amp_out_bw, active_history["zi_amp"] = sosfilt(SOS_BAND, amp_in, axis=0, zi=active_history["zi_amp"])
            phase_out_bw, active_history["zi_phase"] = sosfilt(SOS_BAND, phase_in, axis=0, zi=active_history["zi_phase"])
            ready_amp_bw, ready_phase_bw = amp_out_bw[0], phase_out_bw[0]
            
            active_history["last_filtered_amp"] = ready_amp_bw.copy()
            active_history["last_filtered_phase"] = ready_phase_bw.copy()

        if SAVGOL_ENABLED:
            zi_sg_base = lfilter_zi(SG_COEFFS, [1.0])
            active_history["zi_amp_sg"] = zi_sg_base[:, np.newaxis] * amp_med[np.newaxis, :]
            active_history["zi_phase_sg"] = zi_sg_base[:, np.newaxis] * phase_med[np.newaxis, :]
            
            amp_out_sg, active_history["zi_amp_sg"] = lfilter(SG_COEFFS, [1.0], amp_in, axis=0, zi=active_history["zi_amp_sg"])
            phase_out_sg, active_history["zi_phase_sg"] = lfilter(SG_COEFFS, [1.0], phase_in, axis=0, zi=active_history["zi_phase_sg"])
            ready_amp_sg, ready_phase_sg = amp_out_sg[0], phase_out_sg[0]
            
            active_history["last_sg_amp"] = ready_amp_sg.copy()
            active_history["last_sg_phase"] = ready_phase_sg.copy()

    else:
        t_prev = active_history["last_ts"]
        amp_prev = active_history["last_amp_med"]
        phase_prev = active_history["last_phase_med"]
        
        interpolated_amps = []
        interpolated_phases = []
        
        # Защита от артефактов при подвисании порта (сброс окна, если задержка > 0.4 сек)
        if (t_curr - active_history["next_target_ts"]) > 0.4:
            active_history["next_target_ts"] = t_curr

        # Наращиваем пропущенные пакеты по линейной интерполяции
        while active_history["next_target_ts"] <= t_curr:
            t_target = active_history["next_target_ts"]
            alpha = np.clip((t_target - t_prev) / (t_curr - t_prev), 0.0, 1.0) if t_curr > t_prev else 1.0
            interpolated_amps.append(amp_prev + alpha * (amp_med - amp_prev))
            interpolated_phases.append(phase_prev + alpha * (phase_med - phase_prev))
            active_history["next_target_ts"] += target_dt
        
        active_history["last_ts"] = t_curr
        active_history["last_amp_med"] = amp_med.copy()
        active_history["last_phase_med"] = phase_med.copy()
        
        if len(interpolated_amps) > 0:
            amp_in = np.vstack(interpolated_amps)
            phase_in = np.vstack(interpolated_phases)
            
            if FILTER_ENABLED:
                amp_out_bw, active_history["zi_amp"] = sosfilt(SOS_BAND, amp_in, axis=0, zi=active_history["zi_amp"])
                phase_out_bw, active_history["zi_phase"] = sosfilt(SOS_BAND, phase_in, axis=0, zi=active_history["zi_phase"])
                ready_amp_bw, ready_phase_bw = amp_out_bw[-1], phase_out_bw[-1]
                active_history["last_filtered_amp"] = ready_amp_bw.copy()
                active_history["last_filtered_phase"] = ready_phase_bw.copy()

            if SAVGOL_ENABLED:
                amp_out_sg, active_history["zi_amp_sg"] = lfilter(SG_COEFFS, [1.0], amp_in, axis=0, zi=active_history["zi_amp_sg"])
                phase_out_sg, active_history["zi_phase_sg"] = lfilter(SG_COEFFS, [1.0], phase_in, axis=0, zi=active_history["zi_phase_sg"])
                ready_amp_sg, ready_phase_sg = amp_out_sg[-1], phase_out_sg[-1]
                active_history["last_sg_amp"] = ready_amp_sg.copy()
                active_history["last_sg_phase"] = ready_phase_sg.copy()
        else:
            # Если пакет пришел слишком быстро, отдаем предыдущее сглаженное значение
            if FILTER_ENABLED:
                ready_amp_bw = active_history["last_filtered_amp"].copy()
                ready_phase_bw = active_history["last_filtered_phase"].copy()
            if SAVGOL_ENABLED:
                ready_amp_sg = active_history["last_sg_amp"].copy()
                ready_phase_sg = active_history["last_sg_phase"].copy()

    # Финальная очистка невалидных поднесущих
    ready_amp_bw[~VALID_MASK] = 0.0
    ready_phase_bw[~VALID_MASK] = 0.0
    ready_amp_sg[~VALID_MASK] = 0.0
    ready_phase_sg[~VALID_MASK] = 0.0

    return ready_amp_bw, ready_phase_bw, ready_amp_sg, ready_phase_sg


# =====================================================================
# ГЛАВНАЯ ФУНКЦИЯ-ОРКЕСТРАТОР
# =====================================================================
def raw_csi_to_amp_phase(msg, file_handles):
    """
    Основная функция-конвейер обработки CSI. Собирает все этапы вместе.
    Ожидает file_handles в виде словаря:
    {"raw": fd, "ratio": fd, "sanitized": fd, "median": fd, "butter": fd, "savgol": fd}
    """
    raw_data = msg['data']
    timestamp = msg.get('timestamp', 'No_Time') 

    def write_stage(stage_key, amps, phases):
        """Локальная функция для записи данных в файл конкретного этапа."""
        handle = file_handles.get(stage_key)
        if handle and not handle.closed:
            vals = [f"{amps[i]:.3f},{phases[i]:.3f}" for i in range(128)]
            handle.write(f"{timestamp}," + ",".join(vals) + "\n")

    try:
        dt_obj = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S.%f')
        t_curr = dt_obj.timestamp()
    except Exception:
        t_curr = time.time()
    
    # Определяем к какому порту относятся данные по имени файла "raw" (или butter/savgol)
    sample_name = file_handles.get("raw", file_handles.get("butter")).name
    port_key = "csi_processed1" if "csi_processed1" in sample_name else "csi_processed2"
    
    p_state = reference_states[port_key]
    active_history = history_p1 if port_key == "csi_processed1" else history_p2

    # --- Базовое извлечение I/Q ---
    raw_np = np.array(raw_data, dtype=np.int8)
    max_sub = min(len(raw_np) // 2, 128)
    I = np.zeros(128, dtype=np.float32)
    Q = np.zeros(128, dtype=np.float32)
    
    I[:max_sub] = raw_np[0:max_sub*2:2]
    Q[:max_sub] = raw_np[1:max_sub*2:2]

    amplitudes_raw = np.sqrt(I**2 + Q**2)
    phases_raw = np.degrees(np.arctan2(Q, I))
    amplitudes_raw[~VALID_MASK] = 0.0

    # 💾 ЗАПИСЬ: Стадия "Сырые данные"
    write_stage("raw", amplitudes_raw, phases_raw)

    # --- ЭТАП 1 и 2: Выбор референса и мониторинг ---
    base_ref = monitor_and_select_reference(amplitudes_raw, I, Q, p_state, port_key)

    # --- ЭТАП 3: CSI Ratio ---
    amplitudes_ratio, phases_ratio = compute_csi_ratio(I, Q, base_ref, p_state["phase_offset"])
    
    # 💾 ЗАПИСЬ: Стадия "CSI Ratio"
    write_stage("ratio", amplitudes_ratio, phases_ratio)

    # --- ЭТАП 4: Развертка и санитация ---
    unwrapped_phases = unwrap_and_sanitize_phase(phases_ratio, active_history)
    
    # 💾 ЗАПИСЬ: Стадия "Санитация фазы" (амплитуда передается из Ratio)
    write_stage("sanitized", amplitudes_ratio, unwrapped_phases)

    # --- ЭТАП 5: Медианный фильтр (Амплитуда и Фаза) ---
    amp_med, phase_med = apply_median_filter(amplitudes_ratio, unwrapped_phases, active_history)
    
    # 💾 ЗАПИСЬ: Стадия "Медианный фильтр"
    write_stage("median", amp_med, phase_med)

    # --- ЭТАП 6 и 7: Интерполяция и Фильтрация (Butterworth, Savitzky-Golay) ---
    amp_bw, phase_bw, amp_sg, phase_sg = interpolate_and_filter(t_curr, amp_med, phase_med, active_history)

    # 💾 ЗАПИСЬ: Стадия "Баттерворт" и "Савицкий-Голей"
    write_stage("butter", amp_bw, phase_bw)
    write_stage("savgol", amp_sg, phase_sg)

    # Возвращаем списки для совместимости с остальным кодом
    return amplitudes_ratio.tolist(), amp_bw.tolist(), phase_med.tolist(), phase_bw.tolist(), amp_sg.tolist(), phase_sg.tolist()



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

    # 1. Определяем только пути к файлам (строки)
    paths_p1 = {
        "raw": "log/csi_processed1_raw.csv",
            "ratio": "log/csi_processed1_ratio.csv",
        "sanitized": "log/csi_processed1_sanitized.csv",
        "median": "log/csi_processed1_median.csv",
        "butter": "log/csi_processed1_butter.csv",
        "savgol": "log/csi_processed1_savgol.csv"
    }      

    paths_p2 = {
        "raw": "log/csi_processed2_raw.csv",
        "ratio": "log/csi_processed2_ratio.csv",
        "sanitized": "log/csi_processed2_sanitized.csv",
        "median": "log/csi_processed2_median.csv",
        "butter": "log/csi_processed2_butter.csv",
        "savgol": "log/csi_processed2_savgol.csv"
    }

    headers = ["time_stamp"]
    for i in range(128):
        headers.extend([f"amp_sub_{i}", f"phase_sub_{i}"])

    # 2. Создаем файлы и пишем заголовки (используя пути-строки)
    for path in [paths_p1["butter"], paths_p1["savgol"], paths_p2["butter"], paths_p2["savgol"]]:
        with open(path, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(headers)

    # 3. Теперь открываем файловые дескрипторы для работы в основном цикле
    file_handles_p1 = {k: open(v, 'a', newline='', encoding='utf-8') for k, v in paths_p1.items()}
    file_handles_p2 = {k: open(v, 'a', newline='', encoding='utf-8') for k, v in paths_p2.items()}


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

    print("--- Система запущена ---")


    # До цикла добавляем переменную для отслеживания состояния калибровки
    was_calibrating = False

    try:
        while True:
            is_calibrating_now = calibrator.is_active or calibrator.is_waiting

            # Если калибровка только что закончилась, обновляем базы анализаторов
            if was_calibrating and not is_calibrating_now:
                print("\n[СИСТЕМА] Калибровка завершена. Обновление эталонов в анализаторах...")
                analyzer_p1._load_baseline()
                analyzer_p2._load_baseline()
                # Очищаем буферы, чтобы старые данные не вызвали ложных срабатываний
                analyzer_p1.amp_bw_buffer.clear()
                analyzer_p1.amp_sg_buffer.clear()
                analyzer_p1.phase_bw_buffer.clear()
                analyzer_p2.amp_bw_buffer.clear()
                analyzer_p2.amp_sg_buffer.clear()
                analyzer_p2.phase_bw_buffer.clear()

            was_calibrating = is_calibrating_now

            # Обработка данных первой очереди
            try:
                msg1 = controller.queue_read1.get(timeout=0.05)
                t = msg1.get('type', 'Unknown')
                if t == 'CSI_DATA':
                    amp_raw, amp_bw, phase_raw, phase_bw, amp_sg, phase_sg = raw_csi_to_amp_phase(msg1, file_handles_p1)
                    calibrator.update(port_id=1, amp_bw=amp_bw, amp_sg=amp_sg)
                    
                    # Передаем данные в анализатор ТОЛЬКО если не идет калибровка
                    if not is_calibrating_now:
                        analyzer_p1.update(amp_bw=amp_bw, amp_sg=amp_sg, phase_bw=phase_bw, phase_sg=phase_sg)
                elif t == 'LOG_DATA':
                    print(f"[P1]: LOG - {msg1.get('data')}")
                elif t == 'FAIL_EVENT':
                    print(f"[P1]: КРИТИЧЕСКАЯ ОШИБКА - {msg1.get('data')}")
            except queue.Empty:
                pass

            # Обработка данных второй очереди (аналогично)
            try:
                msg2 = controller.queue_read2.get(timeout=0.05)
                t = msg2.get('type', 'Unknown')
                
                if t == 'CSI_DATA':
                    amp_raw, amp_bw, phase_raw, phase_bw, amp_sg, phase_sg = raw_csi_to_amp_phase(msg2, file_handles_p2)
                    calibrator.update(port_id=2, amp_bw=amp_bw, amp_sg=amp_sg)
                    
                    if not is_calibrating_now:
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
        # Закрываем все открытые файлы из словарей
        for fh in file_handles_p1.values(): 
            if not fh.closed: fh.close()
        for fh in file_handles_p2.values(): 
            if not fh.closed: fh.close()
            
        controller.p1.terminate()
        controller.p2.terminate()
    print("Запустите файл gui_visualizer.py для отображения интерфейса.")