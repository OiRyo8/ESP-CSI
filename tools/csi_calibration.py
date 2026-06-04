import numpy as np
import time
import json
import os

class CSICalibrator:
    def __init__(self, num_subcarriers=128):
        self.num_subcarriers = num_subcarriers
        self.is_active = False
        self.is_waiting = False
        self.delay_duration = 15
        self.start_time = 0
        self.duration = 0
        self.mode = "empty" # Режимы: "empty", "presence", "movement"
        
        self.buffer_p1_amp_bw = []
        self.buffer_p1_amp_sg = []
        self.buffer_p2_amp_bw = []
        self.buffer_p2_amp_sg = []
        
        # Расширенный словарь для хранения эталонов и динамических порогов
        self.baseline = {
            "p1": {"amp_bw_mean": None, "amp_bw_var": None, "amp_sg_mean": None, "amp_sg_var": None,
                   "thresh_presence_bw": 1.5, "thresh_movement_bw": 3.0,
                   "thresh_presence_sg": 1.5, "thresh_movement_sg": 3.0},
            "p2": {"amp_bw_mean": None, "amp_bw_var": None, "amp_sg_mean": None, "amp_sg_var": None,
                   "thresh_presence_bw": 1.5, "thresh_movement_bw": 3.0,
                   "thresh_presence_sg": 1.5, "thresh_movement_sg": 3.0}
        }
        
        os.makedirs('config', exist_ok=True)
        self.config_path = 'config/baseline.json'

    def start(self, duration_sec=15, mode="empty"):
        self.mode = mode
        mode_names = {"empty": "ПУСТАЯ КОМНАТА", "presence": "ПРИСУТСТВИЕ (Дыхание)", "movement": "ДВИЖЕНИЕ (Ходьба)"}
        print(f"\n[КАЛИБРОВКА] Выбран режим: {mode_names.get(mode, 'НЕИЗВЕСТНО')}")
        
        if mode == "empty":
            print(f"[КАЛИБРОВКА] Покиньте помещение. Сбор базовой линии начнется через {self.delay_duration} сек.")
        elif mode == "presence":
            print(f"[КАЛИБРОВКА] Займите место и сидите спокойно (дышите). Сбор начнется через {self.delay_duration} сек.")
        elif mode == "movement":
            print(f"[КАЛИБРОВКА] Ходите по комнате. Сбор данных начнется через {self.delay_duration} сек.")

        self.is_waiting = True
        self.is_active = False
        self.duration = duration_sec
        self.start_time = time.time()
        
        self.buffer_p1_amp_bw.clear()
        self.buffer_p1_amp_sg.clear()
        self.buffer_p2_amp_bw.clear()
        self.buffer_p2_amp_sg.clear()

    def update(self, port_id, amp_bw, amp_sg):
        current_time = time.time()

        if self.is_waiting:
            if current_time - self.start_time >= self.delay_duration:
                self.is_waiting = False
                self.is_active = True
                self.start_time = time.time()
                print(f"\n[КАЛИБРОВКА] СТАРТ ЗАПИСИ! Собираем данные {self.duration} секунд(ы).")
            else:
                return

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
        print("\n[КАЛИБРОВКА] Сбор данных завершен. Анализирую отклонения...")
        
        def calculate_stats(buffer):
            if not buffer: return None, None
            arr = np.array(buffer)
            return np.mean(arr, axis=0), np.var(arr, axis=0)

        # Статистика текущей сессии
        p1_bw_m, p1_bw_v = calculate_stats(self.buffer_p1_amp_bw)
        p1_sg_m, p1_sg_v = calculate_stats(self.buffer_p1_amp_sg)
        p2_bw_m, p2_bw_v = calculate_stats(self.buffer_p2_amp_bw)
        p2_sg_m, p2_sg_v = calculate_stats(self.buffer_p2_amp_sg)

        if self.mode == "empty":
            # Перезаписываем базу
            self.baseline["p1"]["amp_bw_mean"], self.baseline["p1"]["amp_bw_var"] = p1_bw_m, p1_bw_v
            self.baseline["p1"]["amp_sg_mean"], self.baseline["p1"]["amp_sg_var"] = p1_sg_m, p1_sg_v
            self.baseline["p2"]["amp_bw_mean"], self.baseline["p2"]["amp_bw_var"] = p2_bw_m, p2_bw_v
            self.baseline["p2"]["amp_sg_mean"], self.baseline["p2"]["amp_sg_var"] = p2_sg_m, p2_sg_v
            print("[КАЛИБРОВКА] Эталон 'Пустая комната' успешно обновлен.")
            
        elif self.mode in ["presence", "movement"]:
            # Анализируем отклонение от базы
            for port, curr_bw_v, curr_sg_v in [("p1", p1_bw_v, p1_sg_v), ("p2", p2_bw_v, p2_sg_v)]:
                if self.baseline[port]["amp_bw_var"] is None:
                    print(f"[КАЛИБРОВКА] ОШИБКА: Нет базовой линии для {port.upper()}. Сначала выполните 'calibrate empty'.")
                    continue
                
                # Скалярная база
                base_bw_arr = np.array(self.baseline[port]["amp_bw_var"])
                base_sg_arr = np.array(self.baseline[port]["amp_sg_var"])
                base_bw = np.mean(base_bw_arr[base_bw_arr > 0])
                base_sg = np.mean(base_sg_arr[base_sg_arr > 0])
                
                # Скалярная текущая дисперсия
                curr_bw = np.mean(curr_bw_v[curr_bw_v > 0])
                curr_sg = np.mean(curr_sg_v[curr_sg_v > 0])
                
                # Во сколько раз текущая активность сильнее фона
                ratio_bw = curr_bw / base_bw if base_bw > 0 else 1.0
                ratio_sg = curr_sg / base_sg if base_sg > 0 else 1.0
                
                if self.mode == "presence":
                    # Ставим порог присутствия ровно посередине между фоном (1.0) и зафиксированным дыханием
                    self.baseline[port]["thresh_presence_bw"] = max(1.1, (1.0 + ratio_bw) / 2)
                    self.baseline[port]["thresh_presence_sg"] = max(1.1, (1.0 + ratio_sg) / 2)
                    print(f"[{port.upper()}] Порог ПРИСУТСТВИЯ установлен: BW=x{self.baseline[port]['thresh_presence_bw']:.2f}, SG=x{self.baseline[port]['thresh_presence_sg']:.2f}")
                
                elif self.mode == "movement":
                    # Ставим порог движения посередине между присутствием и ходьбой
                    pres_bw = self.baseline[port].get("thresh_presence_bw", 1.5)
                    pres_sg = self.baseline[port].get("thresh_presence_sg", 1.5)
                    self.baseline[port]["thresh_movement_bw"] = max(pres_bw + 0.5, (pres_bw + ratio_bw) / 2)
                    self.baseline[port]["thresh_movement_sg"] = max(pres_sg + 0.5, (pres_sg + ratio_sg) / 2)
                    print(f"[{port.upper()}] Порог ДВИЖЕНИЯ установлен: BW=x{self.baseline[port]['thresh_movement_bw']:.2f}, SG=x{self.baseline[port]['thresh_movement_sg']:.2f}")

        self._save_to_file()

    def _save_to_file(self):
        data_to_save = {}
        for port in ["p1", "p2"]:
            data_to_save[port] = {}
            for k, v in self.baseline[port].items():
                # Конвертируем numpy массивы в списки, числа оставляем как есть
                data_to_save[port][k] = v.tolist() if isinstance(v, np.ndarray) else v
                
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, indent=4)
            
    def load_from_file(self):
        if not os.path.exists(self.config_path):
            print("[КАЛИБРОВКА] Файл базовой линии не найден.")
            return False
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for port in ["p1", "p2"]:
                    for key in ["amp_bw_mean", "amp_bw_var", "amp_sg_mean", "amp_sg_var"]:
                        if data[port].get(key): self.baseline[port][key] = np.array(data[port][key])
                    for key in ["thresh_presence_bw", "thresh_movement_bw", "thresh_presence_sg", "thresh_movement_sg"]:
                        if key in data[port]: self.baseline[port][key] = float(data[port][key])
            print("[КАЛИБРОВКА] Динамические эталоны и пороги успешно загружены.")
            return True
        except Exception as e:
            print(f"[КАЛИБРОВКА] Ошибка чтения файла: {e}")
            return False