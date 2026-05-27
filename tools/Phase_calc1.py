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
from scipy.signal import butter, filtfilt
from collections import deque

 # --- Butterworth bandpass filter helper ---
FILTER_ENABLED = True
 # Default band (Hz) and sampling rate - adjust as needed
FILTER_LOW_HZ = 0.5
FILTER_HIGH_HZ = 10
FILTER_ORDER = 4
SAMPLE_RATE = 75

HISTORY_DEPTH = 75 

 # Буферы для первого и второго порта
history_p1 = {"amp": [deque(maxlen=HISTORY_DEPTH) for _ in range(52)],
              "phase": [deque(maxlen=HISTORY_DEPTH) for _ in range(52)]}

history_p2 = {"amp": [deque(maxlen=HISTORY_DEPTH) for _ in range(52)],
              "phase": [deque(maxlen=HISTORY_DEPTH) for _ in range(52)]}


# --- Константы ---
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
        # Преобразование в знаковые целые (аналог логики из оригинала)
        return [b - 256 if b > 127 else b for b in bin_data]
    except Exception as e:
        print(f"Ошибка декодирования base64: {e}")
        return []



def serial_handle(queue_read, queue_write, port):
    try:
        # Убедитесь, что pyserial установлен (pip install pyserial)
        ser = serial.Serial(port=port, baudrate=2000000, bytesize=8, parity='N', stopbits=1, timeout=0.1)
    except Exception as e:
        print(f"Ошибка открытия порта {port}: {e}")
        queue_read.put({'type': 'FAIL_EVENT', 'data': f"Failed to open {port}"})
        return

    print(f"Порт {port} открыт.")
    ser.flushInput()

    # Создание папок
    for folder in ['log', 'data']:
        if not os.path.exists(folder):
            os.makedirs(folder)

    safe_port_name = port.replace('/', '_').replace('\\', '_')

    # Добавляем имя порта к названиям файлов
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

    ser.write(b"restart\r\n")
    
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
            print(line_str)

            matched = False
            for cfg in data_configs:
                if cfg["type"] in line_str:
                    matched = True
                    # Обрезаем строку до начала тега типа данных
                    start_idx = line_str.find(cfg["type"])
                    clean_line = line_str[start_idx:]
                    
                    csv_reader = csv.reader(StringIO(clean_line))
                    try:
                        row = next(csv_reader)
                    except StopIteration: continue

                    if len(row) == len(cfg["cols"]):
                        # Создаем словарь (замена pd.Series)
                        data_dict = dict(zip(cfg["cols"], row))
                        
                        # Валидация таймстемпа
                        ts = data_dict.get('timestamp', '')
                        try:
                            datetime.strptime(ts, '%Y-%m-%d %H:%M:%S.%f')
                        except:
                            data_dict['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

                        if cfg["type"] == 'CSI_DATA':
                            raw_csi = base64_decode_bin(data_dict['data'])
                            data_dict['data'] = raw_csi # Сохраняем как список чисел
                            
                            # Логика записи отдельных файлов для целей (Target)
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
                                
                                # Записываем строку в файл таргета
                                row_to_write = [data_dict[col] for col in CSI_DATA_COLUMNS]
                                target_csv_writer.writerow(row_to_write)
                                target_last, target_seq_last = current_target, current_seq

                        # Запись в общий лог-файл
                        row_to_write = [data_dict.get(col, '') for col in cfg["cols"]]
                        writers[cfg["type"]].writerow(row_to_write)
                        files_fds[cfg["type"]].flush()

                        # Отправка в основную очередь
                        if not queue_read.full():
                            queue_read.put(data_dict)
                    break
                    

            if not matched:
                # Обработка обычных системных логов ESP32
                clean_log = re.sub(r'\x1b\[[0-9;]*m', '', line_str) # Удаление ANSI цветов
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

def remove_linear_trend(phs, xs=None):
    # Убирает линейный тренд (a*x + b) методом наименьших квадратов, без numpy
    n = len(phs)
    if n == 0:
       return []
    if xs is None:
        xs = list(range(n))
    if len(xs) != n:
        xs = list(range(n))

    # Вычисляем усреднения
    sum_x = 0.0
    sum_y = 0.0
    for xi, yi in zip(xs, phs):
        sum_x += xi
        sum_y += yi
    mean_x = sum_x / n
    mean_y = sum_y / n

    num = 0.0
    den = 0.0
    for xi, yi in zip(xs, phs):
        dx = xi - mean_x
        dy = yi - mean_y
        num += dx * dy
        den += dx * dx

    if den == 0.0:
        slope = 0.0
    else:
        slope = num / den

    intercept = mean_y - slope * mean_x

        # Вычитаем найденную линию
    detrended = [yi - (slope * xi + intercept) for xi, yi in zip(xs, phs)]
    return detrended


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


def _get_median(lst):
    """Вспомогательная функция для поиска медианы в чистом Python."""
    sorted_lst = sorted(lst)
    n = len(sorted_lst)
    if n == 0:
        return 0
    if n % 2 == 1:
        return sorted_lst[n // 2]
    else:
        return (sorted_lst[n // 2 - 1] + sorted_lst[n // 2]) / 2.0

barrel = 0.0
barrel_av = 0.0 

def barrel(amps, time):

    if isinstance(time, str):
        time = datetime.strptime(time, "%Y-%m-%d %H:%M:%S.%f").timestamp()

    t1 = 0.0
    t2 = 0.0
    t1 = time
    T = float(t1)-float(t2)
    t2 = time 

    for i in range(0, len(amps)):
        barrel += amps[i]
        barrel_av = barrel / 60
        barrel -= barrel_av
        amps[i]=barrel_av
    return amps

def moving_average(data, window_size):
    """Фильтр низких частот (аналог ФНЧ)"""
    if not data:
        return []
    smoothed = []
    for i in range(len(data)):
        # Берем окно из последних window_size элементов
        start_idx = max(0, i - window_size + 1)
        window = data[start_idx : i + 1]
        # Считаем среднее арифметическое
        avg = sum(window) / len(window)
        smoothed.append(avg)
    return smoothed

def hampel_filter(data, window_size=5, n_sigmas=3):
    """
    Скользящее окно, которое заменяет аномальные скачки на медиану.
    Внимание: применяется ко времени (последовательности пакетов), 
    а не к поднесущим внутри одного пакета!
    """
    n = len(data)
    if n == 0:
        return []
    result = list(data) # Создаем копию
    k = 1.4826 # Коэффициент масштабирования для нормального распределения

    # Проходим скользящим окном
    for i in range(window_size, n - window_size):
        # Вырезаем окно
        window = data[i - window_size : i + window_size + 1]
        median = _get_median(window)

        # Вычисляем MAD (Медианное абсолютное отклонение)
        deviations = [abs(x - median) for x in window]
        mad = _get_median(deviations)

        threshold = n_sigmas * k * mad

        # Если точка слишком сильно отклоняется от медианы окна - срезаем
        if mad == 0:
            continue
        if abs(data[i] - median) > threshold:
            result[i] = median

    return result



def butter_bandpass(lowcut, highcut, fs, order=4):
    nyq = 0.5 * fs
    low = lowcut / nyq
    high = highcut / nyq
    if low <= 0:
        low = 1e-6
    if high >= 1:
        high = 0.999999
    b, a = butter(order, [low, high], btype='band')
    return b, a


b_band, a_band = butter_bandpass(FILTER_LOW_HZ, FILTER_HIGH_HZ, SAMPLE_RATE, FILTER_ORDER)


def bandpass_filter_fast(data):
    """Применяем заранее рассчитанный фильтр."""
    if not data:
        return []
    try:
        y = filtfilt(b_band, a_band, data)
        return [float(x) for x in y]
    except Exception:
        return data


def raw_csi_to_amp_phase(msg, processed_file):
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

    for i in range(len(amplitudes)):
        Iacurr=I[i]
        Qacurr=Q[i]
        Ia.append((Iacurr/10**(agc_gain/20))*math.sqrt(rssi_linear/sum_amp))
        Qa.append((Qacurr/10**(agc_gain/20))*math.sqrt(rssi_linear/sum_amp))


    x=0.0
    ref = 9
    for i in range(9,39):
        if amplitudes[i] > x:
            x=amplitudes[i]
            ref = i
    
    #CSI Ratio для обычных компл ампл (Нужно повторить для уебищных а потом уже считать)
    for i in range(len(amplitudes)):
        Incurr=(I[i]*I[ref]+Q[i]*Q[ref])/(I[ref]**2+Q[ref]**2)
        Qncurr=(Q[i]*I[ref]-I[i]*Q[ref])/(I[ref]**2+Q[ref]**2)
        In.append(Incurr)
        Qn.append(Qncurr)

    for i in range(len(amplitudes)):
        Iacurr=(Ia[i]*Ia[ref]+Qa[i]*Qa[ref])/(Ia[ref]**2+Qa[ref]**2)
        Qacurr=(Qa[i]*Ia[ref]-Ia[i]*Qa[ref])/(Ia[ref]**2+Qa[ref]**2)
        Ian.append(Iacurr)
        Qan.append(Qacurr)

    for i in range(len(amplitudes)):
        Incurr = In[i]
        Qncurr = Qn[i]
        Iacurr = Ian[i]
        Qacurr = Qan[i]
        amplitudes_f.append(math.sqrt(Iacurr**2 + Qacurr**2))
        phases_f.append(math.degrees(math.atan2(Qncurr, Incurr)))

    # for i in range(len(amplitudes_agc)):
    #     amplitudes_rssi.append(amplitudes_agc[i]*math.sqrt(rssi_linear/sum(amplitudes_agc)**2))

    # # Развёртка фазы (в градусах) перед записью
    unwrapped_phases = unwrap_phase_deg(phases_f)


    global history_p1, history_p2
    active_history = history_p1 if "csi_processed1" in processed_file else history_p2

    # Сюда сложим финальные отфильтрованные точки текущего пакета
    ready_amplitudes = []
    ready_phases = []

    # Шаг 5: Фильтрация ВО ВРЕМЕНИ для каждой поднесущей отдельно
    for idx in range(len(amplitudes_f)):
        # Добавляем текущие значения в историю этой конкретной поднесущей
        active_history["amp"][idx].append(amplitudes_f[idx])
        active_history["phase"][idx].append(unwrapped_phases[idx])

        # Переводим деку в обычный список для SciPy
        amp_series = list(active_history["amp"][idx])
        phase_series = list(active_history["phase"][idx])

        # Фильтр Баттерворта требует хотя бы ~15 пакетов истории, чтобы не вылетать
        if FILTER_ENABLED and len(amp_series) > 30:
            try:
                filtered_amps = bandpass_filter_fast(amp_series)
                filtered_phases = bandpass_filter_fast(phase_series)
                
                # Берем самый ПОСЛЕДНИЙ (актуальный) элемент из отфильтрованного временного ряда
                ready_amplitudes.append(filtered_amps[-1])
                ready_phases.append(filtered_phases[-1])
            except:
                # Если фильтр сбоит, берем сырое значение пакета
                ready_amplitudes.append(amplitudes_f[idx])
                ready_phases.append(unwrapped_phases[idx])
        else:
            # Пока история не накопилась, пишем сырые данные пакета
            ready_amplitudes.append(amplitudes_f[idx])
            ready_phases.append(unwrapped_phases[idx])



    # # Удаляем линейный сдвиг фазы перед записью
    # detrended_phases = remove_linear_trend(unwrapped_phases)

    # # Применяем фильтр Хампеля к амплитудам перед записью
    # try:
    #     amplitudes = hampel_filter(amplitudes)
    # except Exception:
    #     # В случае неожиданных данных оставляем оригинальные амплитуды
    #     pass


    #amplitudes = barrel(amplitudes, timestamp)


    
    # Запись в уже открытый дескриптор файла f_out (без повторного open/close)
    line = f"{timestamp},"
    for subcarrier_index in range(len(amplitudes)):
        amp = ready_amplitudes[subcarrier_index]
        ph = ready_phases[subcarrier_index]
        line += f"{amp},{ph},"
    f_out.write(line + "\n")

    # with open(processed_file, 'a', newline='', encoding='utf-8') as f:
    #     writer = csv.writer(f)
    #     for idx, amp in enumerate(amplitudes):
    #         current_time = timestamp if idx == 0 else ""
    #         writer.writerow([current_time, idx, round(amp, 2), round(phases[idx], 4)])
    #     # f.flush() # Внутри 'with' flush обычно не нужен, файл закроется сам


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
        """Send wifi_config command to both devices. Mirrors GUI logic from esp_csi_tool.py."""
        if not ssid:
            return
        cmd = f"wifi_config --ssid \"{ssid}\""
        if password and len(password) >= 8:
            cmd += f" --password {password}"
        self.send_command(cmd)


if __name__ == "__main__":
    processed_file1 = 'log/csi_processed1.csv'
    processed_file2 = 'log/csi_processed2.csv'
    os.makedirs('log', exist_ok=True) # Убеждаемся, что папка log существует

# Создаем файл и записываем заголовки (режим 'w' перезапишет старый файл при запуске)
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

    time.sleep(2)
    # Send router connect at startup if configuration exists (mirrors GUI behavior)

    try:
        with open('./config/gui_config.json', 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            ssid = cfg.get('router_ssid', '').strip()
            pwd = cfg.get('router_password', '').strip()
            if ssid:
                controller.send_command("radar --csi_output_type LLFT --csi_output_format base64")
                controller.router_connect(ssid, pwd)
                print(f"Sent router connect for SSID '{ssid}' at startup.")
    except Exception:
        pass

    print("--- Система запущена (без Pandas/Numpy) ---")
    print("Команды: 'locate router', 'exit'")


    f_out1 = open(processed_file1, 'a', newline='', encoding='utf-8')
    f_out2 = open(processed_file2, 'a', newline='', encoding='utf-8')

    try:
        while True:
            # Пытаемся достать данные из первой очереди
            try:
                # Ждем данные 0.1 сек, чтобы не перегружать процессор, 
                # но и не зависать навечно
                msg1 = controller.queue_read1.get(timeout=0.1)
                
                # Теперь обрабатываем полученное сообщение
                t = msg1.get('type', 'Unknown')
                
                if t == 'CSI_DATA':
                    # Передаем уже полученное сообщение в функцию обработки
                    raw_csi_to_amp_phase(msg1, f_out1)
                    print(f"[P1]: {t} обработано и записано")
                elif t == 'LOG_DATA':
                    print(f"[P1]: LOG - {msg1.get('data')}")
                else:
                    print(f"[P1]: Получен тип {t}")

            except queue.Empty:
                # Если в очереди1 пусто, просто идем дальше
                pass

            # То же самое для второй очереди (если нужно)
            try:
                msg2 = controller.queue_read2.get(timeout=0.1)
                
                 # Теперь обрабатываем полученное сообщение
                t = msg2.get('type', 'Unknown')
                
                if t == 'CSI_DATA':
                    # Передаем уже полученное сообщение в функцию обработки
                    raw_csi_to_amp_phase(msg2, f_out2)
                    print(f"[P2]: {t} обработано и записано")
                elif t == 'LOG_DATA':
                    print(f"[P2]: LOG - {msg2.get('data')}")
                else:
                    print(f"[P2]: Получен тип {t}")
            except queue.Empty:
                pass

    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        controller.p1.terminate()
        controller.p2.terminate()