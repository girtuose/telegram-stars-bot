import logging
import json
import os
import random
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from enum import Enum
import aiofiles
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    JobQueue
)
from telegram.error import TelegramError
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
logger = logging.getLogger(_name_)

# Конфигурация (должна быть в .env)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7373633619:AAG1whl3hRIk3Obq2auPASIeBESSscyefxc")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "8104814490")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@Fluuuuuuuuuu")

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

# Состояния разговора
REQUEST_USERNAME, WAITING_PAYMENT_PROOF, WAITING_ADMIN_MESSAGE = range(3)

class SecurityManager:
    @staticmethod
    def validate_user_input(text: str, max_length: int = 100) -> bool:
        """Проверка пользовательского ввода"""
        if not text or len(text) > max_length:
            return False
        # Базовые проверки на инъекции
        dangerous_patterns = ['<script>', '../', ';', '--', '/', '/']
        return not any(pattern in text.lower() for pattern in dangerous_patterns)
    
    @staticmethod
    def generate_order_id() -> str:
        """Генерация безопасного ID заказа"""
        timestamp = int(datetime.now().timestamp())
        random_part = random.randint(1000, 9999)
        return f"ORD{timestamp}{random_part}"

class DatabaseManager:
    def _init_(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    
    async def get_user_data(self, user_id: int) -> Dict:
        """Получение данных пользователя"""
        try:
            key = f"user:{user_id}"
            data = self.redis_client.get(key)
            if data:
                return json.loads(data)
            
            # Данные по умолчанию
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
            await self.update_user_data(user_id, default_data)
            return default_data
        except Exception as e:
            logger.error(f"Error getting user data: {e}")
            return {}
    
    async def update_user_data(self, user_id: int, updates: Dict):
        """Обновление данных пользователя"""
        try:
            key = f"user:{user_id}"
            current_data = await self.get_user_data(user_id)
            current_data.update(updates)
            current_data["last_activity"] = datetime.now().isoformat()
            self.redis_client.set(key, json.dumps(current_data), ex=86400*30)  # 30 дней
        except Exception as e:
            logger.error(f"Error updating user data: {e}")
    
    async def create_order(self, order_data: Dict) -> str:
        """Создание нового заказа"""
        try:
            order_id = SecurityManager.generate_order_id()
            order_data["order_id"] = order_id
            order_data["created_at"] = datetime.now().isoformat()
            order_data["status"] = OrderStatus.PENDING.value
            
            key = f"order:{order_id}"
            self.redis_client.set(key, json.dumps(order_data), ex=86400*7)  # 7 дней
            
            # Добавляем в список заказов пользователя
            user_orders_key = f"user_orders:{order_data['user_id']}"
            self.redis_client.lpush(user_orders_key, order_id)
            self.redis_client.ltrim(user_orders_key, 0, 99)  # Храним последние 100 заказов
            
            return order_id
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            raise
    
    async def get_order(self, order_id: str) -> Optional[Dict]:
        """Получение заказа по ID"""
        try:
            key = f"order:{order_id}"
            data = self.redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"Error getting order: {e}")
            return None
    
    async def update_order(self, order_id: str, updates: Dict):
        """Обновление заказа"""
        try:
            order = await self.get_order(order_id)
            if order:
                order.update(updates)
                key = f"order:{order_id}"
                self.redis_client.set(key, json.dumps(order), ex=86400*7)
        except Exception as e:
            logger.error(f"Error updating order: {e}")
    
    async def get_pending_orders(self) -> List[Dict]:
        """Получение ожидающих заказов"""
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
    
    async def get_all_users(self) -> List[Dict]:
        """Получение всех пользователей"""
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
    def _init_(self, bot):
        self.bot = bot
    
    async def send_admin_notification(self, message: str, order_data: Dict = None):
        """Отправка уведомления администратору"""
        try:
            if order_data:
                message += f"\n\n📦 Заказ: #{order_data.get('order_id', 'N/A')}"
            
            await self.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🔔 {message}",
                parse_mode='HTML'
            )
        except TelegramError as e:
            logger.error(f"Error sending admin notification: {e}")
    
    async def send_user_notification(self, user_id: int, message: str, parse_mode='HTML'):
        """Отправка уведомления пользователю"""
        try:
            user_data = await db.get_user_data(user_id)
            if user_data.get("notifications", True):
                await self.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode=parse_mode
                )
        except TelegramError as e:
            logger.error(f"Error sending user notification: {e}")

