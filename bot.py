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
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove
)
from telebot import custom_filters
from dotenv import load_dotenv
import redis
import hashlib
import hmac

load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME")
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
    SUPPORT = "support"

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

POINTS_REWARDS = {
    "reward_50": {"points": 100, "stars": 50},
    "reward_100": {"points": 190, "stars": 100},
    "reward_250": {"points": 450, "stars": 250},
    "reward_500": {"points": 850, "stars": 500},
}

# Состояния для конечных автоматов
class UserState:
    REQUEST_USERNAME = 1
    WAITING_PAYMENT_PROOF = 2

# Хранилище состояний пользователей
user_states = {}

class SecurityManager:
    @staticmethod
    def validate_user_input(text: str, max_length: int = 100) -> bool:
        if not text or len(text) > max_length:
            return False
        dangerous_patterns = ['<script>', '../', ';', '--', '/', '/']
        return not any(pattern in text.lower() for pattern in dangerous_patterns)
    
    @staticmethod
    def generate_order_id() -> str:
        timestamp = int(datetime.now().timestamp())
        random_part = random.randint(1000, 9999)
        return f"ORD{timestamp}{random_part}"

class DatabaseManager:
    def _init_(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    
    def get_user_data(self, user_id: int) -> Dict:
        try:
            key = f"user:{user_id}"
            data = self.redis_client.get(key)
            if data:
                return json.loads(data)
            
            default_data = {
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
            self.update_user_data(user_id, default_data)
            return default_data
        except Exception as e:
            logger.error(f"Error getting user data: {e}")
            return {}
    
    def update_user_data(self, user_id: int, updates: Dict):
        try:
            key = f"user:{user_id}"
            current_data = self.get_user_data(user_id)
            current_data.update(updates)
            current_data["last_activity"] = datetime.now().isoformat()
            self.redis_client.set(key, json.dumps(current_data), ex=86400*30)
        except Exception as e:
            logger.error(f"Error updating user data: {e}")
    
    def create_order(self, order_data: Dict) -> str:
        try:
            order_id = SecurityManager.generate_order_id()
            order_data["order_id"] = order_id
            order_data["created_at"] = datetime.now().isoformat()
            order_data["status"] = OrderStatus.PENDING.value
            
            key = f"order:{order_id}"
            self.redis_client.set(key, json.dumps(order_data), ex=86400*7)
            
            user_orders_key = f"user_orders:{order_data['user_id']}"
            self.redis_client.lpush(user_orders_key, order_id)
            self.redis_client.ltrim(user_orders_key, 0, 99)
            
            return order_id
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            raise
    
    def get_order(self, order_id: str) -> Optional[Dict]:
        try:
            key = f"order:{order_id}"
            data = self.redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"Error getting order: {e}")
            return None
    
    def update_order(self, order_id: str, updates: Dict):
        try:
            order = self.get_order(order_id)
            if order:
                order.update(updates)
                key = f"order:{order_id}"
                self.redis_client.set(key, json.dumps(order), ex=86400*7)
        except Exception as e:
            logger.error(f"Error updating order: {e}")
    
    def get_pending_orders(self) -> List[Dict]:
        try:
            pending_orders = []
            for key in self.redis_client.scan_iter("order:*"):
                order_data = self.redis_client.get(key)
                if order_data:
                    order = json.loads(order_data)
                    if order.get("status") == OrderStatus.PAID.value:
                        pending_orders.append(order)
            return sorted(pending_orders, key=lambda x: x["created_at"])
        except Exception as e:
            logger.error(f"Error getting pending orders: {e}")
            return []
    
    def get_all_users(self) -> List[Dict]:
        try:
            users = []
            for key in self.redis_client.scan_iter("user:*"):
                user_data = self.redis_client.get(key)
                if user_data:
                    users.append(json.loads(user_data))
            return users
        except Exception as e:
            logger.error(f"Error getting all users: {e}")
            return []

class NotificationManager:
    def _init_(self, bot_instance):
        self.bot = bot_instance
    
    def send_admin_notification(self, message: str, order_data: Dict = None):
        try:
            if order_data:
                message += f"\n\n📦 Заказ: #{order_data.get('order_id', 'N/A')}"
            
            self.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🔔 {message}",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Error sending admin notification: {e}")
    
    def send_user_notification(self, user_id: int, message: str, parse_mode='HTML'):
        try:
            user_data = db.get_user_data(user_id)
            if user_data.get("notifications", True):
                self.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode=parse_mode
                )
        except Exception as e:
            logger.error(f"Error sending user notification: {e}")

