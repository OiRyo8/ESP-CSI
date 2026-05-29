# gui_visualizer.py
import sys
import os
import queue
import json
import argparse
from collections import deque
import csv

from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QComboBox
from PyQt6.QtCore import QTimer
import pyqtgraph as pg

# Импортируем контроллер и вычислительную функцию из вашего основного файла
from Phase_calc2 import RadarController, raw_csi_to_amp_phase

class CsiVisualizer(QMainWindow):
    def __init__(self, port1, port2):
        super().__init__()
        self.setWindowTitle("ESP32 CSI Real-Time Visualizer")
        self.resize(1100, 750)

        self.processed_file1 = 'log/csi_processed1.csv'
        self.processed_file2 = 'log/csi_processed2.csv'
        os.makedirs('log', exist_ok=True)

        for f_name in [self.processed_file1, self.processed_file2]:
            with open(f_name, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(["time_stamp"])

        self.f_out1 = open(self.processed_file1, 'a', newline='', encoding='utf-8')
        self.f_out2 = open(self.processed_file2, 'a', newline='', encoding='utf-8')

        # Инициализация фоновых процессов получения CSI data
        self.controller = RadarController(port1, port2)
        self.controller.start()

        # Глубина истории точек на самом графике
        self.ui_history_depth = 300
        
        # Структура для хранения скользящего окна точек графиков (52 поднесущие)
        self.ui_plot_data = {
            "p1": {
                "amp_f": [deque(maxlen=self.ui_history_depth) for _ in range(52)],
                "ready_amp": [deque(maxlen=self.ui_history_depth) for _ in range(52)],
                "phase_unwrapped": [deque(maxlen=self.ui_history_depth) for _ in range(52)],
                "ready_phase": [deque(maxlen=self.ui_history_depth) for _ in range(52)]
            },
            "p2": {
                "amp_f": [deque(maxlen=self.ui_history_depth) for _ in range(52)],
                "ready_amp": [deque(maxlen=self.ui_history_depth) for _ in range(52)],
                "phase_unwrapped": [deque(maxlen=self.ui_history_depth) for _ in range(52)],
                "ready_phase": [deque(maxlen=self.ui_history_depth) for _ in range(52)]
            }
        }

        self.init_ui()
        self.load_auto_config()

        # Высокопроизводительный таймер отрисовки интерфейса (20 мс)
        self.timer = QTimer()
        self.timer.timeout.connect(self.process_queues)
        self.timer.start(20)

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Панель селекторов
        control_layout = QHBoxLayout()
        control_layout.addWidget(QLabel("Активный порт:"))
        self.port_select = QComboBox()
        self.port_select.addItems(["Порт 1 (p1)", "Порт 2 (p2)"])
        control_layout.addWidget(self.port_select)

        control_layout.addWidget(QLabel("Поднесущая (0-51):"))
        self.subcarrier_select = QSpinBox()
        self.subcarrier_select.setRange(0, 51)
        self.subcarrier_select.setValue(10)
        control_layout.addWidget(self.subcarrier_select)
        
        control_layout.addStretch()
        main_layout.addLayout(control_layout)

        # Сетка pyqtgraph
        pg.setConfigOptions(antialias=True)
        self.graphics_layout = pg.GraphicsLayoutWidget()
        main_layout.addWidget(self.graphics_layout)

        # Создание 4 графиков
        self.p_amp_f = self.graphics_layout.addPlot(title="Исходная амплитуда (amplitudes_f)")
        self.curve_amp_f = self.p_amp_f.plot(pen=pg.mkPen('c', width=1.5))
        self.p_amp_f.showGrid(x=True, y=True)

        self.p_ready_amp = self.graphics_layout.addPlot(title="Отфильтрованная амплитуда (ready_amplitudes)")
        self.curve_ready_amp = self.p_ready_amp.plot(pen=pg.mkPen('g', width=1.5))
        self.p_ready_amp.showGrid(x=True, y=True)

        self.graphics_layout.nextRow()

        self.p_phase_unwrapped = self.graphics_layout.addPlot(title="Развернутая фаза (phase_series_unwrapped)")
        self.curve_phase_unwrapped = self.p_phase_unwrapped.plot(pen=pg.mkPen('m', width=1.5))
        self.p_phase_unwrapped.showGrid(x=True, y=True)

        self.p_ready_phase = self.graphics_layout.addPlot(title="Отфильтрованная фаза (ready_phases)")
        self.curve_ready_phase = self.p_ready_phase.plot(pen=pg.mkPen('y', width=1.5))
        self.p_ready_phase.showGrid(x=True, y=True)

    def load_auto_config(self):
        try:
            if os.path.exists('./config/gui_config.json'):
                with open('./config/gui_config.json', 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                    ssid = cfg.get('router_ssid', '').strip()
                    pwd = cfg.get('router_password', '').strip()
                    if ssid:
                        self.controller.send_command("radar --csi_output_type LLFT --csi_output_format base64")
                        self.controller.router_connect(ssid, pwd)
                        print(f"Конфигурация отправлена на роутер: {ssid}")
        except Exception as e:
            print(f"Ошибка чтения автоконфига: {e}")

    def process_queues(self):
        # Читаем Порт 1
        while True:
            try:
                msg = self.controller.queue_read1.get_nowait()
                if msg.get('type') == 'CSI_DATA':
                    res = raw_csi_to_amp_phase(msg, self.f_out1)
                    if res:
                        amp_f, ready_amp, phase_unw, ready_ph = res
                        for idx in range(min(len(amp_f), 52)):
                            self.ui_plot_data["p1"]["amp_f"][idx].append(amp_f[idx])
                            self.ui_plot_data["p1"]["ready_amp"][idx].append(ready_amp[idx])
                            self.ui_plot_data["p1"]["phase_unwrapped"][idx].append(phase_unw[idx])
                            self.ui_plot_data["p1"]["ready_phase"][idx].append(ready_ph[idx])
                elif msg.get('type') == 'LOG_DATA':
                    print(f"[P1 Log]: {msg.get('data')}")
            except queue.Empty:
                break

        # Читаем Порт 2
        while True:
            try:
                msg = self.controller.queue_read2.get_nowait()
                if msg.get('type') == 'CSI_DATA':
                    res = raw_csi_to_amp_phase(msg, self.f_out2)
                    if res:
                        amp_f, ready_amp, phase_unw, ready_ph = res
                        for idx in range(min(len(amp_f), 52)):
                            self.ui_plot_data["p2"]["amp_f"][idx].append(amp_f[idx])
                            self.ui_plot_data["p2"]["ready_amp"][idx].append(ready_amp[idx])
                            self.ui_plot_data["p2"]["phase_unwrapped"][idx].append(phase_unw[idx])
                            self.ui_plot_data["p2"]["ready_phase"][idx].append(ready_ph[idx])
                elif msg.get('type') == 'LOG_DATA':
                    print(f"[P2 Log]: {msg.get('data')}")
            except queue.Empty:
                break

        self.update_plots()

    def update_plots(self):
        port = "p1" if self.port_select.currentIndex() == 0 else "p2"
        subcarrier_idx = self.subcarrier_select.value()
        target = self.ui_plot_data[port]

        if subcarrier_idx >= len(target["amp_f"]): return

        y_amp_f = list(target["amp_f"][subcarrier_idx])
        y_ready_amp = list(target["ready_amp"][subcarrier_idx])
        y_phase_unw = list(target["phase_unwrapped"][subcarrier_idx])
        y_ready_ph = list(target["ready_phase"][subcarrier_idx])

        if y_amp_f: self.curve_amp_f.setData(y_amp_f)
        if y_ready_amp: self.curve_ready_amp.setData(y_ready_amp)
        if y_phase_unw: self.curve_phase_unwrapped.setData(y_phase_unw)
        if y_ready_ph: self.curve_ready_phase.setData(y_ready_ph)

    def closeEvent(self, event):
        self.timer.stop()
        self.f_out1.close()
        self.f_out2.close()
        self.controller.p1.terminate()
        self.controller.p2.terminate()
        event.accept()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-p1', '--port1', required=True, help="COM-порт первого девайса")
    parser.add_argument('-p2', '--port2', required=True, help="COM-порт второго девайса")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    visualizer = CsiVisualizer(args.port1, args.port2)
    visualizer.show()
    sys.exit(app.exec())