class AnalyticsManager:
    def _init_(self):
        self.db = DatabaseManager()
    
    async def get_bot_statistics(self) -> Dict:
        """Получение статистики бота"""
        try:
            users = await self.db.get_all_users()
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

# Инициализация менеджеров
db = DatabaseManager()
notification_manager = None
analytics = AnalyticsManager()

def get_user_role(user_id: int) -> UserRole:
    """Получение роли пользователя"""
    return UserRole.ADMIN if str(user_id) == ADMIN_CHAT_ID else UserRole.USER

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    user_role = get_user_role(user_id)
    
    # Обновляем данные пользователя
    await db.update_user_data(user_id, {
        "username": update.effective_user.username or "",
        "first_name": update.effective_user.first_name or ""
    })
    
    # Создаем клавиатуру в зависимости от роли
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
        f"🌟 Добро пожаловать, {update.effective_user.first_name}!\n\n"
        "⚡ <b>Telegram Stars Bot</b> - быстрая и надежная покупка Stars\n\n"
        "✅ <b>Преимущества:</b>\n"
        "• 🚀 Доставка: 1-6 часов\n"
        "• 🎁 Бонусная система\n"
        "• 💎 Гарантия доставки\n"
        "• 🔒 Безопасные платежи\n\n"
        "Выберите действие ниже 👇"
    )
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')

async def show_stars_packages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать пакеты Stars"""
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
    
    await update.message.reply_text(info_text, reply_markup=reply_markup, parse_mode='HTML')

async def handle_package_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка выбора пакета"""
    query = update.callback_query
    await query.answer()
    
    selected_package = TELEGRAM_STARS_PACKAGES.get(query.data)
    
    if selected_package:
        context.user_data['current_order'] = selected_package
        
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
        
        await query.edit_message_text(order_text, parse_mode='HTML')
        return REQUEST_USERNAME
    
    await query.edit_message_text("❌ Произошла ошибка. Пожалуйста, начните заново")
    return ConversationHandler.END

