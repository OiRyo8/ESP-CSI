import numpy as np
import time
from collections import deque
import json

class CSIAnalyzer:
    def __init__(self, port_id, sample_rate=50, window_sec=10, update_interval_sec=1.0):
        
        self.port_id = port_id # 'p1' или 'p2'
        self.fs = sample_rate
        self.window_size = int(sample_rate * window_sec)
        self.update_interval = int(sample_rate * update_interval_sec)
        

        self.amp_bw_buffer = deque(maxlen=self.window_size)
        self.amp_sg_buffer = deque(maxlen=self.window_size)
        self.phase_bw_buffer = deque(maxlen=self.window_size)
        
        self.packet_count = 0
        
        # Динамические пороги (будут загружены из файла, это дефолты)
        self.thresh_presence_bw = 1.5
        self.thresh_presence_sg = 1.5
        self.thresh_movement_bw = 3.0
        self.thresh_movement_sg = 3.0
        
        self.baseline_bw_var = None
        self.baseline_sg_var = None
        self._load_baseline()

        self.current_state = "EMPTY"       
        self.candidate_state = "EMPTY"     
        self.candidate_count = 0           
        self.DEBOUNCE_THRESH = 3 
        self.seconds_since_last_bio = 0.0

    def _load_baseline(self):
        try:
            with open('config/baseline.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                # Загружаем дисперсии
                bw_var = np.array(data[self.port_id]["amp_bw_var"])
                sg_var = np.array(data[self.port_id]["amp_sg_var"])
                self.baseline_bw_var = np.mean(bw_var[bw_var > 0])
                self.baseline_sg_var = np.mean(sg_var[sg_var > 0])
                
                # Загружаем индивидуальные пороги
                self.thresh_presence_bw = data[self.port_id].get("thresh_presence_bw", 1.5)
                self.thresh_presence_sg = data[self.port_id].get("thresh_presence_sg", 1.5)
                self.thresh_movement_bw = data[self.port_id].get("thresh_movement_bw", 3.0)
                self.thresh_movement_sg = data[self.port_id].get("thresh_movement_sg", 3.0)
                
                print(f"[АНАЛИТИКА {self.port_id.upper()}] Базы загружены. Пороги: Присутствие x{self.thresh_presence_bw:.1f}, Движение x{self.thresh_movement_bw:.1f}")
        except Exception:
            print(f"[АНАЛИТИКА {self.port_id.upper()}] ПРЕДУПРЕЖДЕНИЕ: baseline.json не найден. Использую дефолты.")
            self.baseline_bw_var = 1.0
            self.baseline_sg_var = 1.0
            self.thresh_presence_bw = 1.5
            self.thresh_presence_sg = 1.5
            self.thresh_movement_bw = 3.0
            self.thresh_movement_sg = 3.0

    def update(self, amp_bw, amp_sg, phase_bw, phase_sg):
        amp_bw = np.array(amp_bw)
        amp_sg = np.array(amp_sg)
        phase_bw = np.array(phase_bw)
        
        # Проверяем, что пакет не пустой
        if np.sum(amp_bw) == 0 or np.sum(amp_sg) == 0: 
            return

        # Добавляем массивы целиком (форма буфера станет двумерной: [window_size, 128])
        self.amp_bw_buffer.append(amp_bw)
        self.amp_sg_buffer.append(amp_sg)
        self.phase_bw_buffer.append(np.mean(phase_bw[phase_bw != 0])) # Фазу для FFT можно оставить скаляром
        
        self.packet_count += 1
        
        if len(self.amp_sg_buffer) == self.window_size and self.packet_count % self.update_interval == 0:
            self._analyze_window()
            
    def _analyze_window(self):
        # Конвертируем деки в 2D numpy-массивы формы (window_size, 128)
        arr_bw = np.array(self.amp_bw_buffer)
        arr_sg = np.array(self.amp_sg_buffer)
        
        # 1. Считаем дисперсию по оси времени для каждой поднесущей отдельно
        vars_bw = np.var(arr_bw, axis=0)
        vars_sg = np.var(arr_sg, axis=0)
        
        # 2. Усредняем только валидные дисперсии (исключая пустые поднесущие)
        current_bw_var = np.mean(vars_bw[vars_bw > 0]) if np.any(vars_bw > 0) else 0
        current_sg_var = np.mean(vars_sg[vars_sg > 0]) if np.any(vars_sg > 0) else 0
        
        # Дальнейшая логика сравнения с порогами остается без изменений
        ratio_bw = current_bw_var / self.baseline_bw_var if self.baseline_bw_var else 0
        ratio_sg = current_sg_var / self.baseline_sg_var if self.baseline_sg_var else 0
        
        # Индивидуальная проверка фильтров по их личным динамическим порогам
        is_movement = (ratio_bw >= self.thresh_movement_bw) or (ratio_sg >= self.thresh_movement_sg)
        is_presence = (ratio_bw >= self.thresh_presence_bw) or (ratio_sg >= self.thresh_presence_sg)
        
        if is_movement:
            instant_state = "MOVEMENT"
        elif is_presence:
            instant_state = "PRESENCE"
        else:
            instant_state = "EMPTY"
            
        # --- Алгоритм дебаунса (инерции) ---
        if instant_state == self.current_state:
            self.candidate_state = self.current_state
            self.candidate_count = 0
        else:
            if instant_state == self.candidate_state:
                self.candidate_count += 1
            else:
                self.candidate_state = instant_state
                self.candidate_count = 1
            
            if self.candidate_count >= self.DEBOUNCE_THRESH:
                old_state = self.current_state
                self.current_state = self.candidate_state
                self.candidate_count = 0
                print(f"\n--- [{self.port_id.upper()} СТАТУС ИЗМЕНЕН: {old_state} -> {self.current_state}] | Отношение к базе: BW x{ratio_bw:.1f}, SG x{ratio_sg:.1f} ---")

        self.seconds_since_last_bio += (self.update_interval / self.fs)
        if self.seconds_since_last_bio >= 15.0:
            self.seconds_since_last_bio = 0.0
            if self.current_state == "PRESENCE":
                self._analyze_breathing()
            elif self.current_state == "MOVEMENT":
                self._analyze_steps()

    def _analyze_breathing(self):
        signal = np.array(self.phase_bw_buffer)
        freqs, fft_magnitude = self._compute_fft(signal)
        mask = (freqs >= 0.15) & (freqs <= 0.5)
        if not np.any(mask): return
        breath_freq = freqs[mask][np.argmax(fft_magnitude[mask])]
        bpm = breath_freq * 60 
        anomaly_flag = "⚠️ АНОМАЛИЯ!" if bpm < 10 or bpm > 25 else "В норме"
        print(f"[{self.port_id.upper()} БИОМЕТРИЯ] Дыхание: {bpm:.1f} вдохов/мин | {anomaly_flag}")

    def _analyze_steps(self):
        signal = np.array(self.phase_bw_buffer)
        freqs, fft_magnitude = self._compute_fft(signal)
        mask = (freqs >= 1.0) & (freqs <= 2.5)
        if not np.any(mask): return
        step_freq = freqs[mask][np.argmax(fft_magnitude[mask])]
        steps_per_min = step_freq * 60
        print(f"[{self.port_id.upper()} БИОМЕТРИЯ] Движение: {step_freq:.2f} Гц (~{steps_per_min:.0f} шагов/мин)")

    def _compute_fft(self, signal):
        signal = signal - np.mean(signal)
        window = np.hanning(len(signal))
        signal = signal * window
        fft_vals = np.abs(np.fft.rfft(signal))
        freqs = np.fft.rfftfreq(len(signal), d=1.0/self.fs)
        return freqs, fft_vals