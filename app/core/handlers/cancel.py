"""/cancel command handler — clears FSM state."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

router = Router(name="core.cancel")


@router.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current is None:
        await message.answer("Нет активного диалога.")
    else:
        await message.answer("Диалог отменён.")
