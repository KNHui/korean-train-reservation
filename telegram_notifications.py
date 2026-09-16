"""Reservation notifications using Telegram settings from the OS keyring."""

import keyring
import requests


class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def __call__(self, reservation, paid=False):
        message = (
            f"[기차 예약] {'결제 완료' if paid else '예약 성공'}\n"
            f"{str(reservation)[:3500]}\n\n"
            + ("승차권은 공식 앱에서 확인하세요." if paid
               else "결제는 공식 앱에서 구입기한 내에 완료하세요.")
        )
        # One separate notification request, even if the railway budget is spent.
        # Do not retry ambiguous delivery or expose URLs containing the bot token.
        with requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": message},
            timeout=10,
            allow_redirects=False,
        ) as response:
            if response.status_code != 200 or response.json().get("ok") is not True:
                raise RuntimeError("Telegram notification rejected")


def load_notifier(emit=print):
    try:
        token = (keyring.get_password("telegram", "token") or "").strip()
        chat_id = (keyring.get_password("telegram", "chat_id") or "").strip()
    except Exception:
        emit("텔레그램 설정을 읽지 못했습니다. 예약 결과는 터미널에서 확인하세요.")
        return None
    if not token or not chat_id:
        emit("텔레그램 설정이 없어 성공 알림을 보내지 않습니다. 메뉴에서 텔레그램을 설정하세요.")
        return None
    emit("텔레그램 예약 성공 알림이 활성화되었습니다.")
    return TelegramNotifier(token, chat_id)
