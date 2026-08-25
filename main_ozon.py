import asyncio
import logging
import json
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, error
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
from telegram.error import TelegramError  # Добавьте это
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
import httpx
import traceback
from functools import lru_cache

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Глобальные переменные
REGISTRATION, PHONE = range(2)
STATS_MONTH, STATS_YEAR = range(2)
SET_TIME, SET_TIME_PROCESS = range(2)

# Словарь для преобразования номера месяца в название
month_names = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
}

# Данные для склада Ozon
SCALD_DATA = {
    "Ozon": {
        "admin_chat_ids": [],
        "google_sheet_file": "",
        "sheet_key": "",
        "Утренний опрос": (6, 00),  # Часы и минуты для morning_check
        "Утренний отчет": (7, 00),  # Часы и минуты для morning_report_to_admin
        "Дневной опрос": (9, 30),  # Часы и минуты для day_check
        "Дневной отчет": (12, 5),  # Часы и минуты для day_report_to_admin
        "Утренняя рассылка рабочего места": (7, 5)  # Часы и минуты для morning_spisok_report_to_admin
    }
}

# Индексы столбцов в таблице
COLUMN_NAME = 1  # A
COLUMN_VEST_NUMBER = 2  # B
COLUMN_PHONE = 34  # AH
COLUMN_CHAT_ID = 35  # AI

# Авторизация в Google Sheets
def get_gspread_client(google_sheet_file):
    scope = [""]
    try:
        credentials = ServiceAccountCredentials.from_json_keyfile_name(google_sheet_file, scope)
        return gspread.authorize(credentials)
    except Exception as e:
        logger.error(f"Ошибка авторизации в Google Sheets: {e}")
        raise

# Функция для получения листа по дате с кэшированием
@lru_cache(maxsize=128)
async def get_sheet_by_date_cached(client, sheet_key, date):
    month = month_names[date.month]
    year = date.year
    sheet_name = f"{month} {year}"
    cache_dir = "cache_sheets"
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{sheet_key}_{sheet_name}.json")

    # Кэширование не применяется напрямую к объекту sheet, поэтому просто логика загрузки
    try:
        spreadsheet = client.open_by_key(sheet_key)
        sheet = spreadsheet.worksheet(sheet_name)
        # Сохраняем данные на диск для последующего анализа, если нужно
        data = sheet.get_all_values()
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return sheet
    except gspread.exceptions.WorksheetNotFound:
        logger.warning(f"Лист {sheet_name} не найден в таблице.")
        return None
    except Exception as e:
        logger.error(f"Ошибка при получении листа {sheet_name}: {e}")
        raise

# Функция для получения листа по дате
async def get_sheet_by_date(client, sheet_key, date):
    return await get_sheet_by_date_cached(client, sheet_key, date)

# Кастомный обработчик логов для отправки в Telegram
class TelegramLoggerHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.buffer = []
        self.in_sending = False  # Флаг для предотвращения рекурсии

    def emit(self, record):
        if self.in_sending:  # Пропускаем логи во время отправки, чтобы избежать рекурсии
            return
        if record.levelno >= logging.ERROR:
            log_entry = self.format(record)
            log_type = "Критическая ошибка"
            detailed_log = f"{log_type}: {log_entry}\n{record.exc_info[2].tb_frame.f_code.co_filename}:{record.exc_info[2].tb_lineno}\n{traceback.format_exc()}" if record.exc_info else log_entry
            self.buffer.append(detailed_log)
            asyncio.create_task(self.send_logs_to_admins())

    async def send_logs_to_admins(self):
        if not self.buffer or self.in_sending:  # Пропускаем, если буфер пустой или уже в процессе отправки
            return
        self.in_sending = True
        log_text = "\n".join(self.buffer)
        max_message_length = 4000  # Немного меньше лимита Telegram для безопасности
        parts = [log_text[i:i + max_message_length] for i in range(0, len(log_text), max_message_length)]
        max_retries = 3
        retry_delay = 5
        for attempt in range(max_retries):
            try:
                for sklad_name, sklad_data in SCALD_DATA.items():
                    for admin_id in sklad_data["admin_chat_ids"]:
                        for part_num, part in enumerate(parts, 1):
                            if not part.strip():  # Пропускаем пустые части
                                continue
                            part_text = f"Часть {part_num}/{len(parts)}:\n{part}" if len(parts) > 1 else part
                            try:
                                await application.bot.send_message(chat_id=admin_id, text=part_text)
                            except error.BadRequest as e:
                                if "Chat not found" in str(e):
                                    print(f"Chat not found для админа {admin_id}. Пропускаем.")
                                    continue
                                elif "Message text is empty" in str(e):
                                    print("Пустой текст лога. Пропускаем.")
                                    continue
                                elif "Message is too long" in str(e):
                                    print(f"Текст все еще слишком длинный для админа {admin_id}: {e}. Пропускаем эту часть.")
                                    continue
                                else:
                                    print(f"BadRequest при отправке логов админу {admin_id}: {e}")
                            except (httpx.ConnectError, httpx.TimeoutException) as e:
                                print(f"Попытка {attempt + 1}/{max_retries} не удалась для админа {admin_id}: {e}")
                self.buffer.clear()
                self.in_sending = False
                return
            except Exception as e:
                print(f"Общая ошибка отправки логов: {e}")
                await asyncio.sleep(retry_delay)
        print(f"Не удалось отправить логи после {max_retries} попыток.")
        self.buffer.clear()
        self.in_sending = False
        
# Функция приветствия
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Здравствуйте! Добро пожаловать! Напишите пожалуйста ваше полное ФИО таким образом: Иванов Иван Иванович")
    return REGISTRATION

# Обработка имени пользователя
async def handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text
    # Проверка формата ФИО
    if not name.replace(' ', '').isalpha() or len(name.split()) != 3 or not all(word.istitle() for word in name.split()):
        await update.message.reply_text("Неверный формат ФИО. Убедитесь, что ввели данные в формате: Фамилия Имя Отчество, каждое слово с большой буквы, используя только русский алфавит.")
        return REGISTRATION
    context.user_data['name'] = name
    await update.message.reply_text("Спасибо! Теперь укажите ваш номер телефона.")
    return PHONE

# Обработка телефона пользователя и сохранение данных
async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text
    name = context.user_data.get('name')
    chat_id = update.message.chat_id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        await update.message.reply_text("Произошла ошибка при регистрации. Пожалуйста, попробуйте позже.")
        return ConversationHandler.END

    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]

    try:
        # Авторизация в Google Sheets
        client = get_gspread_client(google_sheet_file)
        sheet = await get_sheet_by_date(client, sheet_key, datetime.now())

        if not sheet:
            await update.message.reply_text(f"Ошибка. Сообщите пожалуйста вашему бригадиру об ошибке.")
            return ConversationHandler.END

        # Получаем все существующие chat_id и телефоны из соответствующих столбцов
        existing_chat_ids = sheet.col_values(COLUMN_CHAT_ID)  # Столбец AI
        existing_phones = sheet.col_values(COLUMN_PHONE)      # Столбец AH

        # Проверяем, зарегистрирован ли пользователь с таким chat_id или телефоном
        if str(chat_id) in existing_chat_ids:
            await update.message.reply_text("Вы уже зарегистрированы. Повторная регистрация невозможна.")
            return ConversationHandler.END

        if phone in existing_phones:
            await update.message.reply_text("Пользователь с таким номером телефона уже зарегистрирован.")
            return ConversationHandler.END

        # Определяем первую пустую строку
        all_values = sheet.get_all_values()
        next_row = len(all_values) + 1  # Номер следующей пустой строки

        # Создаем новую строку с данными пользователя
        new_row = [''] * 35  # Создаем пустую строку с 35 колонками
        new_row[COLUMN_NAME - 1] = name       # ФИО в столбец A
        new_row[COLUMN_PHONE - 1] = phone     # Номер телефона в столбец AH
        new_row[COLUMN_CHAT_ID - 1] = str(chat_id)  # CHAT ID в столбец AI

        # Вставляем новую строку в таблицу
        sheet.insert_row(new_row, index=next_row)
        logger.info(f"Данные для {name} успешно добавлены в таблицу {sklad_name}. Данные: {new_row}")

    except Exception as e:
        logger.error(f"Ошибка при добавлении данных для {name} в таблицу {sklad_name}: {e}")
        for admin_id in sklad_data["admin_chat_ids"]:
            try:
                await application.bot.send_message(chat_id=admin_id, text=f"Ошибка при добавлении данных для {name} в таблице {sklad_name}: {e}")
            except error.BadRequest as bad_e:
                if "Chat not found" in str(bad_e):
                    logger.warning(f"Chat not found для админа {admin_id}.")
                else:
                    logger.error(f"BadRequest админу {admin_id}: {bad_e}")
            except Exception as inner_e:
                logger.error(f"Ошибка отправки админу {admin_id}: {inner_e}")
        await update.message.reply_text("Произошла ошибка при регистрации. Пожалуйста, попробуйте позже.")
        return ConversationHandler.END

    # Успешное завершение регистрации
    await update.message.reply_text(
        "Спасибо за регистрацию! Сюда будет отправляться информация о вашем месте работы на складе. "
        "Вас мы хотим попросить отвечать на будущие сообщения данного бота, чтобы мы видели, что вы ознакомились с его содержимым. "
        "Удачной работы! :)"
    )
    return ConversationHandler.END
    
