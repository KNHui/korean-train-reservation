"""Local settings UI using the existing OS keyring entries."""

from types import SimpleNamespace

import inquirer
import keyring

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
        fields = (("number", "카드 번호"), ("password", "카드 비밀번호 앞 2자리"),
                  ("birthday", "생년월일 YYMMDD / 사업자등록번호"), ("expire", "유효기간 YYMM"))
        result = app.inquirer.prompt([
            app.inquirer.Password(name, message=label,
                                  default=app.keyring.get_password("card", name) or "")
            for name, label in fields
        ])
        if not result:
            return False
        for name, _ in fields:
            app.keyring.set_password("card", name, result[name])
        app.keyring.set_password("card", "ok", "1")
        print("카드 설정을 저장했습니다. 결제는 공식 앱에서 진행하세요.")
        return True

    app.set_station = set_station
    app.edit_station = edit_station
    app.set_telegram = set_telegram
    app.set_card = set_card
    return app
