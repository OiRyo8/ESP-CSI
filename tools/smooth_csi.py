import csv
import os

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

def process_csi_data(input_filename, output_filename, window_size=5):
    print(f"Чтение данных из {input_filename}...")
    
    # Читаем файл целиком как текст для точной обработки структуры
    with open(input_filename, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Разбиваем на строки и убираем пустые
    lines = [line.strip() for line in content.strip().split('\n') if line.strip()]
    
    # Определяем разделитель (запятая или точка с запятой) по первой строке данных
    delimiter = ';' if ';' in lines[1] else ','
    
    reader = csv.reader(lines, delimiter=delimiter)
    header = next(reader)
    rows = list(reader)

    if not rows:
        print("Ошибка: Файл пуст или содержит только заголовок!")
        return

    # ВАЖНОЕ ИСПРАВЛЕНИЕ: Количество колонок берем из первой строки с ДАННЫМИ,
    # так как оригинальный заголовок содержит только слово 'time_stamp'
    num_cols = len(rows[0])
    num_rows = len(rows)
    smoothed_columns = []

    # 1. Столбец времени (time_stamp) оставляем без изменений
    timestamps = [row[0] for row in rows]
    smoothed_columns.append(timestamps)

    # 2. Обрабатываем каждый столбец с данными (со 2-го до самого конца)
    print(f"Обработка {num_cols - 1} колонок данных по времени (окно={window_size})...")
    for col_idx in range(1, num_cols):
        col_data = []
        for row in rows:
            # Защита на случай, если какая-то строка окажется битой или короче других
            if col_idx < len(row) and row[col_idx].strip():
                col_data.append(float(row[col_idx]))
            else:
                col_data.append(0.0)
        
        # Применяем вашу функцию сглаживания к временному ряду текущей поднесущей
        smoothed_col = moving_average(col_data, window_size)
        smoothed_columns.append(smoothed_col)

    # 3. Собираем столбцы обратно в строки формата CSV
    print("Формирование итоговой матрицы данных...")
    smoothed_rows = []
    for row_idx in range(num_rows):
        new_row = [smoothed_columns[col_idx][row_idx] for col_idx in range(num_cols)]
        smoothed_rows.append(new_row)

    # 4. Автоматически достраиваем заголовок (заполняем пропущенные ampX, phaseX)
    if len(header) < num_cols:
        extended_header = [header[0]]
        for i in range(1, num_cols):
            pair_num = (i - 1) // 2 + 1
            is_phase = (i % 2 == 0)
            name = f"phase{pair_num}" if is_phase else f"amp{pair_num}"
            extended_header.append(name)
        header = extended_header

    # 5. Запись результатов в новый файл
    print(f"Запись результатов в {output_filename}...")
    with open(output_filename, 'w', encoding='utf-8', newline='') as f:
            
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerow(header)
        writer.writerows(smoothed_rows)
        
    print(f"Успешно завершено! Файл сохранен как: {output_filename}")

# Запуск скрипта — обрабатываем два файла: csi_processed1 и csi_processed2
if __name__ == "__main__":
    INPUT_FILES = [
        "log/csi_processed1.csv",
        "log/csi_processed2.csv",
    ]
    WINDOW = 20  # Размер окна сглаживания (настройте под себя)

    for input_path in INPUT_FILES:
        if not os.path.exists(input_path):
            print(f"Файл не найден: {input_path} — пропускаю.")
            continue

        base = os.path.basename(input_path)
        # Заменим 'processed' на 'smoothed' в имени файла, иначе добавим префикс
        if "processed" in base:
            out_base = base.replace("processed", "smoothed")
        else:
            out_base = f"smoothed_{base}"

        output_path = os.path.join("log", out_base)
        process_csi_data(input_path, output_path, WINDOW)