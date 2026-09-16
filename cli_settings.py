"""Local settings UI using the existing OS keyring entries."""

from types import SimpleNamespace

import inquirer
import keyring

import payment
from stations import merge_stations


def create_app():
    app = SimpleNamespace(inquirer=inquirer, keyring=keyring)

    def save_stations(selected):
        selected = merge_stations(selected)
        if not selected:
            print("역을 하나 이상 선택하세요.")
            return False
        app.keyring.set_password("KTX", "station", ",".join(selected))
        return True

    def set_station(rail_type="KTX"):
        choices, selected = app.get_station(rail_type)
        result = app.inquirer.prompt([app.inquirer.Checkbox(
            "stations", message="역 선택 (Space: 선택, Enter: 완료)",
            choices=choices, default=selected,
        )])
        return bool(result) and save_stations(result["stations"])

    def edit_station(rail_type="KTX"):
        _, selected = app.get_station(rail_type)
        result = app.inquirer.prompt([app.inquirer.Text(
            "stations", message="역 수정 (쉼표로 구분)", default=",".join(selected),
        )])
        return bool(result) and save_stations(result["stations"].split(","))

    def set_telegram():
        result = app.inquirer.prompt([
            app.inquirer.Password("token", message="텔레그램 봇 토큰",
                                  default=app.keyring.get_password("telegram", "token") or ""),
            app.inquirer.Text("chat_id", message="텔레그램 채팅 ID",
                              default=app.keyring.get_password("telegram", "chat_id") or ""),
        ])
        if not result:
            return False
        if not result["token"].strip() or not result["chat_id"].strip():
            print("봇 토큰과 채팅 ID를 모두 입력하세요.")
            return False
        for name in ("token", "chat_id"):
            app.keyring.set_password("telegram", name, result[name].strip())
        print("텔레그램 설정을 저장했습니다. 예약 성공 시 알림을 보냅니다.")
        return True

    def set_card():
        result = app.inquirer.prompt([
            app.inquirer.Password(name, message=label,
                                  default=app.keyring.get_password(payment.SERVICE, name) or "")
            for name, label in payment.FIELDS
        ])
        if not result:
            return False
        values = {name: str(result[name]).strip() for name, _ in payment.FIELDS}
        try:
            # Reject an unusable card here, not at the moment money would move.
            payment.Card(**values)
        except ValueError as exc:
            print(exc)
            return False
        for name, _ in payment.FIELDS:
            app.keyring.set_password(payment.SERVICE, name, values[name])
        app.keyring.set_password(payment.SERVICE, "ok", "1")
        print("카드 설정을 저장했습니다. 자동 결제는 --pay 또는 예매 메뉴에서 선택할 때만 실행합니다.")
        return True

    def clear_card():
        for name in [name for name, _ in payment.FIELDS] + ["ok"]:
            try:
                app.keyring.delete_password(payment.SERVICE, name)
            except Exception:
                continue
        print("저장된 카드 정보를 삭제했습니다.")
        return True

    app.set_station = set_station
    app.edit_station = edit_station
    app.set_telegram = set_telegram
    app.set_card = set_card
    app.clear_card = clear_card
    return app
