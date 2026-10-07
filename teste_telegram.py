import os
from pathlib import Path

import requests
from dotenv import load_dotenv


MENSAGEM = """TESTE RADAR TECH

Logitech G305 Wireless

De R$ 299 por R$ 199
33% OFF

Mouse wireless gamer com ótimo custo-benefício.

Radar Tech — a gente encontra, você economiza."""


def main():
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or token == "COLE_SEU_TOKEN_AQUI":
        print("Configure TELEGRAM_BOT_TOKEN no arquivo .env antes de testar.")
        return 1

    if not chat_id:
        print("Configure TELEGRAM_CHAT_ID no arquivo .env antes de testar.")
        return 1

    try:
        resposta = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": MENSAGEM},
            timeout=10,
        )
    except requests.exceptions.Timeout:
        print("Tempo esgotado ao conectar com o Telegram.")
        return 1
    except requests.exceptions.ConnectionError:
        print("Falha de conexão com o Telegram.")
        return 1
    except requests.exceptions.RequestException:
        print("Falha ao enviar mensagem ao Telegram.")
        return 1

    try:
        resultado = resposta.json()
    except ValueError:
        print(f"Resposta inválida do Telegram (HTTP {resposta.status_code}).")
        return 1

    if not isinstance(resultado, dict):
        print(f"Resposta inválida do Telegram (HTTP {resposta.status_code}).")
        return 1

    if resposta.ok and resultado.get("ok") is True:
        print("Mensagem de teste enviada com sucesso.")
        return 0

    codigo = resultado.get("error_code")
    if isinstance(codigo, int) and not isinstance(codigo, bool):
        print(f"Telegram recusou a mensagem (código {codigo}).")
    else:
        print(f"Telegram recusou a mensagem (HTTP {resposta.status_code}).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