# Обработка любых сообщений после регистрации
async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Извините, я не понимаю, что вы хотите сказать. В случае возникновения вопросов, пожалуйста, обратитесь к вашему бригадиру.")

# Функция для обработки откликов через кнопку
async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        logger.error("Callback query отсутствует.")
        return
    chat_id = query.message.chat_id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        await query.answer("Произошла ошибка. Пожалуйста, попробуйте позже.", show_alert=True)
        return
    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    sheet = await get_sheet_by_date(client, sheet_key, datetime.now())
    if not sheet:
        logger.warning(f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        await query.answer("Произошла ошибка. Пожалуйста, попробуйте позже.", show_alert=True)
        return
    today = datetime.now().strftime("%d.%m.%Y")
    header_row = sheet.row_values(1)
    if today not in header_row:
        logger.warning(f"Столбец с датой {today} не найден в таблице для склада {sklad_name}.")
        await query.answer("Произошла ошибка. Пожалуйста, попробуйте позже.", show_alert=True)
        return
    col_index = header_row.index(today) + 1
    rows = sheet.get_all_values()

    for row_index, row in enumerate(rows[1:], start=2):
        chat_id_str = row[COLUMN_CHAT_ID - 1]
        if not chat_id_str or not chat_id_str.isdigit():
            logger.warning(f"Пропущена строка с некорректным или отсутствующим chat_id: {row}")
            continue
        chat_id_int = int(chat_id_str)
        name = row[COLUMN_NAME - 1]
        symbols = row[col_index - 1].strip()  # Символы на сегодня

        if chat_id_int == chat_id:
            # Проверяем время утреннего отчета (аналогично дневному)
            current_datetime = datetime.now()
            morning_report_time = datetime.strptime(
                f"{sklad_data['Утренний отчет'][0]:02}:{sklad_data['Утренний отчет'][1]:02}",
                "%H:%M"
            ).time()
            morning_report_datetime = datetime.combine(
                current_datetime.date(),
                morning_report_time
            )
    
            if sklad_name not in response_tracking:
                response_tracking[sklad_name] = {}
            if chat_id not in response_tracking[sklad_name]:
                response_tracking[sklad_name][chat_id] = {"name": name, "responded": True}
                await query.answer("Спасибо за ваш ответ!")
        
                # Отправляем уведомление администраторам только после утреннего отчета
                if current_datetime >= morning_report_datetime:
                    await notify_admins(sklad_data, f"Пользователь {name} ответил на утренний опрос о том, собирается ли он на работу сегодня: Да")
        
                if symbols and symbols != "?":
                    await application.bot.send_message(chat_id=chat_id, text=f"Спасибо за ваш ответ! Сегодня необходимо работать на секторе: {symbols}")
                else:
                    await application.bot.send_message(chat_id=chat_id, text="Спасибо за ваш ответ!")
            else:
                await query.answer("Вы уже ответили ранее.", show_alert=True)
                await application.bot.send_message(chat_id=chat_id, text="Вы уже ответили ранее.")
            break
    else:
        logger.warning(f"Пользователь с chat_id {chat_id} не найден в таблице для склада {sklad_name}.")
        await query.answer("Произошла ошибка. Пожалуйста, попробуйте позже.", show_alert=True)
        await application.bot.send_message(chat_id=chat_id, text="Произошла ошибка. Пожалуйста, попробуйте позже.")
        return

# Обработка утренних ответов пользователей
async def handle_morning_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        logger.error("Callback query отсутствует.")
        return
    await query.answer()
    response = query.data  # "morning_yes" или "morning_no"
    chat_id = query.message.chat_id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        await application.bot.send_message(chat_id=chat_id, text="Произошла ошибка. Пожалуйста, попробуйте позже.")
        return

    # Проверяем, не начался ли новый цикл дневного опроса (8:00 следующего дня)
    current_datetime = datetime.now()
    day_check_time = datetime.strptime(
        f"{sklad_data['Дневной опрос'][0]:02}:{sklad_data['Дневной опрос'][1]:02}",
        "%H:%M"
    ).time()
    next_day_check_datetime = datetime.combine(
        current_datetime.date() + timedelta(days=1),
        day_check_time
    )
    if current_datetime >= next_day_check_datetime:
        await application.bot.send_message(chat_id=chat_id, text="Время для ответа на дневной опрос истекло, новый опрос начался.")
        return

    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    tomorrow = datetime.now() + timedelta(days=1)
    sheet = await get_sheet_by_date(client, sheet_key, tomorrow)
    if not sheet:
        logger.warning(f"Лист для завтрашней даты не найден в таблице для склада {sklad_name}.")
        await application.bot.send_message(chat_id=chat_id, text="Произошла ошибка. Пожалуйста, попробуйте позже.")
        return
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    header_row = sheet.row_values(1)
    if tomorrow_date not in header_row:
        logger.warning(f"Столбец с датой {tomorrow_date} не найден в таблице для склада {sklad_name}.")
        await application.bot.send_message(chat_id=chat_id, text="Произошла ошибка. Пожалуйста, попробуйте позже.")
        return
    col_index = header_row.index(tomorrow_date) + 1
    rows = sheet.get_all_values()

    # Проверяем время дневного отчета (14:00)
    day_report_time = datetime.strptime(
        f"{sklad_data['Дневной отчет'][0]:02}:{sklad_data['Дневной отчет'][1]:02}",
        "%H:%M"
    ).time()
    day_report_datetime = datetime.combine(
        current_datetime.date(),
        day_report_time
    )

    for row_index, row in enumerate(rows[1:], start=2):
        chat_id_str = row[COLUMN_CHAT_ID - 1]
        if not chat_id_str or not chat_id_str.isdigit():
            logger.warning(f"Пропущена строка с некорректным или отсутствующим chat_id: {row}")
            continue
        chat_id_int = int(chat_id_str)
        name = row[COLUMN_NAME - 1]
        if chat_id_int == chat_id:
            if sklad_name not in morning_response_tracking:
                morning_response_tracking[sklad_name] = {}
            response_text = "Да" if response == "morning_yes" else "Нет"
            # Сохраняем ответ пользователя
            morning_response_tracking[sklad_name][chat_id] = {"name": name, "response": response}
            # Отправляем уведомление администраторам только после дневного отчета
            if current_datetime >= day_report_datetime:
                await notify_admins(sklad_data, f"Пользователь {name} ответил на дневной опрос о том, выйдет ли он(а) завтра на работу: {response_text}")
            await application.bot.send_message(chat_id=chat_id, text=f"Вы ответили: {response_text}. Ваш ответ сохранен!")
            break
    else:
        logger.warning(f"Пользователь с chat_id {chat_id} не найден в таблице для склада {sklad_name}.")
        await application.bot.send_message(chat_id=chat_id, text="Произошла ошибка. Пожалуйста, попробуйте позже.")

# Проверка таблицы каждый день утром и отправка напоминания
async def morning_check(sklad_name: str):
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        return
    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    sheet = await get_sheet_by_date(client, sheet_key, datetime.now())
    if not sheet:
        logger.warning(f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        return
    today = datetime.now().strftime("%d.%m.%Y")
    header_row = sheet.row_values(1)
    if today not in header_row:
        logger.warning(f"Столбец с датой {today} не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Столбец с датой {today} не найден в таблице для склада {sklad_name}.")
        return
    col_index = header_row.index(today) + 1
    rows = sheet.get_all_values()

    # Очистка данных о предыдущих ответах
    if sklad_name in response_tracking:
        response_tracking[sklad_name].clear()

    for row_index, row in enumerate(rows[1:], start=2):
        cell_value = row[col_index - 1].strip()  # Удаляем лишние пробелы
        chat_id_str = row[COLUMN_CHAT_ID - 1]  # Получаем значение chat_id как строку
        if not chat_id_str or not chat_id_str.isdigit():
            logger.warning(f"Пропущена строка с некорректным или отсутствующим chat_id: {row}")
            continue
        chat_id = int(chat_id_str)
        name = row[COLUMN_NAME - 1]
        vest_number = row[COLUMN_VEST_NUMBER - 1]  # Получаем номер жилетки
        symbols = row[col_index - 1].strip()
        if cell_value and cell_value != "?":  # Исключаем ячейки со знаком ?
            message_text = (
                f"Доброе утро,\n{name}!\n"
                "На работу собираетесь?\n"
                "Всё в порядке?"
            )
            keyboard = [[InlineKeyboardButton("Да!", callback_data="response_yes")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await application.bot.send_message(chat_id=chat_id, text=message_text, reply_markup=reply_markup)
            except error.BadRequest as e:
                if "Chat not found" in str(e):
                    print(f"Chat not found для пользователя {name} (chat_id {chat_id}). Пропускаем.")  # Замените logger.warning на print
                    continue
                else:
                    raise
            except httpx.ConnectError as e:
                print(f"Ошибка сети при отправке {name}: {e}")  # Замените logger.error на print
                await notify_admins(sklad_data, f"Ошибка отправки сообщения пользователю {name} на складе {sklad_name}: {e}")
            except Exception as e:
                print(f"Ошибка отправки сообщения пользователю {name} на складе {sklad_name}: {e}")  # Замените logger.error на print
                await notify_admins(sklad_data, f"Ошибка отправки сообщения пользователю {name} на складе {sklad_name}: {e}")
                continue

# Утреннее напоминание и отчет
async def day_check(sklad_name: str):
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        return
    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    tomorrow = datetime.now() + timedelta(days=1)
    sheet = await get_sheet_by_date(client, sheet_key, tomorrow)
    if not sheet:
        logger.warning(f"Лист для завтрашней даты не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Лист для завтрашней даты не найден в таблице для склада {sklad_name}.")
        return
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    header_row = sheet.row_values(1)
    if tomorrow_date not in header_row:
        logger.warning(f"Столбец с датой {tomorrow_date} не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Столбец с датой {tomorrow_date} не найден в таблице для склада {sklad_name}.")
        return
    col_index = header_row.index(tomorrow_date) + 1
    rows = sheet.get_all_values()

    # Очистка данных о предыдущих ответах в начале нового опроса
    if sklad_name in morning_response_tracking:
        morning_response_tracking[sklad_name].clear()

    for row_index, row in enumerate(rows[1:], start=2):
        cell_value = row[col_index - 1].strip()
        chat_id_str = row[COLUMN_CHAT_ID - 1]
        if not chat_id_str or not chat_id_str.isdigit():
            logger.warning(f"Пропущена строка с некорректным или отсутствующим chat_id: {row}")
            continue
        chat_id = int(chat_id_str)
        name = row[COLUMN_NAME - 1]
        if cell_value and cell_value != "?":
            keyboard = [
                [InlineKeyboardButton("Да", callback_data="morning_yes"),
                 InlineKeyboardButton("Нет", callback_data="morning_no")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            message_text = (
                f"Добрый день,\n{name}.\n"
                "Напоминаю, что завтра на работу.\n"
                "Всё в порядке?\n"
                "Выходите?\n"
                "Прошу ответить до 12:00"
            )
            try:
                await application.bot.send_message(chat_id=chat_id, text=message_text, reply_markup=reply_markup)
            except error.BadRequest as e:
                if "Chat not found" in str(e):
                    logger.warning(f"Chat not found для пользователя {name} (chat_id {chat_id}). Пропускаем.")
                    continue
                else:
                    raise
            except httpx.ConnectError as e:
                logger.error(f"Ошибка отправки дневного напоминания {name} на складе {sklad_name}: {e}")
                await notify_admins(sklad_data, f"Ошибка отправки дневного напоминания {name} на складе {sklad_name}: {e}")
                continue  # Добавьте
            except Exception as e:
                logger.error(f"Ошибка отправки дневного напоминания {name} на складе {sklad_name}: {e}")
                await notify_admins(sklad_data, f"Ошибка отправки дневного напоминания {name} на складе {sklad_name}: {e}")
                continue  # Добавьте

# Функция для генерации утреннего отчета для администратора
async def morning_report_to_admin(sklad_name: str):
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        return
    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    sheet = await get_sheet_by_date(client, sheet_key, datetime.now())
    if not sheet:
        logger.warning(f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        return
    today = datetime.now().strftime("%d.%m.%Y")
    header_row = sheet.row_values(1)
    if today not in header_row:
        logger.warning(f"Столбец с датой {today} не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Столбец с датой {today} не найден в таблице для склада {sklad_name}.")
        return
    col_index = header_row.index(today) + 1
    rows = sheet.get_all_values()

    unresponded = []
    responded = []
    unregistered = []

    for row_index, row in enumerate(rows[1:], start=2):
        chat_id_str = row[COLUMN_CHAT_ID - 1]
        name = row[COLUMN_NAME - 1]
        cell_value = row[col_index - 1].strip()
    
        if not name:
            continue  # Пропускаем строки без имени
    
        if not cell_value or cell_value == "?":
            continue  # Пропускаем строки без символов или со знаком ?
    
        # Теперь проверяем unregistered только для тех, кто должен участвовать (есть символы)
        if not chat_id_str or not chat_id_str.isdigit():
            unregistered.append(name)
            continue
    
        chat_id = int(chat_id_str)
        if sklad_name not in response_tracking:
            response_tracking[sklad_name] = {}
        if chat_id not in response_tracking[sklad_name]:
            response_tracking[sklad_name][chat_id] = {"name": name, "responded": False}
        if response_tracking[sklad_name][chat_id]["responded"]:
            responded.append(name)
        else:
            unresponded.append(name)

    if not responded and not unresponded:
        logger.info(f"Нет сотрудников с символами на сегодняшний день для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Нет сотрудников с символами на сегодняшний день для склада {sklad_name}.")
        return

    report = f"Отчет об откликах для склада {sklad_name}:\n"
    report += "\nОтветили:\n" + "\n".join(responded) if responded else "\nОтветивших нет."
    report += "\n\nНе ответили:\n" + "\n".join(unresponded) if unresponded else "\nВсе ответили."
    report += "\n\nНезарегистрированные пользователи:\n" + "\n".join(unregistered) if unregistered else "\nНезарегистрированные пользователи: Нет"

    await notify_admins(sklad_data, report)

    if sklad_name in response_tracking:
        response_tracking[sklad_name].clear()

async def day_report_to_admin(sklad_name: str):
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        return
    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    tomorrow = datetime.now() + timedelta(days=1)
    sheet = await get_sheet_by_date(client, sheet_key, tomorrow)
    if not sheet:
        logger.warning(f"Лист для завтрашней даты не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Лист для завтрашней даты не найден в таблице для склада {sklad_name}.")
        return
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    header_row = sheet.row_values(1)
    if tomorrow_date not in header_row:
        logger.warning(f"Столбец с датой {tomorrow_date} не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Столбец с датой {tomorrow_date} не найден в таблице для склада {sklad_name}.")
        return
    col_index = header_row.index(tomorrow_date) + 1
    rows = sheet.get_all_values()

    yes_responses = []
    no_responses = []
    no_response = []
    unregistered = []

    for row_index, row in enumerate(rows[1:], start=2):
        chat_id_str = row[COLUMN_CHAT_ID - 1]
        name = row[COLUMN_NAME - 1]
        cell_value = row[col_index - 1].strip()
    
        if not name:
            continue  # Пропускаем строки без имени
    
        if not cell_value or cell_value == "?":
            continue  # Пропускаем строки без символов или со знаком ?
    
        # Теперь проверяем unregistered только для тех, кто должен участвовать (есть символы)
        if not chat_id_str or not chat_id_str.isdigit():
            unregistered.append(name)
            continue
    
        chat_id = int(chat_id_str)
        if sklad_name not in morning_response_tracking:
            morning_response_tracking[sklad_name] = {}
        if chat_id not in morning_response_tracking[sklad_name]:
            morning_response_tracking[sklad_name][chat_id] = {"name": name, "response": None}
        response = morning_response_tracking[sklad_name][chat_id]["response"]
        if response == "morning_yes":
            yes_responses.append(name)
        elif response == "morning_no":
            no_responses.append(name)
        elif response is None:
            no_response.append(name)

    if not yes_responses and not no_responses and not no_response:
        logger.info(f"Нет сотрудников с символами на завтрашний день для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Нет сотрудников с символами на завтрашний день для склада {sklad_name}.")
        return

    report = f"Дневной отчет для склада {sklad_name} на {tomorrow_date}:\n"
    report += "\nОтветили Да:\n" + "\n".join(yes_responses) if yes_responses else "\nОтветивших Да нет."
    report += "\n\nОтветили Нет:\n" + "\n".join(no_responses) if no_responses else "\nОтветивших Нет нет."
    report += "\n\nНе ответили:\n" + "\n".join(no_response) if no_response else "\nВсе ответили."
    report += "\n\nНезарегистрированные пользователи:\n" + "\n".join(unregistered) if unregistered else "\nНезарегистрированные пользователи: Нет"

    await notify_admins(sklad_data, report)

async def morning_spisok_report_to_admin(sklad_name: str):
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        return
    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    sheet = await get_sheet_by_date(client, sheet_key, datetime.now())
    if not sheet:
        logger.warning(f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        return
    today = datetime.now().strftime("%d.%m.%Y")
    header_row = sheet.row_values(1)
    if today not in header_row:
        logger.warning(f"Столбец с датой {today} не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Столбец с датой {today} не найден в таблице для склада {sklad_name}.")
        return
    col_index = header_row.index(today) + 1
    rows = sheet.get_all_values()

    # Формируем данные для отчетов
    symbols_dict = {}
    symbol_counts = {}
    for row_index, row in enumerate(rows[1:], start=2):
        cell_value = row[col_index - 1].strip()
        if cell_value and cell_value != "?":
            name = row[COLUMN_NAME - 1]
            vest_number = row[COLUMN_VEST_NUMBER - 1]
            if cell_value not in symbols_dict:
                symbols_dict[cell_value] = []
            symbols_dict[cell_value].append(f"{name} {vest_number}")
            symbol_counts[cell_value] = symbol_counts.get(cell_value, 0) + 1

    if not symbols_dict:
        logger.info(f"Нет сотрудников с символами на сегодняшний день для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Нет сотрудников с символами на сегодняшний день для склада {sklad_name}.")
        return

    # Отправка списка сотрудникам
    await send_workplace_list_to_employees(sklad_name, symbols_dict, today)

    # Отправка списка Юпитер администраторам
    await send_jupiter_list_to_admins(sklad_name, symbol_counts, today)

# Команда /day_check_participants
async def day_check_participants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return

    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    tomorrow = datetime.now() + timedelta(days=1)
    sheet = await get_sheet_by_date(client, sheet_key, tomorrow)

    if not sheet:
        logger.warning(f"Лист для завтрашней даты не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Лист для завтрашней даты не найден в таблице для склада {sklad_name}.")
        return

    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    header_row = sheet.row_values(1)

    if tomorrow_date not in header_row:
        logger.warning(f"Столбец с датой {tomorrow_date} не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Столбец с датой {tomorrow_date} не найден в таблице для склада {sklad_name}.")
        return

    col_index = header_row.index(tomorrow_date) + 1
    rows = sheet.get_all_values()

    participants = []

    for row_index, row in enumerate(rows[1:], start=2):
        cell_value = row[col_index - 1].strip()  # Удаляем лишние пробелы
        chat_id_str = row[COLUMN_CHAT_ID - 1]  # Получаем значение chat_id как строку

        # Проверяем, что chat_id существует и не является пустой строкой
        if not chat_id_str or not chat_id_str.isdigit():
            logger.warning(f"Пропущена строка с некорректным или отсутствующим chat_id: {row}")
            continue

        chat_id = int(chat_id_str)
        name = row[COLUMN_NAME - 1]

        if cell_value and cell_value != "?":  # Исключаем ячейки со знаком ?
            participants.append(name)

    if not participants:
        logger.info(f"Нет сотрудников с символами на завтрашний день для склада {sklad_name}.")
        await update.message.reply_text(f"Нет сотрудников с символами на завтрашний день для склада {sklad_name}.")
        return

    report = f"Сотрудники, участвующие в рассылке Дневной опрос для склада {sklad_name}:\n"
    report += "\n".join(participants)

    await update.message.reply_text(report)

# Функция для получения общей статистики
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return

    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)

    # Сегодня
    sheet_today = await get_sheet_by_date(client, sheet_key, datetime.now())
    today_count = 0
    if sheet_today:
        today = datetime.now().strftime("%d.%m.%Y")
        header_row = sheet_today.row_values(1)
        if today in header_row:
            col_index = header_row.index(today) + 1
            for row in sheet_today.get_all_values()[1:]:
                if row[COLUMN_NAME - 1] and row[col_index - 1].strip() and row[col_index - 1].strip() != "?":
                    today_count += 1

    # Завтра
    sheet_tomorrow = await get_sheet_by_date(client, sheet_key, datetime.now() + timedelta(days=1))
    tomorrow_count = 0
    if sheet_tomorrow:
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
        header_row = sheet_tomorrow.row_values(1)
        if tomorrow in header_row:
            col_index = header_row.index(tomorrow) + 1
            for row in sheet_tomorrow.get_all_values()[1:]:
                if row[COLUMN_NAME - 1] and row[col_index - 1].strip() and row[col_index - 1].strip() != "?":
                    tomorrow_count += 1

    total_users = len([row for row in sheet_today.get_all_values()[1:] if row[COLUMN_NAME - 1]])

    message = (
        f"📊 Статистика:\n"
        f"• Всего сотрудников: {total_users}\n"
        f"• Сотрудников на сегодня: {today_count}\n"
        f"• Сотрудников на завтра: {tomorrow_count}\n"
    )
    await update.message.reply_text(message)

# Команда /stats_month
async def stats_month_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(month_names[i], callback_data=f"month_{i}")] for i in range(1, 13)
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Выберите месяц:", reply_markup=reply_markup)
    return STATS_MONTH

# Обработка выбора месяца
async def stats_month_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        logger.error("Callback query отсутствует.")
        return ConversationHandler.END

    await query.answer()
    month = int(query.data.split("_")[1])
    context.user_data['month'] = month
    await query.edit_message_text(text="Введите год (например, 2025):")
    return STATS_YEAR

async def stats_year_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    year = update.message.text
    if not year.isdigit() or len(year) != 4:
        await update.message.reply_text("Неверный формат года. Введите четырёхзначное число.")
        return STATS_YEAR

    year = int(year)
    context.user_data['year'] = year
    month = context.user_data.get('month')
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    sheet_name = f"{month_names[month]} {year}"

    try:
        spreadsheet = client.open_by_key(sheet_key)
        sheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        await update.message.reply_text(f"Лист {sheet_name} не найден.")
        return ConversationHandler.END

    # Получаем первую строку (заголовки) только с C (3) до AG (33)
    header_row = sheet.row_values(1)[2:33]  # Индексы с 2 (C) до 32 (AG включительно)
    today = datetime.now()
    date_columns = []

    # Фильтруем только валидные даты в диапазоне C:AG
    for i, value in enumerate(header_row, start=3):  # Начинаем с 3, так как C — третий столбец
        value = value.strip()
        if not value or value.count('.') != 2:  # Пропускаем пустые или не даты
            continue
        try:
            date = datetime.strptime(value, "%d.%m.%Y")
            if date < today:  # Учитываем только даты до сегодня
                date_columns.append((i, date))
        except ValueError:
            logger.warning(f"Некорректный формат даты в заголовке: {value}")
            continue

    if not date_columns:
        await update.message.reply_text(f"Нет данных до сегодняшнего дня в листе {sheet_name} в столбцах C:AG.")
        return ConversationHandler.END

    # Получаем все данные таблицы
    all_rows = sheet.get_all_values()
    total_rows = len(all_rows)
    batch_size = 100
    stats = {}

    # Порционная обработка
    for start_row in range(1, total_rows, batch_size):  # Начинаем с 1, так как 0 — заголовок
        end_row = min(start_row + batch_size, total_rows)
        rows = all_rows[start_row:end_row]

        for row in rows:
            if len(row) < COLUMN_CHAT_ID:  # COLUMN_CHAT_ID = 35
                continue
            chat_id_str = row[COLUMN_CHAT_ID - 1]
            if not chat_id_str or not chat_id_str.isdigit():
                continue
            name = row[COLUMN_NAME - 1]
            if not name:
                continue
            if name not in stats:
                stats[name] = 0

            for col_index, date in date_columns:
                if col_index - 1 < len(row):
                    cell_value = row[col_index - 1].strip()
                    if cell_value and cell_value != "?":
                        stats[name] += 1

        await asyncio.sleep(0.1)  # Небольшая задержка

    # Формирование отчета
    report = f"Статистика за {month_names[month]} {year}:\n"
    sorted_names = sorted(stats.keys())
    chunk_size = 50
    for i in range(0, len(sorted_names), chunk_size):
        chunk = sorted_names[i:i + chunk_size]
        chunk_report = report + "\n".join(f"{name}: {stats[name]} дней" for name in chunk)
        await update.message.reply_text(chunk_report)
        await asyncio.sleep(0.5)

    return ConversationHandler.END

# Команда /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return

    message = (
        "📚 Доступные команды:\n"
        "/start - Начать регистрацию\n"
        "/stats - Получить общую статистику\n"
        "/stats_month - Получить статистику за любой месяц по количеству отработанных дней сотрудников\n"
        "/monthly_users - Получить количество зарегистрированных пользователей за текущий месяц\n"
        "/status - Состояние бота\n"
        "/set_time - Установить время для процессов\n"
        "/day_check_participants - Получить список участников, кому придет сообщение с опросом о завтрашней работе\n"
        "/add_admin - Добавить нового администратора для данного бота\n"
        "/workers - Получить список сотрудников с выбором сортировки и даты\n"
        "/cancel - Отмена любой из команды (Если бот завис)\n"
        "/help - Показать это сообщение\n"
    )
    await update.message.reply_text(message)

# Команда /add_admin
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return

    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Неверный формат команды. Используйте '/add_admin <chat_id>'.")
        return

    new_admin_id = int(context.args[0])
    if new_admin_id in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("Этот пользователь уже является администратором.")
        return

    sklad_data["admin_chat_ids"].append(new_admin_id)
    await update.message.reply_text(f"Пользователь с chat_id {new_admin_id} добавлен в список администраторов.")
    logger.info(f"Пользователь с chat_id {new_admin_id} добавлен в список администраторов для склада {sklad_name}.")

# Новые состояния для /workers
WORKERS_SORT, WORKERS_DATE = range(2, 4)

# Новая функция для команды /workers
async def workers_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("По алфавиту", callback_data="sort_alpha")],
        [InlineKeyboardButton("По секторам", callback_data="sort_sectors")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите способ сортировки:", reply_markup=reply_markup)
    return WORKERS_SORT

# Обработка выбора сортировки
async def workers_sort_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['sort'] = query.data  # "sort_alpha" или "sort_sectors"
    keyboard = [
        [InlineKeyboardButton("Сегодня", callback_data="date_today")],
        [InlineKeyboardButton("Завтра", callback_data="date_tomorrow")],
        [InlineKeyboardButton("Ввести дату", callback_data="date_custom")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите дату:", reply_markup=reply_markup)
    return WORKERS_DATE

# Обработка выбора даты
async def workers_date_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sort_type = context.user_data.get('sort')
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if query.data == "date_custom":
        await query.edit_message_text("Введите дату в формате ДД.ММ.ГГГГ:")
        return WORKERS_DATE

    date = datetime.now() if query.data == "date_today" else datetime.now() + timedelta(days=1)
    date_str = date.strftime("%d.%m.%Y")

    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    sheet = await get_sheet_by_date(client, sheet_key, date)
    if not sheet:
        await query.edit_message_text(f"Лист для даты {date_str} не найден.")
        return ConversationHandler.END

    header_row = sheet.row_values(1)
    if date_str not in header_row:
        await query.edit_message_text(f"Столбец с датой {date_str} не найден.")
        return ConversationHandler.END

    col_index = header_row.index(date_str) + 1
    rows = sheet.get_all_values()
    report_data = []

    for row in rows[1:]:
        cell_value = row[col_index - 1].strip()
        if cell_value and cell_value != "?":
            name = row[COLUMN_NAME - 1]
            vest_number = row[COLUMN_VEST_NUMBER - 1]
            report_data.append((name, vest_number, cell_value))

    if not report_data:
        await query.edit_message_text(f"Нет сотрудников с символами на дату {date_str}.")
        return ConversationHandler.END

    if sort_type == "sort_alpha":
        report_data.sort(key=lambda x: x[0])  # Сортировка по имени
        report = f"Дата: {date_str}\n" + "\n".join(f"{name} {vest_number}: {symbols}" for name, vest_number, symbols in report_data)
    else:  # sort_sectors
        symbols_dict = {}
        for name, vest_number, symbols in report_data:
            if symbols not in symbols_dict:
                symbols_dict[symbols] = []
            symbols_dict[symbols].append(f"{name} {vest_number}")
        report = f"Дата: {date_str}\n"
        for symbol, workers in symbols_dict.items():
            report += f"{len(workers)} человека на {symbol.upper()}:\n" + "\n".join(workers) + "\n\n"

    await query.edit_message_text(report)
    return ConversationHandler.END

# Обработка ввода пользовательской даты
async def workers_custom_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    date_str = update.message.text
    try:
        date = datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text("Неверный формат даты. Используйте ДД.ММ.ГГГГ.")
        return WORKERS_DATE

    sort_type = context.user_data.get('sort')
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    sheet = await get_sheet_by_date(client, sheet_key, date)
    if not sheet:
        await update.message.reply_text(f"Лист для даты {date_str} не найден.")
        return ConversationHandler.END

    header_row = sheet.row_values(1)
    if date_str not in header_row:
        await update.message.reply_text(f"Столбец с датой {date_str} не найден.")
        return ConversationHandler.END

    col_index = header_row.index(date_str) + 1
    rows = sheet.get_all_values()
    report_data = []

    for row in rows[1:]:
        cell_value = row[col_index - 1].strip()
        if cell_value and cell_value != "?":
            name = row[COLUMN_NAME - 1]
            vest_number = row[COLUMN_VEST_NUMBER - 1]
            report_data.append((name, vest_number, cell_value))

    if not report_data:
        await update.message.reply_text(f"Нет сотрудников с символами на дату {date_str}.")
        return ConversationHandler.END

    if sort_type == "sort_alpha":
        report_data.sort(key=lambda x: x[0])
        report = f"Дата: {date_str}\n" + "\n".join(f"{name} {vest_number}: {symbols}" for name, vest_number, symbols in report_data)
    else:  # sort_sectors
        symbols_dict = {}
        for name, vest_number, symbols in report_data:
            if symbols not in symbols_dict:
                symbols_dict[symbols] = []
            symbols_dict[symbols].append(f"{name} {vest_number}")
        report = f"Дата: {date_str}\n"
        for symbol, workers in symbols_dict.items():
            report += f"{len(workers)} человека на {symbol.upper()}:\n" + "\n".join(workers) + "\n\n"

    await update.message.reply_text(report)
    return ConversationHandler.END

# Новая функция отмены для /workers
async def cancel_workers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отмена команды /workers.")
    return ConversationHandler.END

# Обновленный ConversationHandler для /workers
conv_handler_workers = ConversationHandler(
    entry_points=[CommandHandler("workers", workers_start)],
    states={
        WORKERS_SORT: [CallbackQueryHandler(workers_sort_chosen, pattern=r"^sort_(alpha|sectors)$")],
        WORKERS_DATE: [
            CallbackQueryHandler(workers_date_chosen, pattern=r"^date_(today|tomorrow|custom)$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, workers_custom_date)
        ]
    },
    fallbacks=[CommandHandler("cancel", cancel_workers)],  # Используем новую функцию cancel_workers
    per_message=False
)

# Команда /set_time
async def set_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return ConversationHandler.END

    processes = [
        ("Утренний опрос", sklad_data["Утренний опрос"]),
        ("Утренний отчет", sklad_data["Утренний отчет"]),
        ("Утренняя рассылка рабочего места", sklad_data["Утренняя рассылка рабочего места"]),
        ("Дневной опрос", sklad_data["Дневной опрос"]),
        ("Дневной отчет", sklad_data["Дневной отчет"]),
        ("Вернуться назад", None)
    ]

    process_list = "\n".join(
        f"{i + 1}. {process[0]} (текущее {process[1][0]:02}:{process[1][1]:02})"
        if process[1] is not None else f"{i + 1}. {process[0]}"
        for i, process in enumerate(processes)
    )
    await update.message.reply_text(f"Выберите процесс, для которого хотите установить время:\n{process_list}\nВведите номер процесса:")
    return SET_TIME_PROCESS

async def check_question_marks_for_tomorrow(sklad_name: str):
    current_time = datetime.now().time()
    start_time = datetime.strptime("10:00", "%H:%M").time()
    end_time = datetime.strptime("23:00", "%H:%M").time()

    # Проверяем, находится ли текущее время в заданном диапазоне
    if not (start_time <= current_time <= end_time):
        return  # Если время вне диапазона, ничего не делаем

    global sent_messages_today
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        return
    
    # Определяем текущую дату
    today = datetime.now().date()
    
    # Если дата изменилась, сбрасываем список отправленных сообщений
    if "last_reset_date" not in sent_messages_today or sent_messages_today["last_reset_date"] != today:
        sent_messages_today = {"last_reset_date": today}
    
    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)

    tomorrow = datetime.now() + timedelta(days=1)
    sheet = await get_sheet_by_date(client, sheet_key, tomorrow)
    if not sheet:
        logger.warning(f"Лист для завтрашней даты не найден в таблице для склада {sklad_name}.")
        return
    
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    header_row = sheet.row_values(1)
    if tomorrow_date not in header_row:
        logger.warning(f"Столбец с датой {tomorrow_date} не найден в таблице для склада {sklad_name}.")
        return
    
    col_index = header_row.index(tomorrow_date) + 1
    rows = sheet.get_all_values()
    
    # Список для сбора пользователей, которым отправлен опрос
    users_notified = []
    
    for row_index, row in enumerate(rows[1:], start=2):
        chat_id_str = row[COLUMN_CHAT_ID - 1]
        if not chat_id_str or not chat_id_str.isdigit():
            logger.warning(f"Пропущена строка с некорректным или отсутствующим chat_id: {row}")
            continue
        chat_id = int(chat_id_str)
        name = row[COLUMN_NAME - 1]
        cell_value = row[col_index - 1].strip()
        
        # Проверяем, отправлялось ли уже сообщение этому пользователю сегодня
        if cell_value == "?" and chat_id not in sent_messages_today:
            keyboard = [
                [InlineKeyboardButton("Да", callback_data=f"tomorrow_yes_{chat_id}")],
                [InlineKeyboardButton("Нет", callback_data=f"tomorrow_no_{chat_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            message_text = (
                f"Добрый день, {name}.\n"
                "Если завтра будет работа, готовы выйти?"
            )
            try:
                await application.bot.send_message(chat_id=chat_id, text=message_text, reply_markup=reply_markup)
                # Добавляем пользователя в список отправленных
                sent_messages_today[chat_id] = True
                
                # Добавляем пользователя в список уведомленных
                users_notified.append(name)
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения пользователю {name} (chat_id: {chat_id}): {e}")
    
    # Если есть пользователи, которым отправлен опрос, уведомляем администраторов
    if users_notified:
        report = "Список сотрудников, которым отправлен опрос о работе с символом (?) на завтра:\n" + "\n".join(users_notified)
        await notify_admins(sklad_data, report)

async def handle_tomorrow_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        logger.error("Callback query отсутствует.")
        return
    await query.answer()
    
    data = query.data
    if data.startswith("tomorrow_yes_") or data.startswith("tomorrow_no_"):
        chat_id = int(data.split("_")[-1])
        response = "Да" if data.startswith("tomorrow_yes_") else "Нет"
        
        name = None
        for sklad_name, sklad_data in SCALD_DATA.items():
            google_sheet_file = sklad_data["google_sheet_file"]
            sheet_key = sklad_data["sheet_key"]
            client = get_gspread_client(google_sheet_file)
            sheet = await get_sheet_by_date(client, sheet_key, datetime.now())
            if not sheet:
                continue
            
            rows = sheet.get_all_values()
            for row in rows[1:]:
                if str(chat_id) == row[COLUMN_CHAT_ID - 1]:
                    name = row[COLUMN_NAME - 1]
                    break
        
        if name:
            # Отправляем администраторам результат опроса
            message_to_admin = f"{name} на вопрос (сможет ли он завтра выйти на работу) ответил(а): {response}"
            await notify_admins(SCALD_DATA["Ozon"], message_to_admin)
            
            # Ответ пользователю
            await query.edit_message_text(text=f"Вы ответили: {response}. Спасибо!")
        else:
            logger.warning(f"Пользователь с chat_id {chat_id} не найден в таблице.")
            await query.edit_message_text(text="Произошла ошибка. Пожалуйста, попробуйте позже.")
            
async def start_question_mark_checker(context):
    global scheduler
    scheduler.add_job(check_question_marks_for_tomorrow, 'interval', minutes=2, args=["Ozon"])
    logger.info("Проверка вопросительных знаков для завтрашнего дня запущена.")


# Обработка выбора процесса для изменения времени
async def set_time_process_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text
    if not user_input.isdigit():
        await update.message.reply_text("Неверный формат. Введите номер процесса.")
        return SET_TIME_PROCESS

    process_index = int(user_input) - 1
    processes = [
        "Утренний опрос",
        "Утренний отчет",
        "Утренняя рассылка рабочего места",
        "Дневной опрос",
        "Дневной отчет",
        "Вернуться назад"
    ]

    if process_index < 0 or process_index >= len(processes):
        await update.message.reply_text("Неверный номер процесса. Попробуйте снова.")
        return SET_TIME_PROCESS

    if process_index == 5:  # Вернуться назад
        await update.message.reply_text("Отмена команды /set_time.")
        return ConversationHandler.END

    process = processes[process_index]
    context.user_data['process'] = process
    await update.message.reply_text("Введите время в формате HH:MM (например, 13:42):")
    return SET_TIME

# Обработка ввода времени
async def set_time_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return ConversationHandler.END

    time_input = update.message.text
    try:
        hour, minute = map(int, time_input.split(':'))
        if not (0 <= hour < 24) or not (0 <= minute < 60):
            raise ValueError("Неверный формат времени.")
    except ValueError:
        await update.message.reply_text("Неверный формат времени. Введите время в формате HH:MM (например, 13:42).")
        return SET_TIME

    process = context.user_data.get('process')
    if not process:
        logger.error("Процесс не выбран.")
        await update.message.reply_text("Процесс не выбран. Пожалуйста, начните команду заново.")
        return ConversationHandler.END

    sklad_data[process] = (hour, minute)
    logger.info(f"Время для процесса {process} установлено на {hour:02}:{minute:02} для склада {sklad_name}.")

    # Перезапуск планировщика с новыми временами
    await restart_schedulers()

    await update.message.reply_text(f"Время для процесса {process} установлено на {hour:02}:{minute:02}.")
    return ConversationHandler.END

# Перезапуск планировщика с новыми временами
async def restart_schedulers():
    global scheduler
    if scheduler:
        scheduler.shutdown()
    scheduler = AsyncIOScheduler()
    for sklad_name, sklad_data in SCALD_DATA.items():
        scheduler.add_job(morning_check, 'cron', hour=sklad_data["Утренний опрос"][0], minute=sklad_data["Утренний опрос"][1], args=[sklad_name])
        scheduler.add_job(morning_report_to_admin, 'cron', hour=sklad_data["Утренний отчет"][0], minute=sklad_data["Утренний отчет"][1], args=[sklad_name])
        scheduler.add_job(day_check, 'cron', hour=sklad_data["Дневной опрос"][0], minute=sklad_data["Дневной опрос"][1], args=[sklad_name])
        scheduler.add_job(day_report_to_admin, 'cron', hour=sklad_data["Дневной отчет"][0], minute=sklad_data["Дневной отчет"][1], args=[sklad_name])
        scheduler.add_job(morning_spisok_report_to_admin, 'cron', hour=sklad_data["Утренняя рассылка рабочего места"][0], minute=sklad_data["Утренняя рассылка рабочего места"][1], args=[sklad_name])
    
    # Добавляем новую задачу
    scheduler.add_job(check_question_marks_for_tomorrow, 'interval', minutes=2, args=["Ozon"])
    
    scheduler.start()
    logger.info("Планировщик перезапущен с новыми временами.")

# Обработчик отмены регистрации
async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Регистрация отменена.")
    return ConversationHandler.END

# Обработчик отмены команды /stats_month
async def cancel_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отмена команды /stats_month.")
    return ConversationHandler.END

# Обработчик отмены команды /set_time
async def cancel_set_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отмена команды /set_time.")
    return ConversationHandler.END

async def handle_callback_query_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.callback_query:
        logger.error(f"Обработка ошибки без callback query: {context.error}")
        return
    try:
        await update.callback_query.answer("Произошла ошибка. Пожалуйста, попробуйте позже.", show_alert=True)
    except httpx.TimeoutException:
        logger.error(f"Таймаут при ответе на callback query: {context.error}")
    except Exception as e:
        logger.error(f"Ошибка обработки callback query: {e}")

# Функция для проверки таблиц при запуске бота
async def check_sheets_on_startup():
    for sklad_name, sklad_data in SCALD_DATA.items():
        google_sheet_file = sklad_data["google_sheet_file"]
        sheet_key = sklad_data["sheet_key"]
        client = get_gspread_client(google_sheet_file)
        sheet = await get_sheet_by_date(client, sheet_key, datetime.now())
        if not sheet:
            await notify_admins(sklad_data, f"⚠️ Лист для текущей даты не найден в таблице для склада {sklad_name}.")
            continue
        today = datetime.now().strftime("%d.%m.%Y")
        header_row = sheet.row_values(1)
        if today not in header_row:
            await notify_admins(sklad_data, f"⚠️ Столбец с датой {today} не найден в таблице для склада {sklad_name}.")

# Установка планировщиков для каждого склада
async def start_schedulers(context):
    global scheduler
    scheduler = AsyncIOScheduler()
    for sklad_name, sklad_data in SCALD_DATA.items():
        scheduler.add_job(morning_check, 'cron', hour=sklad_data["Утренний опрос"][0], minute=sklad_data["Утренний опрос"][1], args=[sklad_name])
        scheduler.add_job(morning_report_to_admin, 'cron', hour=sklad_data["Утренний отчет"][0], minute=sklad_data["Утренний отчет"][1], args=[sklad_name])
        scheduler.add_job(day_check, 'cron', hour=sklad_data["Дневной опрос"][0], minute=sklad_data["Дневной опрос"][1], args=[sklad_name])
        scheduler.add_job(day_report_to_admin, 'cron', hour=sklad_data["Дневной отчет"][0], minute=sklad_data["Дневной отчет"][1], args=[sklad_name])
        scheduler.add_job(morning_spisok_report_to_admin, 'cron', hour=sklad_data["Утренняя рассылка рабочего места"][0], minute=sklad_data["Утренняя рассылка рабочего места"][1], args=[sklad_name])
    scheduler.add_job(check_question_marks_for_tomorrow, 'interval', minutes=2, args=["Ozon"])
    # Добавляем новую задачу на 7:05
    scheduler.start()
    logger.info("Планировщик запущен.")

# Обработчик кнопки /start для новых пользователей
async def start_new_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        await update.message.reply_text("Произошла ошибка. Пожалуйста, попробуйте позже.")
        return

    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    sheet = await get_sheet_by_date(client, sheet_key, datetime.now())

    if not sheet:
        logger.warning(f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        await update.message.reply_text(f"Ошибка. Сообщите пожалуйста вашему бригадиру об ошибке.")
        return

    chat_ids = sheet.col_values(COLUMN_CHAT_ID)[1:]

    if str(user_id) not in chat_ids:
        await update.message.reply_text("Здравствуйте! Добро пожаловать! Напишите пожалуйста ваше полное ФИО таким образом: Иванов Иван Иванович")
        return REGISTRATION
    else:
        await update.message.reply_text("Добро пожаловать обратно! Как я могу вам помочь сегодня?")
        return ConversationHandler.END

# Команда /monthly_users
async def monthly_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return

    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    current_month = datetime.now().month
    current_year = datetime.now().year
    sheet_name = f"{month_names[current_month]} {current_year}"

    try:
        spreadsheet = client.open_by_key(sheet_key)
        sheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        logger.warning(f"Лист {sheet_name} не найден в таблице для склада {sklad_name}.")
        await update.message.reply_text(f"Лист {sheet_name} не найден в таблице для склада {sklad_name}.")
        return
    except Exception as e:
        logger.error(f"Ошибка при получении листа {sheet_name}: {e}")
        await update.message.reply_text(f"Произошла ошибка при получении листа {sheet_name}: {e}")
        return

    chat_ids = sheet.col_values(COLUMN_CHAT_ID)[1:]  # Получаем все chat_id из столбца с chat_id (C)
    registered_count = len(chat_ids)

    await update.message.reply_text(f"Количество зарегистрированных пользователей за текущий месяц ({month_names[current_month]} {current_year}): {registered_count}")

# Команда /status
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or user_id not in sklad_data["admin_chat_ids"]:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return

    status_message = (
        "📊 Статус бота:\n"
        "• Работоспособность: Активен\n"
        "• Последние ошибки:\n"
    )
    error_logs = []
    for record in telegram_handler.buffer:  # Предполагаем, что telegram_handler доступен глобально
        if "ERROR" in record:
            error_logs.append(record)

    if error_logs:
        status_message += "\n".join(error_logs[:5])
    else:
        status_message += "Нет критических ошибок.\n"

    await update.message.reply_text(status_message)

# Функция для отправки сообщения о рабочем месте
async def send_workplace_list_to_employees(sklad_name: str, symbols_dict: dict, today: str):
    global sent_messages_today
    today_date = datetime.now().date()
    
    # Сбрасываем sent_messages_today, если дата изменилась
    if "last_reset_date" not in sent_messages_today or sent_messages_today["last_reset_date"] != today_date:
        sent_messages_today = {"last_reset_date": today_date}
    
    # Формируем отчет для сотрудников
    report = f"Сегодняшняя дата: {today}\n"
    for symbol, workers in sorted(symbols_dict.items()):  # Сортировка по символам для единообразия
        report += f"{len(workers)} человека на {symbol.upper()}:\n" + "\n".join(workers) + "\n\n"
  
    # Получаем список chat_id сотрудников с символами
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        return
    
    google_sheet_file = sklad_data["google_sheet_file"]
    sheet_key = sklad_data["sheet_key"]
    client = get_gspread_client(google_sheet_file)
    sheet = await get_sheet_by_date(client, sheet_key, datetime.now())
    if not sheet:
        logger.warning(f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Лист для текущей даты не найден в таблице для склада {sklad_name}.")
        return
    
    header_row = sheet.row_values(1)
    if today not in header_row:
        logger.warning(f"Столбец с датой {today} не найден в таблице для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Столбец с датой {today} не найден в таблице для склада {sklad_name}.")
        return
    
    col_index = header_row.index(today) + 1
    rows = sheet.get_all_values()
    employee_chat_ids = []
    
    for row in rows[1:]:
        cell_value = row[col_index - 1].strip()
        chat_id_str = row[COLUMN_CHAT_ID - 1]
        if not chat_id_str or not chat_id_str.isdigit():
            logger.warning(f"Пропущена строка с некорректным или отсутствующим chat_id: {row}")
            continue
        if cell_value and cell_value != "?":
            employee_chat_ids.append(int(chat_id_str))
    
    # Отправляем отчет всем сотрудникам
    for chat_id in employee_chat_ids:
        if chat_id in sent_messages_today:
            logger.info(f"Сообщение для chat_id {chat_id} уже отправлено сегодня, пропускаем.")
            continue
        try:
            await application.bot.send_message(chat_id=chat_id, text=report)
            sent_messages_today[chat_id] = True
            logger.info(f"Отправлен список сотрудников с chat_id {chat_id}")
        except error.BadRequest as e:
            if "Chat not found" in str(e):
                logger.warning(f"Chat not found для сотрудника {chat_id}. Пропускаем.")
                continue
            else:
                logger.error(f"BadRequest для сотрудника {chat_id}: {e}")
        except error.TelegramError as e:  # Общий TelegramError
            logger.error(f"Ошибка отправки списка сотруднику с chat_id {chat_id}: {e}")
            await notify_admins(sklad_data, f"Ошибка отправки списка сотруднику с chat_id {chat_id}: {e}")
            continue

async def send_jupiter_list_to_admins(sklad_name: str, symbol_counts: dict, today: str):
    sklad_data = SCALD_DATA.get(sklad_name)
    if not sklad_data:
        logger.warning(f"Данные для склада {sklad_name} не найдены.")
        return
    
    if not symbol_counts:
        logger.info(f"Нет сотрудников с символами для списка Юпитер на {today} для склада {sklad_name}.")
        await notify_admins(sklad_data, f"Нет сотрудников с символами для списка Юпитер на {today} для склада {sklad_name}.")
        return
    
    sorted_symbols = sorted(symbol_counts.keys())
    report = f"Юпитер {today}\n \n"
    for symbol in sorted_symbols:
        report += f"{symbol} - {symbol_counts[symbol]}\n"
    
    await notify_admins(sklad_data, report)
    logger.info(f"Отправлен список Юпитер администраторам для склада {sklad_name} на {today}")

# Функция для проверки доступа администратора
def is_admin(user_id, sklad_data):
    return user_id in sklad_data.get("admin_chat_ids", [])

# Ограничение доступа к командам только для администраторов
async def restricted_command(update: Update, context: ContextTypes.DEFAULT_TYPE, command_name: str):
    user_id = update.message.from_user.id
    sklad_name = "Ozon"
    sklad_data = SCALD_DATA.get(sklad_name)

    if not sklad_data or not is_admin(user_id, sklad_data):
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return

    # Выполняем команду, если пользователь администратор
    if command_name == "stats":
        await stats(update, context)
    elif command_name == "stats_month":
        await stats_month_start(update, context)
    elif command_name == "status":
        await status(update, context)
    elif command_name == "set_time":
        await set_time_start(update, context)
    elif command_name == "day_check_participants":
        await day_check_participants(update, context)
    elif command_name == "add_admin":
        await add_admin(update, context)

# Обработчик команды /stats
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await restricted_command(update, context, "stats")

# Обработчик команды /stats_month
async def stats_month_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await restricted_command(update, context, "stats_month")

# Обработчик команды /status
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await restricted_command(update, context, "status")

# Обработчик команды /set_time
async def set_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await restricted_command(update, context, "set_time")

# Обработчик команды /day_check_participants
async def day_check_participants_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await restricted_command(update, context, "day_check_participants")

# Обработчик команды /add_admin
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await restricted_command(update, context, "add_admin")

# Функция для отправки сообщений администраторам
async def notify_admins(sklad_data: dict, message: str):
    if not message.strip():  # Пропускаем, если сообщение пустое (игнорируя пробелы)
        print("Пустое сообщение для админов. Пропускаем.")
        return
    admin_chat_ids = sklad_data.get("admin_chat_ids", [])
    max_message_length = 4000
    parts = [message[i:i + max_message_length] for i in range(0, len(message), max_message_length)]
    for admin_id in admin_chat_ids:
        for part_num, part in enumerate(parts, 1):
            if not part.strip():
                continue
            part_text = f"Часть {part_num}/{len(parts)}:\n{part}" if len(parts) > 1 else part
            try:
                await application.bot.send_message(chat_id=admin_id, text=part_text)
            except error.BadRequest as e:
                if "Chat not found" in str(e):
                    print(f"Chat not found для админа {admin_id}. Пропускаем.")
                    continue
                elif "Message text is empty" in str(e):
                    print("Пустой текст сообщения. Пропускаем.")
                    continue
                elif "Message is too long" in str(e):
                    print(f"Текст все еще слишком длинный для админа {admin_id}: {e}. Пропускаем эту часть.")
                    continue
                else:
                    print(f"BadRequest админу {admin_id}: {e}")
            except Exception as e:
                print(f"Ошибка отправки админу {admin_id}: {e}")
                continue

# Глобальные списки откликнувшихся пользователей
response_tracking = {}
morning_response_tracking = {}

# Словарь для отслеживания пользователей, которым уже отправлено сообщение сегодня
sent_messages_today = {}

# Инициализация данных для всех складов
for sklad_name in SCALD_DATA.keys():
    response_tracking[sklad_name] = {}
    morning_response_tracking[sklad_name] = {}

# Основная функция
if __name__ == "__main__":
    # Использование переменной окружения для токена
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_Ozon")
    if not TOKEN:
        # Временно используем непосредственный токен для тестирования
        TOKEN = ""
        logger.warning("Telegram bot token for Ozon is not set in environment variables. Using hardcoded token for testing purposes.")

    application = Application.builder().token(TOKEN).connect_timeout(30).read_timeout(30).build()

    # Инициализация кастомного обработчика логов после создания application
    telegram_handler = TelegramLoggerHandler()
    logger.addHandler(telegram_handler)

    conv_handler_registration = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTRATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration),
                CommandHandler("cancel", cancel_registration)
            ],
            PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone),
                CommandHandler("cancel", cancel_registration)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_registration)],
        per_message=False  # Устанавливаем per_message=False для отслеживания команд и callback queries
    )

    conv_handler_stats_month = ConversationHandler(
        entry_points=[CommandHandler("stats_month", stats_month_start)],
        states={
            STATS_MONTH: [
                CallbackQueryHandler(stats_month_chosen, pattern=r"^month_\d+$")
            ],
            STATS_YEAR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, stats_year_chosen)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_stats)],
        per_message=False  # Устанавливаем per_message=False для отслеживания команд и callback queries
    )

    conv_handler_set_time = ConversationHandler(
        entry_points=[CommandHandler("set_time", set_time_start)],
        states={
            SET_TIME_PROCESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_time_process_chosen)
            ],
            SET_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_time_chosen)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_set_time)],
        per_message=False  # Устанавливаем per_message=False для отслеживания команд и callback queries
    )

    application.add_handler(conv_handler_registration)
    application.add_handler(conv_handler_stats_month)
    application.add_handler(conv_handler_set_time)
    application.add_handler(CallbackQueryHandler(handle_button_click, pattern="response_yes"))
    application.add_handler(CallbackQueryHandler(handle_morning_response, pattern="morning_(yes|no)"))
    application.add_handler(CallbackQueryHandler(handle_tomorrow_response, pattern=r"^tomorrow_(yes|no)_"))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("monthly_users", monthly_users))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("day_check_participants", day_check_participants_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add_admin", add_admin_command))
    application.add_handler(conv_handler_workers)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    application.add_handler(MessageHandler(filters.COMMAND, start_new_user))  # Обработчик для новых пользователей

    # Добавление обработчика ошибок для callback queries
    application.add_error_handler(handle_callback_query_error)

    # Запуск планировщиков через выполнение асинхронной задачи
    application.job_queue.run_once(lambda context: asyncio.ensure_future(start_schedulers(context)), 0)
    application.job_queue.run_once(lambda context: asyncio.ensure_future(check_sheets_on_startup()), 0)

    # Запуск бота
    try:
        application.run_polling()
    except KeyboardInterrupt:
        logger.info("Бот остановлен по команде пользователя.")
        if scheduler:
            scheduler.shutdown()
        logger.info("Планировщик остановлен.")