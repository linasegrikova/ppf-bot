import asyncio
import logging
import os
import re
import sys
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# 🚀 Оптимизация сетевого цикла на Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==============================================================================
# ⚙️ НАСТРОЙКИ С ВАШИМИ ДАННЫМИ
# ==============================================================================
import os
TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_ID = 8864323031

PHONE_NUMBER = "+375336689966"
INSTAGRAM_URL = "https://www.instagram.com/ppf.lab.by/"
WEBSITE_URL = "https://ppflab.by"
MAPS_URL = "https://yandex.by/maps/157/minsk/?ll=27.450889%2C53.825487&mode=poi&poi%5Bpoint%5D=27.450787%2C53.825501&poi%5Buri%5D=ymapsbm1%3A%2F%2Forg%3Foid%3D70527229275&z=18"

# Прайс-лист на элементы (минимальные цены «от» в BYN)
PRICE_LIST = {
    "hood": {"name": "Капот", "price": 600, "type": "bool"},
    "front_fenders": {"name": "Передние крылья (пара)", "price": 480, "type": "bool"},
    "front_bumper": {"name": "Передний бампер", "price": 700, "type": "bool"},
    "bumper_details": {"name": "Детализация бампера (эл-ты)", "price": 15, "type": "count", "max": 10},
    "headlights": {"name": "Фары (пара)", "price": 150, "type": "bool"},
    "mirrors": {"name": "Зеркала (пара)", "price": 180, "type": "bool"},
    "pillars": {"name": "Стойки лобового (пара)", "price": 180, "type": "bool"},
    "windshield_strip": {"name": "Полоса над лобовым (50 см)", "price": 100, "type": "bool"},
    "door_edges_cups": {"name": "Зоны ручек / канты дверей", "price": 60, "type": "bool"},
    "inner_sills": {"name": "Внутренние пороги", "price": 150, "type": "bool"},
    "outer_sills": {"name": "Внешние пороги", "price": 500, "type": "bool"},
    "trunk_sill": {"name": "Зона погрузки (задний бампер)", "price": 100, "type": "bool"},
    "roof": {"name": "Крыша целиком", "price": 500, "type": "bool"},
    "doors": {"name": "Двери целиком", "price": 450, "type": "count", "max": 4},
    "trunk_lid": {"name": "Крышка багажника", "price": 300, "type": "bool"},
    "rear_fenders": {"name": "Задние крылья", "price": 750, "type": "count", "max": 2},
}

class Form(StatesGroup):
    calculating = State()
    waiting_car_model = State()
    waiting_manager_contact = State()

# Сверхбыстрый RAM-кеш сессий и очередь задач дебаунса
user_calc_cache: dict[int, dict] = {}
user_edit_tasks: dict[int, asyncio.Task] = {}

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="Markdown")
)
dp = Dispatcher(storage=MemoryStorage())

# ==============================================================================
# 🌐 МИКРО-СЕРВЕР ДЛЯ ПРОВЕРКИ RENDER (HEALTH CHECK)
# ==============================================================================
async def handle_ping(request):
    return web.Response(text="PPF.LAB Bot is live and running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Фоновый веб-сервер проверки запущен на порту {port}!")

# ==============================================================================
# 🎛️ КЛАВИАТУРЫ
# ==============================================================================
def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Сделать расчёт оклейки", callback_data="start_calc")],
        [InlineKeyboardButton(text="👨‍💼 Связаться с мастером", callback_data="call_manager")],
        [InlineKeyboardButton(text="📞 Контакты студии", callback_data="show_contacts")],
        [InlineKeyboardButton(text="📍 Наш адрес на Яндекс Картах", url=MAPS_URL)],
    ])


