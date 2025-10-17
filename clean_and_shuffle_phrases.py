#!/usr/bin/env python3
# clean_and_shuffle_phrases.py
# Использование:
#   python3 clean_and_shuffle_phrases.py phrases.txt output.txt

import sys
import random

if len(sys.argv) < 3:
    print("Usage: python3 clean_and_shuffle_phrases.py input.txt output.txt")
    sys.exit(1)

input_file, output_file = sys.argv[1], sys.argv[2]

# --- Читаем файл ---
with open(input_file, "r", encoding="utf-8") as f:
    lines = [line.strip() for line in f if line.strip()]

# --- Формируем пары (португальская, русский) ---
pairs = [(lines[i], lines[i+1]) for i in range(0, len(lines), 2)]

# --- Удаляем дубликаты по португальской фразе ---
unique = {}
for pt, ru in pairs:
    if pt not in unique:
        unique[pt] = ru

pairs = list(unique.items())

# --- Перемешиваем пары ---
random.shuffle(pairs)

# --- Записываем результат ---
with open(output_file, "w", encoding="utf-8") as f:
    for pt, ru in pairs:
        f.write(f"{pt}\n{ru}\n")

print(f"Done. {len(pairs)} unique phrase pairs written to {output_file}")
