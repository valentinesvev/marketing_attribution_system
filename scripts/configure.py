"""Интерактивная настройка без передачи токена через аргументы команд."""
import getpass
import os
from pathlib import Path
import re
import secrets
import tempfile

from dotenv import dotenv_values, set_key

ENV = Path(__file__).resolve().parents[1] / "src" / ".env"


def ask(label, default, pattern, secret=False):
    while True:
        hint = " [Enter — сохранить]" if secret and default else (f" [{default}]" if default else "")
        read = getpass.getpass if secret else input
        value = read(label + hint + ": ").strip() or default
        if re.fullmatch(pattern, value):
            return value
        print("Неверный формат. Проверьте значение и повторите ввод.")


def main():
    current = dotenv_values(ENV) if ENV.exists() else {}
    print("Настройки будут записаны в", ENV)
    print("Токен не отображается при вводе. Enter сохраняет прежнее значение.")
    values = {}
    values["BOT_TOKEN"] = ask("Токен от @BotFather", current.get("BOT_TOKEN") or "", r"\d+:[A-Za-z0-9_-]+", True)
    values["ADMIN_IDS"] = ask("ID администраторов через запятую", current.get("ADMIN_IDS") or "", r"[1-9]\d*(,[1-9]\d*)*")
    own = values["ADMIN_IDS"].split(",")[0]
    values["MANAGER_IDS"] = ask("ID менеджеров через запятую", current.get("MANAGER_IDS") or own, r"[1-9]\d*(,[1-9]\d*)*")
    values["MANAGER_CHAT_ID"] = ask("ID чата для заявок (для проверки — ваш ID)", current.get("MANAGER_CHAT_ID") or own, r"-?[1-9]\d*")
    values["MANAGER_USERNAME"] = ask("Username менеджеров без @, через запятую", current.get("MANAGER_USERNAME") or "", r"[A-Za-z0-9_]+(,[A-Za-z0-9_]+)*")
    salt = current.get("HASH_SALT")
    if salt in ("change-me", "change-me-to-random-string"):
        raise SystemExit("В существующем .env стоит соль-пример. Задайте HASH_SALT вручную; изменение соли у заполненной базы нарушает связь пользователей.")
    values["HASH_SALT"] = salt or secrets.token_hex(32)
    values["ACQUIRING_RATE"] = current.get("ACQUIRING_RATE") or "0"
    values["ATTRIBUTION_WINDOW_DAYS"] = current.get("ATTRIBUTION_WINDOW_DAYS") or "30"
    values["DB_PATH"] = current.get("DB_PATH") or "data/bot.db"
    # Сохраняем дополнительные настройки и комментарии; заменяем файл атомарно.
    fd, name = tempfile.mkstemp(prefix=".settings-", dir=ENV.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as out:
            if ENV.exists():
                out.write(ENV.read_text())
        for key, value in values.items():
            set_key(str(tmp), key, value, quote_mode="always")
        tmp.chmod(0o600)
        tmp.replace(ENV)
    finally:
        tmp.unlink(missing_ok=True)
    print("Готово. Файл доступен только владельцу. Запуск: bash scripts/start.sh")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\nНастройка отменена; файл не изменён.")
