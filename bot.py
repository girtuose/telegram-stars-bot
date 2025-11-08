import logging
import json
import os
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from enum import Enum
import telebot
from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton
)
from dotenv import load_dotenv
import redis

load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(_name_)

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# Инициализация бота
bot = telebot.TeleBot(TOKEN)

# Константы
class OrderStatus(Enum):
    PENDING = "pending"
    PAID = "paid"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    PAYMENT_ERROR = "payment_error"

class UserRole(Enum):
    USER = "user"
    ADMIN = "admin"

# Конфигурация пакетов
TELEGRAM_STARS_PACKAGES = {
    "buy_50": {"amount": 50, "price": 80, "points": 1, "discount": 0},
    "buy_75": {"amount": 75, "price": 130, "points": 2, "discount": 5},
    "buy_100": {"amount": 100, "price": 160, "points": 2, "discount": 10},
    "buy_250": {"amount": 250, "price": 380, "points": 4, "discount": 15},
    "buy_500": {"amount": 500, "price": 780, "points": 8, "discount": 20},
    "buy_750": {"amount": 750, "price": 1300, "points": 12, "discount": 25},
    "buy_1000": {"amount": 1000, "price": 1580, "points": 15, "discount": 30},
}

# Состояния для пользователей
user_states = {}

class SecurityManager:
    @staticmethod
    def validate_user_input(text: str, max_length: int = 100) -> bool:
        if not text or len(text) > max_length:
            return False
        dangerous_patterns = ['<script>', '../', ';', '--']
        return not any(pattern in text.lower() for pattern in dangerous_patterns)
    
    @staticmethod
    def generate_order_id() -> str:
        timestamp = int(datetime.now().timestamp())
        random_part = random.randint(1000, 9999)
        return f"ORD{timestamp}{random_part}"

class DatabaseManager:
    def _init_(self):
        try:
            self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        except:
            self.redis_client = None
    
    def get_user_data(self, user_id: int) -> Dict:
        try:
            if not self.redis_client:
                return self._get_default_user_data()
                
            key = f"user:{user_id}"
            data = self.redis_client.get(key)
            if data:
                return json.loads(data)
            
            default_data = self._get_default_user_data()
            self.update_user_data(user_id, default_data)
            return default_data
        except Exception as e:
            logger.error(f"Error getting user data: {e}")
            return self._get_default_user_data()
    
    def _get_default_user_data(self):
        return {
            "username": "",
            "total_stars": 0,
            "total_spent": 0,
            "points": 0,
            "orders_count": 0,
            "role": UserRole.USER.value,
            "registration_date": datetime.now().isoformat(),
            "last_activity": datetime.now().isoformat(),
            "notifications": True
        }
    
    def update_user_data(self, user_id: int, updates: Dict):
        try:
            if not self.redis_client:
                return
                
            key = f"user:{user_id}"
            current_data = self.get_user_data(user_id)
            current_data.update(updates)
            current_data["last_activity"] = datetime.now().isoformat()
            self.redis_client.set(key, json.dumps(current_data), ex=86400*30)
        except Exception as e:
            logger.error(f"Error updating user data: {e}")
    
    def create_order(self, order_data: Dict) -> str:
        try:
            if not self.redis_client:
                return SecurityManager.generate_order_id()
                
            order_id = SecurityManager.generate_order_id()
            order_data["order_id"] = order_id
            order_data["created_at"] = datetime.now().isoformat()
            order_data["status"] = OrderStatus.PENDING.value
            
            key = f"order:{order_id}"
            self.redis_client.set(key, json.dumps(order_data), ex=86400*7)
            
            return order_id
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            return SecurityManager.generate_order_id()

# Инициализация менеджеров
db = DatabaseManager()

def get_user_role(user_id: int) -> UserRole:
    return UserRole.ADMIN if str(user_id) == ADMIN_CHAT_ID else UserRole.USER

