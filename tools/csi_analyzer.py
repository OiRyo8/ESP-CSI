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
        
        # Буферы для накопления окон (усредненные по поднесущим значения)
        self.amp_bw_buffer = deque(maxlen=self.window_size)
        self.amp_sg_buffer = deque(maxlen=self.window_size)
        self.phase_bw_buffer = deque(maxlen=self.window_size)
        
        self.packet_count = 0
        
        self.THRESH_PRESENCE_MULTIPLIER = 1.0  
        self.THRESH_MOVEMENT_MULTIPLIER = 2.0
        
        self.baseline_bw_var = None
        self.baseline_sg_var = None
        self._load_baseline()

        # --- Новые переменные для стабилизации состояний и таймингов биометрии ---
        self.current_state = "EMPTY"       # Текущий подтвержденный статус
        self.candidate_state = "EMPTY"     # Потенциальный новый статус
        self.candidate_count = 0           # Счетчик циклов удержания кандидата
        
        # Порог подтверждения (3 окна подряд = 3 секунды при update_interval_sec=1.0)
        self.DEBOUNCE_THRESH = 3 
        
        # Таймер для вывода биометрии строго раз в 15 секунд
        self.seconds_since_last_bio = 0.0

    def _load_baseline(self):
        try:
            with open('config/baseline.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                # Загружаем дисперсии для конкретного порта
                bw_var = np.array(data[self.port_id]["amp_bw_var"])
                sg_var = np.array(data[self.port_id]["amp_sg_var"])
                
                # Берем среднее только по полезным поднесущим
                self.baseline_bw_var = np.mean(bw_var[bw_var > 0])
                self.baseline_sg_var = np.mean(sg_var[sg_var > 0])
                
                print(f"[АНАЛИТИКА {self.port_id.upper()}] Базы загружены. BW var: {self.baseline_bw_var:.4f}, SG var: {self.baseline_sg_var:.4f}")
        except Exception:
            print(f"[АНАЛИТИКА {self.port_id.upper()}] ПРЕДУПРЕЖДЕНИЕ: baseline.json не найден или поврежден. Использую 1.0")
            self.baseline_bw_var = 1.0
            self.baseline_sg_var = 1.0

    def update(self, amp_bw, amp_sg, phase_bw, phase_sg):
        amp_bw = np.array(amp_bw)
        amp_sg = np.array(amp_sg)
        phase_bw = np.array(phase_bw)
        
        valid_amp_bw = amp_bw[amp_bw != 0]
        valid_amp_sg = amp_sg[amp_sg != 0]
        valid_phase = phase_bw[phase_bw != 0]
        
        if len(valid_amp_sg) == 0 or len(valid_amp_bw) == 0: return

        self.amp_bw_buffer.append(np.mean(valid_amp_bw))
        self.amp_sg_buffer.append(np.mean(valid_amp_sg))
        self.phase_bw_buffer.append(np.mean(valid_phase))
        
        self.packet_count += 1
        
        if len(self.amp_sg_buffer) == self.window_size and self.packet_count % self.update_interval == 0:
            self._analyze_window()

    def _analyze_window(self):
        current_bw_var = np.var(self.amp_bw_buffer)
        current_sg_var = np.var(self.amp_sg_buffer)
        
        ratio_bw = current_bw_var / self.baseline_bw_var if self.baseline_bw_var else 0
        ratio_sg = current_sg_var / self.baseline_sg_var if self.baseline_sg_var else 0
        
        # Берем максимальное отклонение среди двух фильтров для надежности
        max_ratio = max(ratio_bw, ratio_sg)
        
        # Определяем мгновенное состояние на текущем шаге
        if max_ratio < self.THRESH_PRESENCE_MULTIPLIER:
            instant_state = "EMPTY"
        elif max_ratio < self.THRESH_MOVEMENT_MULTIPLIER:
            instant_state = "PRESENCE"
        else:
            instant_state = "MOVEMENT"
            
        # --- Алгоритм дебаунса (инерции) для смены статуса ---
        if instant_state == self.current_state:
            # Если мгновенное состояние совпадает с текущим подтвержденным, сбрасываем счетчик кандидата
            self.candidate_state = self.current_state
            self.candidate_count = 0
        else:
            # Если мгновенное состояние отличается, проверяем, закрепилось ли оно
            if instant_state == self.candidate_state:
                self.candidate_count += 1
            else:
                self.candidate_state = instant_state
                self.candidate_count = 1
            
            # Статус меняется в консоли ТОЛЬКО при фиксации длительного изменения
            if self.candidate_count >= self.DEBOUNCE_THRESH:
                old_state = self.current_state
                self.current_state = self.candidate_state
                self.candidate_count = 0
                print(f"\n--- [{self.port_id.upper()} СТАТУС ИЗМЕНЕН: {old_state} -> {self.current_state}] | Отношение к базе: BW x{ratio_bw:.1f}, SG x{ratio_sg:.1f} ---")

        # --- Таймер периодического обновления биометрии (каждые 15 секунд) ---
        self.seconds_since_last_bio += (self.update_interval / self.fs)
        
        if self.seconds_since_last_bio >= 15.0:
            self.seconds_since_last_bio = 0.0  # Сброс таймера
            
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