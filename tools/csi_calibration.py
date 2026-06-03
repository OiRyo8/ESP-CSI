import numpy as np
import time
import json
import os

class CSICalibrator:
    def __init__(self, num_subcarriers=128):
        self.num_subcarriers = num_subcarriers
        self.is_active = False
        self.is_waiting = False  # Флаг состояния задержки перед стартом
        self.delay_duration = 15  # Задержка в секундах (настройте под себя)
        self.start_time = 0
        self.duration = 0
        
        # Буферы для сбора данных в реальном времени (отдельно для BW и SG)
        self.buffer_p1_amp_bw = []
        self.buffer_p1_amp_sg = []
        self.buffer_p2_amp_bw = []
        self.buffer_p2_amp_sg = []
        
        # Словарь для хранения вычисленных эталонов
        self.baseline = {
            "p1": {"amp_bw_mean": None, "amp_bw_var": None, "amp_sg_mean": None, "amp_sg_var": None},
            "p2": {"amp_bw_mean": None, "amp_bw_var": None, "amp_sg_mean": None, "amp_sg_var": None}
        }
        
        os.makedirs('config', exist_ok=True)
        self.config_path = 'config/baseline.json'

    def start(self, duration_sec=15):
        print(f"\n[КАЛИБРОВКА] Внимание! Сбор базовой линии начнется через {self.delay_duration} секунд.")
        print("[КАЛИБРОВКА] Пожалуйста, ПОКИНЬТЕ зону видимости радаров...")
        
        self.is_waiting = True
        self.is_active = False
        self.duration = duration_sec
        self.start_time = time.time() # Время начала отсчета паузы
        
        # Очищаем старые буферы заранее
        self.buffer_p1_amp_bw.clear()
        self.buffer_p1_amp_sg.clear()
        self.buffer_p2_amp_bw.clear()
        self.buffer_p2_amp_sg.clear()

    def update(self, port_id, amp_bw, amp_sg):
        """Неблокирующее добавление нового пакета с учетом задержки старта"""
        current_time = time.time()

        # 1. Если мы в режиме ожидания (обратный отсчет)
        if self.is_waiting:
            if current_time - self.start_time >= self.delay_duration:
                # Время задержки прошло, переключаемся в режим записи
                self.is_waiting = False
                self.is_active = True
                self.start_time = time.time() # Перезапускаем таймер для самой калибровки
                print(f"\n[КАЛИБРОВКА] СТАРТ! Сбор данных запущен на {self.duration} секунд(ы).")
            else:
                # Пока идет задержка — просто игнорируем входящие пакеты
                return

        # 2. Если калибровка уже активно собирает данные
        if self.is_active:
            if port_id == 1:
                self.buffer_p1_amp_bw.append(amp_bw)
                self.buffer_p1_amp_sg.append(amp_sg)
            elif port_id == 2:
                self.buffer_p2_amp_bw.append(amp_bw)
                self.buffer_p2_amp_sg.append(amp_sg)

            if current_time - self.start_time >= self.duration:
                self._finish()

    def _finish(self):
        self.is_active = False
        self.is_waiting = False
        print("\n[КАЛИБРОВКА] Сбор данных завершен. Вычисляем дисперсию матриц...")
        
        def calculate_stats(buffer):
            if not buffer: return None, None
            arr = np.array(buffer)
            return np.mean(arr, axis=0), np.var(arr, axis=0)

        # Вычисляем для P1
        self.baseline["p1"]["amp_bw_mean"], self.baseline["p1"]["amp_bw_var"] = calculate_stats(self.buffer_p1_amp_bw)
        self.baseline["p1"]["amp_sg_mean"], self.baseline["p1"]["amp_sg_var"] = calculate_stats(self.buffer_p1_amp_sg)
        
        # Вычисляем для P2
        self.baseline["p2"]["amp_bw_mean"], self.baseline["p2"]["amp_bw_var"] = calculate_stats(self.buffer_p2_amp_bw)
        self.baseline["p2"]["amp_sg_mean"], self.baseline["p2"]["amp_sg_var"] = calculate_stats(self.buffer_p2_amp_sg)

        self._save_to_file()
        print(f"[КАЛИБРОВКА] Успешно! Собранные пакеты P1: {len(self.buffer_p1_amp_bw)}, P2: {len(self.buffer_p2_amp_bw)}")

    def _save_to_file(self):
        data_to_save = {}
        for port in ["p1", "p2"]:
            data_to_save[port] = {
                k: (v.tolist() if v is not None else []) for k, v in self.baseline[port].items()
            }
            
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, indent=4)
            
    def load_from_file(self):
        if not os.path.exists(self.config_path):
            print("[КАЛИБРОВКА] Файл базовой линии не найден. Введите 'calibrate 15' для создания эталона.")
            return False
            
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for port in ["p1", "p2"]:
                    for key in ["amp_bw_mean", "amp_bw_var", "amp_sg_mean", "amp_sg_var"]:
                        self.baseline[port][key] = np.array(data[port][key])
            print("[КАЛИБРОВКА] Базовые эталоны для Баттерворта и Савицкого-Голея успешно загружены.")
            return True
        except Exception as e:
            print(f"[КАЛИБРОВКА] Ошибка чтения файла: {e}")
            return False