import argparse
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

    # Настройка лог-файлов (замена DataFrame на список словарей)
    data_configs = [
        {"type": "CSI_DATA", "cols": CSI_DATA_COLUMNS, "path": "log/csi_data.csv"},
        {"type": "RADAR_DADA", "cols": RADAR_DATA_COLUMNS, "path": "log/radar_data.csv"},
        {"type": "DEVICE_INFO", "cols": DEVICE_INFO_COLUMNS, "path": "log/device_info.csv"}
    ]

    files_fds = {}
    writers = {}

    for cfg in data_configs:
        fd = open(cfg["path"], 'a', encoding='utf-8', newline='')
        writer = csv.writer(fd)
        writer.writerow(cfg["cols"])
        files_fds[cfg["type"]] = fd
        writers[cfg["type"]] = writer

    log_data_writer = open("log/log_data.txt", 'a', encoding='utf-8')
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



class RadarController:
    def __init__(self, port1, port2):
        self.p1_name = port1.strip(", ")
        self.p2_name = port2.strip(", ")
        
        self.queue_write1 = Queue(maxsize=64)
        self.queue_write2 = Queue(maxsize=64)
        self.queue_read1 = Queue(maxsize=10) 
        self.queue_read2 = Queue(maxsize=10)
        
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

    try:
        while True:
            controller.send_command("get_csi")
            # Чтение сообщений из очередей
            while not controller.queue_read1.empty():
                msg = controller.queue_read1.get()
                t = msg.get('type', 'Unknown')
                print(f"[P1]: {t} получено")

            while not controller.queue_read2.empty():
                msg = controller.queue_read2.get()
                t = msg.get('type', 'Unknown')
                print(f"[P2]: {t} получено")

            user_input = input(">> ").strip().lower()

            if user_input == "locate router":
                controller.send_command("get_csi")
                print("Команда отправлена.")
            elif user_input == "exit":
                break
            elif user_input == "":
                continue
            else:
                print(f"Неизвестная команда: {user_input}")

    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        controller.p1.terminate()
        controller.p2.terminate()
        print("Готово.")