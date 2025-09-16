# payment.py
import json
import asyncio
from aiogram import Router, Bot, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    LabeledPrice,
    PreCheckoutQuery,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from src.config import (
    YOOKASSA_PAYMENT_TOKEN,
    EMAIL_FOR_BILL,
    SUBSCRIPTION_PRICES,
    SUBSCRIPTION_DURATION_OPTIONS
)
from src.logger import logger
from src.database import Redis, Database
from src.aiogram.middlewares import WaitingMiddleware
from src.aiogram.utils import vk_send_pixel_event
from src.filters import ChatTypeFilter
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

router = Router()
router.message.filter(ChatTypeFilter(chat_type=["private"]))
router.message.middleware(WaitingMiddleware())


def get_payment_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])

    # Добавляем кнопки выбора срока подписки
    for i in range(0, len(SUBSCRIPTION_DURATION_OPTIONS), 2):
        row = []
        for months in SUBSCRIPTION_DURATION_OPTIONS[i:i + 2]:
            price = SUBSCRIPTION_PRICES.get(months, 0)
            discount_text = ""

            # Рассчитываем скидку (если есть)
            if months > 1:
                monthly_price = price / months
                base_monthly_price = SUBSCRIPTION_PRICES.get(1, price)
                discount = int((1 - monthly_price / base_monthly_price) * 100)
                discount_text = f" (-{discount}%)" if discount > 0 else ""

            text = f"{months} мес. - {price}₽{discount_text}"
            row.append(InlineKeyboardButton(text=text, callback_data=f"months:{months}"))

        if row:
            keyboard.inline_keyboard.append(row)

    return keyboard


@router.callback_query(F.data.startswith("months:"))
async def select_months(callback: CallbackQuery, bot: Bot, db: Database):
    months = int(callback.data.split(":")[1])
    price = SUBSCRIPTION_PRICES.get(months, 0)

    await send_invoice(
        bot=bot,
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        full_name=callback.from_user.full_name,
        user_name=callback.from_user.username,
        months=months,
        price=price
    )
    await callback.answer()


@router.message(Command("pay"))
async def pay_handler(message: Message, bot: Bot, db: Database):
    user_id = message.from_user.id

    if await db.is_subscription_active(user_id):
        sub_expiration_date = await db.get_sub_expiration_date(
            telegram_id=user_id, user_tz="Europe/Moscow"
        )
        formatted_date = sub_expiration_date.strftime("%Y-%m-%d %H:%M:%S")

        text = "\n".join([
            "✅ *Вы уже подписаны\\!*\n",
            f"📅 *Окончание текущей подписки:* `{formatted_date} (МСК)`",
            "",
            "🔄 *Вы можете продлить подписку, выбрав срок ниже:*",
        ])

        await message.answer(
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_payment_keyboard()
        )
        return

    await message.answer(
        "Выберите срок подписки:",
        reply_markup=get_payment_keyboard()
    )


async def send_invoice(bot: Bot, chat_id: int, user_id: int, full_name: str,
                       user_name: str, months: int, price: int):
    description = f"Оплата подписки на {months} месяц(ев)"

    # Сохраняем информацию о выборе пользователя
    await bot.get_redis().setex(
        f"payment_info:{user_id}",
        300,
        json.dumps({"months": months, "price": price})
    )

    await bot.send_invoice(
        chat_id=chat_id,
        title="Оплата подписки",
        description=description,
        payload=f"subscription_{months}",
        start_parameter="payment",
        provider_token=YOOKASSA_PAYMENT_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=description, amount=price * 100)],
        provider_data=json.dumps({
            "receipt": {
                "customer": {
                    "full_name": f"{full_name} ({user_name})",
                    "email": EMAIL_FOR_BILL,
                },
                "items": [{
                    "description": description,
                    "quantity": 1,
                    "amount": {
                        "value": price,
                        "currency": "RUB"
                    },
                    "vat_code": 1,
                    "payment_mode": "full_payment",
                    "payment_subject": "commodity"
                }],
                "tax_system_code": 1
            }
        })
    )


@router.pre_checkout_query()
async def on_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)
    logger.debug(f"(PAYMENT)\t Payment confirmed")


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, db: Database, redis: Redis):
    # Получаем сохраненную информацию о платеже
    payment_info = await redis.get(f"payment_info:{message.from_user.id}")
    if payment_info:
        payment_info = json.loads(payment_info)
        months = payment_info["months"]
        price = payment_info["price"]

        # Добавляем подписку
        expiration_date = await db.add_subscription(
            telegram_id=message.from_user.id,
            months=months
        )

        # Добавляем запись о платеже
        await db.add_payment(
            telegram_id=message.from_user.id,
            telegram_username=message.from_user.username,
            currency=message.successful_payment.currency,
            total_amount=message.successful_payment.total_amount // 100,
            telegram_payment_charge_id=message.successful_payment.telegram_payment_charge_id,
            provider_payment_charge_id=message.successful_payment.provider_payment_charge_id,
            invoice_payload=message.successful_payment.invoice_payload,
            is_recurring=message.successful_payment.is_recurring,
            subscription_expiration_date=expiration_date,
            is_first_recurring=message.successful_payment.is_first_recurring,
            order_info=str(message.successful_payment.order_info) if message.successful_payment.order_info else None,
            months=months
        )

        await message.answer(
            f"✅ Подписка активирована до {expiration_date.strftime('%Y-%m-%d %H:%M')}\n"
            f"Срок: {months} месяцев\n"
            f"Сумма: {price}₽"
        )

        # Отправляем метрику
        asyncio.create_task(
            vk_send_pixel_event(
                redis=redis,
                user_id=message.from_user.id,
                goal_name="payment",
                cost=price
            )
        )
    else:
        await message.answer("Оплата прошла успешно, но возникла ошибка при активации подписки.")

    logger.debug(f"(PAYMENT)\t Successful payment")