class AnalyticsManager:
    def _init_(self):
        self.db = DatabaseManager()
    
    def get_bot_statistics(self) -> Dict:
        try:
            users = self.db.get_all_users()
            total_revenue = sum(user.get('total_spent', 0) for user in users)
            active_users = len([u for u in users if datetime.fromisoformat(u.get('last_activity', '2000-01-01')) > datetime.now() - timedelta(days=30)])
            
            return {
                "total_users": len(users),
                "active_users": active_users,
                "total_revenue": total_revenue,
                "total_orders": sum(user.get('orders_count', 0) for user in users),
                "avg_order_value": total_revenue / len(users) if users else 0
            }
        except Exception as e:
            logger.error(f"Error getting statistics: {e}")
            return {}

class NotificationManager:
    def _init_(self, bot_instance):
        self.bot = bot_instance
    
    def send_admin_notification(self, message: str, order_data: Dict = None):
        try:
            if order_data:
                message += f"\n\n📦 Заказ: #{order_data.get('order_id', 'N/A')}"
            
            self.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🔔 {message}",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Error sending admin notification: {e}")
    
    def send_user_notification(self, user_id: int, message: str, parse_mode='HTML'):
        try:
            user_data = db.get_user_data(user_id)
            if user_data.get("notifications", True):
                self.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode=parse_mode
                )
        except Exception as e:
            logger.error(f"Error sending user notification: {e}")

# Инициализация менеджеров
db = DatabaseManager()
notification_manager = NotificationManager(bot)
analytics = AnalyticsManager()

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
            [KeyboardButton("👥 Пользователи"), KeyboardButton("⚙ Настройки")],
            [KeyboardButton("🎯 Акции"), KeyboardButton("🔔 Уведомления")]
        ]
    else:
        keyboard = [
            [KeyboardButton("🛒 Купить Stars"), KeyboardButton("👤 Профиль")],
            [KeyboardButton("🎁 Обмен очков"), KeyboardButton("🆘 Поддержка")],
            [KeyboardButton("📢 Акции"), KeyboardButton("⚙ Настройки")]
        ]
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, persistent=True)
    
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
    user_states[message.from_user.id] = UserState.REQUEST_USERNAME

@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_package_selection(call):
    selected_package = TELEGRAM_STARS_PACKAGES.get(call.data)
    
    if selected_package:
        user_states[call.from_user.id] = {
            'state': UserState.REQUEST_USERNAME,
            'current_order': selected_package
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

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('state') == UserState.REQUEST_USERNAME)
def handle_telegram_username(message):
    telegram_username = message.text.strip()
    
    if not SecurityManager.validate_user_input(telegram_username):
        bot.send_message(message.chat.id, "❌ Некорректный username. Попробуйте еще раз:")
        return
    
    telegram_username = telegram_username.replace('@', '')
    user_state = user_states[message.from_user.id]
    order = user_state['current_order']
    user_state['telegram_username'] = telegram_username
    user_state['state'] = UserState.WAITING_PAYMENT_PROOF
    
    payment_info = (
        f"✅ <b>Заказ создан!</b>\n\n"
        f"• ⭐ Stars: {order['amount']}\n"
        f"• 💰 Сумма: {order['price']} руб.\n"
        f"• 👤 Ваш Telegram: @{telegram_username}\n"
        f"• 🎁 Очков: {order['points']}\n\n"
        f"💳 <b>Реквизиты для оплаты:</b>\n"
        f"<code>2202 2002 2020 2020</code> - СБЕРБАНК\n"
        f"<code>5536 9137 1234 5678</code> - ТИНЬКОФФ\n\n"
        f"📸 <b>После оплаты прикрепите скриншот чека</b>\n"
        f"⚡ <b>Доставка:</b> 1-6 часов после проверки"
    )
    
    bot.send_message(message.chat.id, payment_info, parse_mode='HTML')
    
    # Уведомление администратору
    user = message.from_user
    admin_msg = (
        f"🛎 <b>НОВЫЙ ЗАКАЗ</b>\n"
        f"• 👤 Пользователь: @{user.username or 'N/A'} ({user.first_name})\n"
        f"• 🆔 ID: {user.id}\n"
        f"• ⭐ Stars: {order['amount']}\n"
        f"• 💰 Сумма: {order['price']} руб.\n"
        f"• 📧 Telegram для отправки: @{telegram_username}"
    )
    
    notification_manager.send_admin_notification(admin_msg)