async def handle_telegram_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка username пользователя"""
    telegram_username = update.message.text.strip()
    
    if not SecurityManager.validate_user_input(telegram_username):
        await update.message.reply_text("❌ Некорректный username. Попробуйте еще раз:")
        return REQUEST_USERNAME
    
    telegram_username = telegram_username.replace('@', '')
    context.user_data['telegram_username'] = telegram_username
    order = context.user_data.get('current_order')
    
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
    
    await update.message.reply_text(payment_info, parse_mode='HTML')
    
    # Уведомление администратору
    user = update.effective_user
    admin_msg = (
        f"🛎 <b>НОВЫЙ ЗАКАЗ</b>\n"
        f"• 👤 Пользователь: @{user.username or 'N/A'} ({user.first_name})\n"
        f"• 🆔 ID: {user.id}\n"
        f"• ⭐ Stars: {order['amount']}\n"
        f"• 💰 Сумма: {order['price']} руб.\n"
        f"• 📧 Telegram для отправки: @{telegram_username}"
    )
    
    await notification_manager.send_admin_notification(admin_msg)
    
    return WAITING_PAYMENT_PROOF

async def handle_payment_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка скриншота оплаты"""
    user_id = update.effective_user.id
    order_data = context.user_data.get('current_order')
    telegram_username = context.user_data.get('telegram_username')
    
    if not (update.message.photo or update.message.document):
        await update.message.reply_text("📸 Пожалуйста, прикрепите скриншот чека (фото или файл)")
        return WAITING_PAYMENT_PROOF
    
    try:
        # Создаем заказ в базе
        order_info = {
            'user_id': user_id,
            'username': update.effective_user.username or '',
            'first_name': update.effective_user.first_name or '',
            'telegram_username': telegram_username,
            'stars_amount': order_data['amount'],
            'price': order_data['price'],
            'points': order_data['points'],
        }
        
        order_id = await db.create_order(order_info)
        
        # Отправляем подтверждение пользователю
        user_msg = (
            f"📸 <b>Скриншот получен!</b>\n\n"
            f"🆔 <b>Номер заказа:</b> #{order_id}\n"
            f"⏱ <b>Статус:</b> Ожидает проверки\n"
            f"🚚 <b>Доставка:</b> 1-6 часов\n\n"
            f"Мы уведомим вас о смене статуса заказа."
        )
        
        await update.message.reply_text(user_msg, parse_mode='HTML')
        
        # Отправляем уведомление администратору
        admin_msg = (
            f"💰 <b>ПОЛУЧЕНА ОПЛАТА</b>\n"
            f"• 🆔 Заказ: #{order_id}\n"
            f"• 👤 Пользователь: @{update.effective_user.username or 'N/A'}\n"
            f"• ⭐ Stars: {order_data['amount']}\n"
            f"• 💰 Сумма: {order_data['price']} руб.\n"
            f"• 📧 Telegram: @{telegram_username}"
        )
        
        await notification_manager.send_admin_notification(admin_msg)
        
        # Пересылаем скриншот администратору
        if update.message.photo:
            await context.bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=update.message.photo[-1].file_id,
                caption=f"📸 Скриншот от @{update.effective_user.username or 'N/A'} | Заказ #{order_id}"
            )
        elif update.message.document:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=update.message.document.file_id,
                caption=f"📸 Скриншот от @{update.effective_user.username or 'N/A'} | Заказ #{order_id}"
            )
        
        # Очищаем данные сессии
        context.user_data.clear()
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Error processing payment: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке заказа. Попробуйте еще раз.")
        return ConversationHandler.END

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать профиль пользователя"""
    user_id = update.effective_user.id
    user_data = await db.get_user_data(user_id)
    
    # Рассчитываем уровень пользователя
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
    
    await update.message.reply_text(profile_text, parse_mode='HTML')

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Панель администратора"""
    if get_user_role(update.effective_user.id) != UserRole.ADMIN:
        await update.message.reply_text("❌ Доступ запрещен!")
        return
    
    stats = await analytics.get_bot_statistics()
    
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
    
    await update.message.reply_text(admin_text, reply_markup=reply_markup, parse_mode='HTML')