# Базовые обработчики
@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    user_role = get_user_role(user_id)
    
    db.update_user_data(user_id, {
        "username": message.from_user.username or "",
        "first_name": message.from_user.first_name or ""
    })
    
    if user_role == UserRole.ADMIN:
        keyboard = [
            [KeyboardButton("📊 Статистика"), KeyboardButton("📦 Заказы")],
            [KeyboardButton("👥 Пользователи")]
        ]
    else:
        keyboard = [
            [KeyboardButton("🛒 Купить Stars"), KeyboardButton("👤 Профиль")],
            [KeyboardButton("🆘 Поддержка")]
        ]
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_text = (
        f"🌟 Добро пожаловать, {message.from_user.first_name}!\n\n"
        "⚡ <b>Telegram Stars Bot</b> - быстрая и надежная покупка Stars\n\n"
        "✅ <b>Преимущества:</b>\n"
        "• 🚀 Доставка: 1-6 часов\n"
        "• 🎁 Бонусная система\n"
        "• 💎 Гарантия доставки\n"
        "• 🔒 Безопасные платежи\n\n"
        "Выберите действие ниже 👇"
    )
    
    bot.send_message(message.chat.id, welcome_text, reply_markup=reply_markup, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text == "🛒 Купить Stars")
def show_stars_packages(message):
    keyboard = []
    for key, package in TELEGRAM_STARS_PACKAGES.items():
        discount_text = f" 🔥 -{package['discount']}%" if package['discount'] > 0 else ""
        button_text = f"{package['amount']} Stars - {package['price']} руб.{discount_text}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=key)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    info_text = (
        "🎯 <b>Выберите количество Telegram Stars</b>\n\n"
        "⚡ <b>Доставка:</b> 1-6 часов\n"
        "💎 <b>Гарантия доставки</b>\n"
        "🎁 <b>Бонусные очки</b> за каждую покупку!\n\n"
        "🔥 <i>Скидки на крупные пакеты!</i>"
    )
    
    bot.send_message(message.chat.id, info_text, reply_markup=reply_markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_package_selection(call):
    selected_package = TELEGRAM_STARS_PACKAGES.get(call.data)
    
    if selected_package:
        user_states[call.from_user.id] = {
            'current_order': selected_package,
            'step': 'waiting_username'
        }
        
        order_text = (
            f"🎯 <b>Вы выбрали:</b> {selected_package['amount']} Telegram Stars\n"
            f"💰 <b>Сумма к оплате:</b> {selected_package['price']} руб.\n"
            f"🎁 <b>Бонусные очки:</b> {selected_package['points']}\n"
        )
        
        if selected_package['discount'] > 0:
            order_text += f"🔥 <b>Скидка:</b> {selected_package['discount']}%\n"
        
        order_text += (
            "\n📝 <b>Отправьте ваш Telegram username (без @):</b>\n\n"
            "⚠ <b>ВНИМАНИЕ:</b>\n"
            "• Username должен быть публичным\n"
            "• Убедитесь в правильности написания"
        )
        
        bot.edit_message_text(order_text, call.message.chat.id, call.message.message_id, parse_mode='HTML')
    else:
        bot.edit_message_text("❌ Произошла ошибка. Пожалуйста, начните заново", call.message.chat.id, call.message.message_id)

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('step') == 'waiting_username')
def handle_telegram_username(message):
    telegram_username = message.text.strip()
    
    if not SecurityManager.validate_user_input(telegram_username):
        bot.send_message(message.chat.id, "❌ Некорректный username. Попробуйте еще раз:")
        return
    
    telegram_username = telegram_username.replace('@', '')
    user_state = user_states[message.from_user.id]
    order = user_state['current_order']
    user_state['telegram_username'] = telegram_username
    user_state['step'] = 'waiting_payment'
    
    payment_info = (
        f"✅ <b>Заказ создан!</b>\n\n"
        f"• ⭐ Stars: {order['amount']}\n"
        f"• 💰 Сумма: {order['price']} руб.\n"
        f"• 👤 Ваш Telegram: @{telegram_username}\n"
        f"• 🎁 Очков: {order['points']}\n\n"
        f"💳 <b>Реквизиты для оплаты:</b>\n"
        f"<code>2202 2002 2020 2020</code> - СБЕРБАНК\n\n"
        f"📸 <b>После оплаты прикрепите скриншот чека</b>\n"
        f"⚡ <b>Доставка:</b> 1-6 часов после проверки"
    )
    
    bot.send_message(message.chat.id, payment_info, parse_mode='HTML')