@bot.message_handler(content_types=['photo', 'document'], 
                    func=lambda message: user_states.get(message.from_user.id, {}).get('state') == UserState.WAITING_PAYMENT_PROOF)
def handle_payment_screenshot(message):
    user_id = message.from_user.id
    user_state = user_states.get(user_id, {})
    order_data = user_state.get('current_order')
    telegram_username = user_state.get('telegram_username')
    
    try:
        # Создаем заказ в базе
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
        
        # Отправляем подтверждение пользователю
        user_msg = (
            f"📸 <b>Скриншот получен!</b>\n\n"
            f"🆔 <b>Номер заказа:</b> #{order_id}\n"
            f"⏱ <b>Статус:</b> Ожидает проверки\n"
            f"🚚 <b>Доставка:</b> 1-6 часов\n\n"
            f"Мы уведомим вас о смене статуса заказа."
        )
        
        bot.send_message(message.chat.id, user_msg, parse_mode='HTML')
        
        # Отправляем уведомление администратору
        admin_msg = (
            f"💰 <b>ПОЛУЧЕНА ОПЛАТА</b>\n"
            f"• 🆔 Заказ: #{order_id}\n"
            f"• 👤 Пользователь: @{message.from_user.username or 'N/A'}\n"
            f"• ⭐ Stars: {order_data['amount']}\n"
            f"• 💰 Сумма: {order_data['price']} руб.\n"
            f"• 📧 Telegram: @{telegram_username}"
        )
        
        notification_manager.send_admin_notification(admin_msg)
        
        # Пересылаем скриншот администратору
        if message.photo:
            bot.send_photo(
                ADMIN_CHAT_ID,
                message.photo[-1].file_id,
                caption=f"📸 Скриншот от @{message.from_user.username or 'N/A'} | Заказ #{order_id}"
            )
        elif message.document:
            bot.send_document(
                ADMIN_CHAT_ID,
                message.document.file_id,
                caption=f"📸 Скриншот от @{message.from_user.username or 'N/A'} | Заказ #{order_id}"
            )
        
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