async def show_pending_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать ожидающие заказы"""
    if get_user_role(update.effective_user.id) != UserRole.ADMIN:
        await update.message.reply_text("❌ Доступ запрещен!")
        return
    
    orders = await db.get_pending_orders()
    
    if not orders:
        await update.message.reply_text("📦 Нет заказов, ожидающих обработки")
        return
    
    for order in orders[-5:]:  # Последние 5 заказов
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
            ],
            [
                InlineKeyboardButton("💬 Написать", callback_data=f"message_{order['order_id']}"),
                InlineKeyboardButton("📋 Детали", callback_data=f"details_{order['order_id']}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(order_text, reply_markup=reply_markup, parse_mode='HTML')

async def handle_admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка действий администратора"""
    query = update.callback_query
    await query.answer()
    
    if get_user_role(update.effective_user.id) != UserRole.ADMIN:
        await query.edit_message_text("❌ Доступ запрещен!")
        return
    
    action, order_id = query.data.split('_', 1)
    order = await db.get_order(order_id)
    
    if not order:
        await query.edit_message_text("❌ Заказ не найден!")
        return
    
    if action == "confirm":
        # Подтверждение заказа
        user_id = order['user_id']
        stars = order['stars_amount']
        price = order['price']
        
        user_data = await db.get_user_data(user_id)
        package_points = next(
            (pkg['points'] for pkg in TELEGRAM_STARS_PACKAGES.values() 
             if pkg['amount'] == stars and pkg['price'] == price),
            0
        )
        
        # Обновляем данные пользователя
        await db.update_user_data(user_id, {
            "total_stars": user_data.get('total_stars', 0) + stars,
            "total_spent": user_data.get('total_spent', 0) + price,
            "points": user_data.get('points', 0) + package_points,
            "orders_count": user_data.get('orders_count', 0) + 1
        })
        
        # Обновляем статус заказа
        await db.update_order(order_id, {
            "status": OrderStatus.COMPLETED.value,
            "completed_at": datetime.now().isoformat()
        })
        
        # Уведомляем пользователя
        user_msg = (
            f"🎉 <b>Ваш заказ #{order_id} выполнен!</b>\n\n"
            f"• ✅ Получено Stars: {stars}\n"
            f"• 🎁 Начислено очков: {package_points}\n"
            f"• ⭐ Всего Stars: {user_data.get('total_stars', 0) + stars}\n"
            f"• 🎯 Всего очков: {user_data.get('points', 0) + package_points}\n\n"
            f"Спасибо за покупку! 🎊"
        )
        
        await notification_manager.send_user_notification(user_id, user_msg)
        await query.edit_message_text(f"✅ Заказ #{order_id} подтвержден!")
        
    elif action == "error":
        # Ошибка оплаты
        user_id = order['user_id']
        await db.update_order(order_id, {"status": OrderStatus.PAYMENT_ERROR.value})
        
        error_msg = (
            f"❌ <b>Проблема с заказом #{order_id}</b>\n\n"
            f"Обнаружена ошибка при проверке оплаты.\n"
            f"Пожалуйста, свяжитесь с поддержкой: {SUPPORT_USERNAME}"
        )
        
        await notification_manager.send_user_notification(user_id, error_msg)
        await query.edit_message_text(f"⚠ Пользователь уведомлен об ошибке заказа #{order_id}")

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмена текущего диалога"""
    context.user_data.clear()
    await update.message.reply_text("❌ Диалог отменен.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик ошибок"""
    logger.error(f"Exception while handling update: {context.error}")
    
    # Уведомление администратору об ошибке
    error_msg = (
        f"🚨 <b>Произошла ошибка в боте</b>\n\n"
        f"• Ошибка: {type(context.error)._name_}\n"
        f"• Сообщение: {str(context.error)}\n"
        f"• Update: {update.to_dict() if update else 'N/A'}"
    )
    
    try:
        await notification_manager.send_admin_notification(error_msg)
    except:
        pass  # Если не удалось отправить уведомление

async def scheduled_tasks(context: ContextTypes.DEFAULT_TYPE):
    """Плановые задачи"""
    try:
        # Очистка старых данных
        pass
    except Exception as e:
        logger.error(f"Error in scheduled tasks: {e}")

def main() -> None:
    """Основная функция запуска бота"""
    try:
        # Создаем Application
        application = Application.builder().token(TOKEN).build()
        
        # Инициализируем менеджер уведомлений
        global notification_manager
        notification_manager = NotificationManager(application.bot)
        
        # ConversationHandler для покупки
        buy_conversation = ConversationHandler(
            entry_points=[MessageHandler(filters.Text("🛒 Купить Stars"), show_stars_packages)],
            states={
                REQUEST_USERNAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_username)
                ],
                WAITING_PAYMENT_PROOF: [
                    MessageHandler(filters.PHOTO | filters.Document.ALL, handle_payment_screenshot)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            name="buy_conversation"
        )
        
        # Базовые обработчики
        application.add_handler(CommandHandler("start", start))
        application.add_handler(buy_conversation)
        application.add_handler(CallbackQueryHandler(handle_package_selection, pattern="^buy_"))
        
        # Обработчики профиля
        application.add_handler(MessageHandler(filters.Text("👤 Профиль"), show_profile))
        application.add_handler(MessageHandler(filters.Text("🎁 Обмен очков"), show_points_rewards))
        
        # Админ-обработчики
        application.add_handler(MessageHandler(filters.Text("📊 Статистика"), show_admin_panel))
        application.add_handler(MessageHandler(filters.Text("📦 Заказы"), show_pending_orders))
        application.add_handler(CallbackQueryHandler(handle_admin_actions, pattern="^(confirm|error)_"))
        
        # Обработчик ошибок
        application.add_error_handler(error_handler)
        
        # Планировщик задач
        job_queue = application.job_queue
        if job_queue:
            job_queue.run_repeating(scheduled_tasks, interval=3600, first=10)  # Каждый час
        
        logger.info("Бот запущен успешно")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}")
        raise

if _name_ == '_main_':
    main()