@bot.message_handler(content_types=['photo'], 
                    func=lambda message: user_states.get(message.from_user.id, {}).get('step') == 'waiting_payment')
def handle_payment_screenshot(message):
    user_id = message.from_user.id
    user_state = user_states.get(user_id, {})
    order_data = user_state.get('current_order')
    telegram_username = user_state.get('telegram_username')
    
    try:
        order_info = {
            'user_id': user_id,
            'username': message.from_user.username or '',
            'first_name': message.from_user.first_name or '',
            'telegram_username': telegram_username,
            'stars_amount': order_data['amount'],
            'price': order_data['price'],
            'points': order_data['points'],
        }
        
        order_id = db.create_order(order_info)
        
        user_msg = (
            f"📸 <b>Скриншот получен!</b>\n\n"
            f"🆔 <b>Номер заказа:</b> #{order_id}\n"
            f"⏱ <b>Статус:</b> Ожидает проверки\n"
            f"🚚 <b>Доставка:</b> 1-6 часов\n\n"
            f"Мы уведомим вас о смене статуса заказа."
        )
        
        bot.send_message(message.chat.id, user_msg, parse_mode='HTML')
        
        # Очищаем состояние пользователя
        user_states.pop(user_id, None)
        
    except Exception as e:
        logger.error(f"Error processing payment: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при обработке заказа. Попробуйте еще раз.")
        user_states.pop(user_id, None)

@bot.message_handler(func=lambda message: message.text == "👤 Профиль")
def show_profile(message):
    user_id = message.from_user.id
    user_data = db.get_user_data(user_id)
    
    total_spent = user_data.get('total_spent', 0)
    if total_spent >= 5000:
        level = "💎 Платиновый"
    elif total_spent >= 2000:
        level = "🔥 Золотой"
    elif total_spent >= 500:
        level = "⚡ Серебряный"
    else:
        level = "🎯 Бронзовый"
    
    profile_text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"💎 <b>Уровень:</b> {level}\n"
        f"⭐ <b>Куплено Stars:</b> {user_data.get('total_stars', 0)}\n"
        f"💰 <b>Всего потрачено:</b> {user_data.get('total_spent', 0)} руб.\n"
        f"🎯 <b>Накоплено очков:</b> {user_data.get('points', 0)}\n"
        f"📦 <b>Заказов:</b> {user_data.get('orders_count', 0)}\n"
        f"📅 <b>Регистрация:</b> {user_data.get('registration_date', 'N/A')[:16]}\n\n"
        f"💡 Накопите очки и обменивайте их на Stars!"
    )
    
    bot.send_message(message.chat.id, profile_text, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text == "🆘 Поддержка")
def show_support(message):
    support_text = (
        f"🆘 <b>Поддержка</b>\n\n"
        f"По всем вопросам обращайтесь:\n"
        f"👤 {SUPPORT_USERNAME}\n\n"
        f"📞 <b>Мы поможем:</b>\n"
        f"• С вопросами по заказам\n"
        f"• С проблемами оплаты\n"
        f"• С техническими неполадками"
    )
    bot.send_message(message.chat.id, support_text, parse_mode='HTML')

@bot.message_handler(commands=['help'])
def help_handler(message):
    help_text = (
        "🤖 <b>Доступные команды:</b>\n\n"
        "/start - Запустить бота\n"
        "/help - Помощь\n"
        "/cancel - Отменить текущее действие\n\n"
        "📱 <b>Основные функции:</b>\n"
        "• 🛒 Купить Stars - Выбор пакета Stars\n"
        "• 👤 Профиль - Ваша статистика\n"
        "• 🆘 Поддержка - Связь с поддержкой"
    )
    bot.send_message(message.chat.id, help_text, parse_mode='HTML')

@bot.message_handler(commands=['cancel'])
def cancel_handler(message):
    user_id = message.from_user.id
    if user_id in user_states:
        user_states.pop(user_id)
        bot.send_message(message.chat.id, "❌ Текущее действие отменено.")
    else:
        bot.send_message(message.chat.id, "❌ Нечего отменять.")

# Запуск бота
if _name_ == '_main_':
    print("🤖 Бот запускается...")
    try:
        bot.infinity_polling()
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        print(f"❌ Ошибка: {e}")