@bot.message_handler(func=lambda message: message.text == "📊 Статистика" and get_user_role(message.from_user.id) == UserRole.ADMIN)
def show_admin_panel(message):
    stats = analytics.get_bot_statistics()
    
    admin_text = (
        f"🛠 <b>Панель администратора</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• 👥 Всего пользователей: {stats['total_users']}\n"
        f"• 🔥 Активных пользователей: {stats['active_users']}\n"
        f"• 💰 Общая выручка: {stats['total_revenue']} руб.\n"
        f"• 📦 Всего заказов: {stats['total_orders']}\n"
        f"• 📊 Средний чек: {stats['avg_order_value']:.2f} руб.\n\n"
        f"Выберите действие:"
    )
    
    keyboard = [
        [KeyboardButton("📦 Заказы"), KeyboardButton("👥 Пользователи")],
        [KeyboardButton("📊 Детальная статистика"), KeyboardButton("🎯 Рассылка")],
        [KeyboardButton("⚙ Настройки бота")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    bot.send_message(message.chat.id, admin_text, reply_markup=reply_markup, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text == "📦 Заказы" and get_user_role(message.from_user.id) == UserRole.ADMIN)
def show_pending_orders(message):
    orders = db.get_pending_orders()
    
    if not orders:
        bot.send_message(message.chat.id, "📦 Нет заказов, ожидающих обработки")
        return
    
    for order in orders[-5:]:
        order_text = (
            f"🆔 <b>Заказ:</b> #{order['order_id']}\n"
            f"👤 <b>Пользователь:</b> @{order['username']} (ID: {order['user_id']})\n"
            f"⭐ <b>Stars:</b> {order['stars_amount']}\n"
            f"💰 <b>Сумма:</b> {order['price']} руб.\n"
            f"📧 <b>Telegram:</b> @{order['telegram_username']}\n"
            f"🕐 <b>Создан:</b> {order['created_at'][:16]}"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{order['order_id']}"),
                InlineKeyboardButton("❌ Ошибка", callback_data=f"error_{order['order_id']}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        bot.send_message(message.chat.id, order_text, reply_markup=reply_markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith(('confirm_', 'error_')) and get_user_role(call.from_user.id) == UserRole.ADMIN)
def handle_admin_actions(call):
    action, order_id = call.data.split('_', 1)
    order = db.get_order(order_id)
    
    if not order:
        bot.answer_callback_query(call.id, "❌ Заказ не найден!")
        return
    
    if action == "confirm":
        user_id = order['user_id']
        stars = order['stars_amount']
        price = order['price']
        
        user_data = db.get_user_data(user_id)
        package_points = next(
            (pkg['points'] for pkg in TELEGRAM_STARS_PACKAGES.values() 
             if pkg['amount'] == stars and pkg['price'] == price),
            0
        )
        
        db.update_user_data(user_id, {
            "total_stars": user_data.get('total_stars', 0) + stars,
            "total_spent": user_data.get('total_spent', 0) + price,
            "points": user_data.get('points', 0) + package_points,
            "orders_count": user_data.get('orders_count', 0) + 1
        })
        
        db.update_order(order_id, {
            "status": OrderStatus.COMPLETED.value,
            "completed_at": datetime.now().isoformat()
        })
        
        user_msg = (
            f"🎉 <b>Ваш заказ #{order_id} выполнен!</b>\n\n"
            f"• ✅ Получено Stars: {stars}\n"
            f"• 🎁 Начислено очков: {package_points}\n"
            f"• ⭐ Всего Stars: {user_data.get('total_stars', 0) + stars}\n"
            f"• 🎯 Всего очков: {user_data.get('points', 0) + package_points}\n\n"
            f"Спасибо за покупку! 🎊"
        )
        
        notification_manager.send_user_notification(user_id, user_msg)
        bot.answer_callback_query(call.id, f"✅ Заказ #{order_id} подтвержден!")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        
    elif action == "error":
        user_id = order['user_id']
        db.update_order(order_id, {"status": OrderStatus.PAYMENT_ERROR.value})
        
        error_msg = (
            f"❌ <b>Проблема с заказом #{order_id}</b>\n\n"
            f"Обнаружена ошибка при проверке оплаты.\n"
            f"Пожалуйста, свяжитесь с поддержкой: {SUPPORT_USERNAME}"
        )
        
        notification_manager.send_user_notification(user_id, error_msg)
        bot.answer_callback_query(call.id, f"⚠ Пользователь уведомлен об ошибке")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

# Дополнительные обработчики
@bot.message_handler(func=lambda message: message.text == "🎁 Обмен очков")
def show_points_rewards(message):
    bot.send_message(message.chat.id, "🔄 Функция обмена очков скоро будет доступна!")

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

@bot.message_handler(commands=['cancel'])
def cancel_conversation(message):
    user_id = message.from_user.id
    if user_id in user_states:
        user_states.pop(user_id)
    bot.send_message(message.chat.id, "❌ Диалог отменен.")

# Запуск бота
if _name_ == '_main_':
    print("Бот запускается...")
    bot.infinity_polling()