def get_calc_keyboard(selected_items: dict):
    buttons = []
    total_price = 0

    for key, data in PRICE_LIST.items():
        qty = selected_items.get(key, 0)
        item_cost = qty * data["price"]
        total_price += item_cost

        if data["type"] == "bool":
            status = "✅" if qty > 0 else "⬜"
            btn_text = f"{status} {data['name']} — от {data['price']} BYN"
        else:
            status = f"✅ ({qty} шт)" if qty > 0 else "⬜ (0 шт)"
            btn_text = f"{status} {data['name']} — от {data['price']} BYN/шт"

        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"toggle_{key}")])

    control_buttons = []
    if total_price > 0:
        control_buttons.append(
            [InlineKeyboardButton(text=f"💵 Рассчитать (от {total_price} BYN) ➔", callback_data="finish_calc")]
        )
    control_buttons.append(
        [
            InlineKeyboardButton(text="🔄 Сбросить выбор", callback_data="reset_calc"),
            InlineKeyboardButton(text="◀️ В главное меню", callback_data="back_to_main"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons + control_buttons), total_price

# Функция отложенной перерисовки клавиатуры (Дебаунс)
async def debounced_edit_markup(message: types.Message, user_id: int):
    # Небольшая пауза для группировки частых кликов (0.15 сек)
    await asyncio.sleep(0.15)
    selected = user_calc_cache.get(user_id, {})
    kb, _ = get_calc_keyboard(selected)
    try:
        await message.edit_reply_markup(reply_markup=kb)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
    except Exception:
        pass

# ==============================================================================
# 🚀 ХЕНДЛЕРЫ
# ==============================================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_calc_cache.pop(message.from_user.id, None)
    welcome_text = (
        f"👋 **Приветствуем в студии PPF.LAB!**\n\n"
        f"🛡️ Профессиональная защита и стайлинг автомобилей "
        f"премиальными полиуретановыми пленками в Минске.\n\n"
        f"✨ **Наши преимущества:**\n"
        f"• Аккуратный арматурный разбор без повреждений\n"
        f"• Глубокий подворот краев без видимых стыков\n"
        f"• **Бессрочная гарантия** на выполненные работы\n\n"
        f"Выберите действие в меню ниже 👇"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu())


@dp.callback_query(F.data == "back_to_main")
async def back_to_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    if user_id in user_edit_tasks and not user_edit_tasks[user_id].done():
        user_edit_tasks[user_id].cancel()
        
    await state.clear()
    user_calc_cache.pop(user_id, None)
    welcome_text = (
        f"👋 **Главное меню студии PPF.LAB**\n\n"
        f"Выберите интересующий вас раздел:"
    )
    try:
        await callback.message.edit_text(welcome_text, reply_markup=get_main_menu())
    except Exception:
        pass


@dp.callback_query(F.data == "show_contacts")
async def show_contacts(callback: types.CallbackQuery):
    await callback.answer()
    contacts_text = (
        f"📞 **Контакты студии PPF.LAB**\n\n"
        f"📱 **Телефон / Telegram:** [{PHONE_NUMBER}](tel:{PHONE_NUMBER})\n"
        f"📸 **Instagram:** [@ppf.lab.by]({INSTAGRAM_URL})\n"
        f"🌐 **Наш сайт:** [ppflab.by]({WEBSITE_URL})\n"
        f"📍 **Адрес:** г. Минск\n\n"
        f"⏰ Работаем по предварительной записи. Ждём вас в гости!"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📍 Открыть точку на Яндекс Картах", url=MAPS_URL)],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_main")],
        ]
    )
    try:
        await callback.message.edit_text(contacts_text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        pass

# ==============================================================================
# 🧮 КАЛЬКУЛЯТОР ОКЛЕЙКИ С ДЕБАУНСОМ (ЗАЩИТА ОТ ТРОТТЛИНГА)
# ==============================================================================
@dp.callback_query(F.data == "start_calc")
async def start_calc(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Form.calculating)
    user_id = callback.from_user.id
    user_calc_cache[user_id] = {}

    kb, _ = get_calc_keyboard(user_calc_cache[user_id])
    calc_text = (
        f"💵 **Интерактивный калькулятор оклейки PPF.LAB**\n\n"
        f"Нажимайте на нужные элементы, чтобы собрать свой комплект защиты.\n"
        f"Для позиций с количеством (двери, детали бампера) каждое нажатие добавляет +1 шт.\n\n"
        f"ℹ️ *В калькуляторе указаны базовые минимальные цены («от»). Итоговая стоимость зависит от сложности геометрии кузова и выбранного бренда пленки.*\n\n"
        f"👇 **Выберите элементы кузова:**"
    )
    try:
        await callback.message.edit_text(calc_text, reply_markup=kb)
    except Exception:
        pass


@dp.callback_query(Form.calculating, F.data.startswith("toggle_"))
async def toggle_calc_item(callback: types.CallbackQuery, state: FSMContext):
    # 1. Мгновенно глушим анимацию клика в Telegram
    await callback.answer()
    
    user_id = callback.from_user.id
    item_key = callback.data.replace("toggle_", "")
    
    if user_id not in user_calc_cache:
        user_calc_cache[user_id] = {}
        
    selected = user_calc_cache[user_id]
    item_meta = PRICE_LIST.get(item_key)
    if not item_meta:
        return

    # 2. Мгновенное изменение в RAM-памяти (0 мс)
    if item_meta["type"] == "bool":
        selected[item_key] = 0 if selected.get(item_key, 0) > 0 else 1
    else:
        current_qty = selected.get(item_key, 0)
        max_qty = item_meta.get("max", 4)
        selected[item_key] = (current_qty + 1) if current_qty < max_qty else 0

    # 3. Дебаунс: отменяем прошлый запрос, если пользователь быстро кликает дальше
    if user_id in user_edit_tasks and not user_edit_tasks[user_id].done():
        user_edit_tasks[user_id].cancel()

    # Запускаем отложенную перерисовку (объединяет серию быстрых кликов)
    user_edit_tasks[user_id] = asyncio.create_task(
        debounced_edit_markup(callback.message, user_id)
    )


@dp.callback_query(Form.calculating, F.data == "reset_calc")
async def reset_calc(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Выбор сброшен")
    user_id = callback.from_user.id
    if user_id in user_edit_tasks and not user_edit_tasks[user_id].done():
        user_edit_tasks[user_id].cancel()

    user_calc_cache[user_id] = {}
    kb, _ = get_calc_keyboard({})
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


@dp.callback_query(Form.calculating, F.data == "finish_calc")
async def finish_calc_ask_car(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id in user_edit_tasks and not user_edit_tasks[user_id].done():
        user_edit_tasks[user_id].cancel()

    selected = user_calc_cache.get(user_id, {})
    _, total = get_calc_keyboard(selected)

    if total == 0:
        return await callback.answer("Выберите хотя бы один элемент кузова!", show_alert=True)

    await callback.answer()
    await state.set_state(Form.waiting_car_model)
    await state.update_data(selected_items=selected)

    ask_text = (
        f"🚗 **Почти готово!**\n\n"
        f"Предварительный ориентир: **от {total} BYN**\n\n"
        f"Напишите **марку, модель и год выпуска** вашего авто (например: *Geely Monjaro 2024* или *BMW M5 2023*):\n\n"
        f"Это нужно для точного учета площади и сложности форм кузова."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Вернуться к выбору", callback_data="start_calc")]]
    )
    try:
        await callback.message.edit_text(ask_text, reply_markup=kb)
    except Exception:
        pass


@dp.message(Form.waiting_car_model)
async def process_car_model(message: types.Message, state: FSMContext):
    car_model = message.text.strip()
    user_id = message.from_user.id
    selected = user_calc_cache.get(user_id, {})
    
    if not selected:
        data = await state.get_data()
        selected = data.get("selected_items", {})
        
    _, total = get_calc_keyboard(selected)

    items_list_text = ""
    for key, qty in selected.items():
        if qty > 0:
            meta = PRICE_LIST[key]
            if meta["type"] == "bool":
                items_list_text += f"• {meta['name']}: от {meta['price']} BYN\n"
            else:
                items_list_text += f"• {meta['name']} ({qty} шт): от {qty * meta['price']} BYN\n"

    # Ответ клиенту
    client_response = (
        f"✅ **Предварительный расчёт сформирован!**\n\n"
        f"🚗 **Автомобиль:** {car_model}\n\n"
        f"📋 **Выбранный комплект защиты:**\n{items_list_text}\n"
        f"💰 **Базовая стоимость:** **от {total} BYN**\n\n"
        f"⚠️ **Важно:**\n"
        f"Все цены в калькуляторе являются **ориентировочными (минимально базовыми)**. "
        f"Конечная стоимость оклейки рассчитывается индивидуально и, как правило, будет отличаться в зависимости от:\n"
        f"• Сложности геометрии и размеров кузова конкретного авто\n"
        f"• Выбранного бренда и типа полиуретана (глянцевый, матовый сатин, цветной)\n"
        f"• Необходимого объема арматурного разбора для глубокого подворота\n\n"
        f"🛡️ **В работу студии всегда входит:**\n"
        f"• Премиальный полиуретан (PPF)\n"
        f"• Деликатный разбор без повреждения крепежей\n"
        f"• Глубокий подворот краев без резов по лаку\n"
        f"• **Бессрочная гарантия** на выполненную работу\n\n"
        f"Мастер свяжется с вами в Telegram для консультации и согласования точной сметы!"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👨‍💼 Задать вопрос мастеру", callback_data="call_manager")],
            [InlineKeyboardButton(text="📍 Наш адрес на картах", url=MAPS_URL)],
            [InlineKeyboardButton(text="🔄 Сделать новый расчет", callback_data="start_calc")],
        ]
    )
    await message.answer(client_response, reply_markup=kb)

    # 🔔 Уведомление в ваш Telegram
    user = message.from_user
    username_str = f"@{user.username}" if user.username else "нет юзернейма"
    
    admin_alert = (
        f"🔥 **НОВАЯ ЗАЯВКА НА РАСЧЕТ ОКЛЕЙКИ!**\n\n"
        f"👤 **Клиент:** {user.full_name} ({username_str})\n"
        f"🆔 **ID:** `{user.id}`\n"
        f"🚗 **Автомобиль:** {car_model}\n\n"
        f"📋 **Выбранные элементы:**\n{items_list_text}\n"
        f"💰 **Базовый ориентир клиента:** **от {total} BYN**"
    )

    admin_kb_buttons = []
    if user.username:
        admin_kb_buttons.append([InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{user.username}")])
    admin_kb = InlineKeyboardMarkup(inline_keyboard=admin_kb_buttons) if admin_kb_buttons else None

    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_alert, reply_markup=admin_kb)
    except Exception as e:
        logging.error(f"Ошибка отправки админу: {e}")

    await state.clear()
    user_calc_cache.pop(user_id, None)

# ==============================================================================
# 👨‍💼 СВЯЗЬ С МАСТЕРОМ
# ==============================================================================
@dp.callback_query(F.data == "call_manager")
async def ask_manager_contact(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    if user_id in user_edit_tasks and not user_edit_tasks[user_id].done():
        user_edit_tasks[user_id].cancel()

    await state.set_state(Form.waiting_manager_contact)
    text = (
        f"👨‍💼 **Связь с мастером студии PPF.LAB**\n\n"
        f"Напишите ваш **номер телефона** или вопрос одним сообщением.\n"
        f"Мы ответим вам в течение нескольких минут!"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="back_to_main")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@dp.message(Form.waiting_manager_contact)
async def process_manager_message(message: types.Message, state: FSMContext):
    user = message.from_user
    username_str = f"@{user.username}" if user.username else "нет юзернейма"
    contact_info = message.text.strip()

    admin_notification = (
        f"📩 **ЗАПРОС СВЯЗИ С МАСТЕРОМ!**\n\n"
        f"👤 **От:** {user.full_name} ({username_str})\n"
        f"🆔 **ID:** `{user.id}`\n"
        f"💬 **Сообщение / Телефон:**\n{contact_info}"
    )

    admin_kb_buttons = []
    if user.username:
        admin_kb_buttons.append([InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{user.username}")])
    admin_kb = InlineKeyboardMarkup(inline_keyboard=admin_kb_buttons) if admin_kb_buttons else None

    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_notification, reply_markup=admin_kb)
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления: {e}")

    await message.answer(
        "✅ **Спасибо! Ваше сообщение передано мастеру.**\nМы свяжемся с вами в ближайшее время.",
        reply_markup=get_main_menu()
    )
    await state.clear()

# ==============================================================================
# 🏁 ЗАПУСК БОТА + ВЕБ-СЕРВЕРА
# ==============================================================================
async def main():
    logging.basicConfig(level=logging.INFO)
    await bot.delete_webhook(drop_pending_updates=True)
    await start_web_server()
    print("🚀 Бот PPF.LAB успешно запущен с дебаунсом перерисовки!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
