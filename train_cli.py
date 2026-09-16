"""KTX reservation menu and settings."""

import argparse
import importlib

from stations import DEFAULT_STATIONS, STATIONS, merge_stations


PASSENGER_OPTIONS = (
    ("어린이", "child"),
    ("경로우대", "senior"),
    ("중증장애인", "disability1to3"),
    ("경증장애인", "disability4to6"),
)


def require_ktx(rail_type):
    if rail_type != "KTX":
        raise ValueError("KTX만 지원합니다.")


def configure_options(app):
    """Use Korail preferences and always search the integrated KTX group."""
    def get_options():
        saved = app.keyring.get_password("KTX", "options")
        selected = (saved or "").split(",")
        return [key for _, key in PASSENGER_OPTIONS if key in selected] + ["ktx"]

    def set_options():
        result = app.inquirer.prompt([
            app.inquirer.Checkbox(
                "options",
                message="승객 유형 선택 (Space: 선택, Enter: 완료, Ctrl-C: 취소)",
                choices=list(PASSENGER_OPTIONS),
                default=[key for key in get_options() if key != "ktx"],
            )
        ])
        if result is None:
            return
        selected = result.get("options", [])
        app.keyring.set_password(
            "KTX", "options",
            ",".join(key for _, key in PASSENGER_OPTIONS if key in selected),
        )

    app.get_options = get_options
    app.set_options = set_options


def configure_login(app):
    """Save credentials only after a bounded, verified login succeeds."""
    from reservation_guard import (
        Adapter, Audit, Budget, Policy, RUNTIME, StopRun, single_instance,
        create_client, close_client,
    )

    def set_login(rail_type="KTX", debug=False):
        require_ktx(rail_type)
        saved_id = app.keyring.get_password(rail_type, "id") or ""
        saved_password = app.keyring.get_password(rail_type, "pass") or ""
        result = app.inquirer.prompt([
            app.inquirer.Text("id", message=f"{rail_type} 계정 아이디", default=saved_id),
            app.inquirer.Password("pass", message=f"{rail_type} 계정 비밀번호", default=saved_password),
        ])
        if result is None:
            return False
        user, password = result["id"].strip(), result["pass"]
        if not user or not password:
            print("아이디와 비밀번호를 입력하세요.")
            return False
        try:
            with single_instance(RUNTIME / "reservation.lock"):
                audit = Audit(RUNTIME / "events.jsonl")
                client = create_client(rail_type, user, password)
                try:
                    budget = Budget(Policy(max_seconds=45, max_requests=4), audit)
                    budget.attach(client)
                    Adapter(client, rail_type, audit=audit).login()
                finally:
                    close_client(client)
                app.keyring.set_password(rail_type, "id", user)
                app.keyring.set_password(rail_type, "pass", password)
                app.keyring.set_password(rail_type, "ok", "1")
            print("로그인을 확인하고 설정을 저장했습니다.")
            return True
        except StopRun as exc:
            print(f"로그인 설정 중단: {exc}")
        except Exception as exc:
            print(f"로그인 설정 중단 ({type(exc).__name__}). 인증·통신 또는 저장 상태를 확인하세요.")
        return False

    app.set_login = set_login


def configure_reservation_list(app):
    from reservation_guard import (
        Adapter, Audit, Budget, Policy, RUNTIME, StopRun, single_instance,
        create_client, close_client, credentials,
    )

    def check_reservation(rail_type="KTX", debug=False):
        require_ktx(rail_type)
        try:
            with single_instance(RUNTIME / "reservation.lock"):
                audit = Audit(RUNTIME / "events.jsonl")
                client = create_client(rail_type, *credentials(rail_type))
                try:
                    budget = Budget(Policy(max_seconds=60, max_requests=20), audit)
                    budget.attach(client)
                    adapter = Adapter(client, rail_type, audit=audit)
                    adapter.login()
                    reservations = adapter.reservations()
                    if not reservations:
                        print("현재 예약·승차권이 없습니다.")
                    for reservation in reservations:
                        print(reservation)
                    print("결제·취소와 구입기한 확인은 공식 앱에서 진행하세요.")
                finally:
                    close_client(client)
        except StopRun as exc:
            print(f"예약 확인 중단: {exc}")
        except Exception as exc:
            print(f"예약 확인 중단 ({type(exc).__name__}). 공식 앱에서 내역을 확인하세요.")

    app.check_reservation = check_reservation


def run_menu(app, debug=False):
    choices = [
        ("예매 시작", "reserve"),
        ("예약·승차권 확인", "reservations"),
        ("로그인 설정", "login"),
        ("텔레그램 설정", "telegram"),
        ("카드 설정", "card"),
        ("카드 정보 삭제", "clear_card"),
        ("역 설정", "stations"),
        ("역 직접 수정", "edit_stations"),
        ("승객 유형 설정", "passengers"),
        ("나가기", "exit"),
    ]
    actions = {
        "reserve": lambda: app.reserve("KTX", debug),
        "reservations": lambda: app.check_reservation("KTX", debug),
        "login": lambda: app.set_login("KTX", debug),
        "telegram": app.set_telegram,
        "card": app.set_card,
        "clear_card": app.clear_card,
        "stations": lambda: app.set_station("KTX"),
        "edit_stations": lambda: app.edit_station("KTX"),
        "passengers": app.set_options,
    }
    while True:
        choice = app.inquirer.list_input(
            message="메뉴 선택 (↕:이동, Enter: 선택)", choices=choices,
            carousel=True,
        )
        if choice in {None, "exit"}:
            return
        action = actions.get(choice)
        if action:
            action()


def configure_stations(app):
    """Load KTX favorites and preserve custom station names."""
    app.STATIONS = {"KTX": list(STATIONS)}
    app.DEFAULT_STATIONS = {"KTX": list(DEFAULT_STATIONS)}

    def get_station(rail_type="KTX"):
        require_ktx(rail_type)
        saved = app.keyring.get_password("KTX", "station")
        selected = merge_stations(saved.split(",")) if saved else ()
        selected = selected or DEFAULT_STATIONS
        # Preserve manually entered stations outside the old predefined lists.
        choices = sorted(merge_stations(STATIONS, selected))
        return list(choices), list(selected)

    app.get_station = get_station


def main(argv=None):
    parser = argparse.ArgumentParser(description="통합 기차 예매")
    parser.add_argument("--list-stations", action="store_true", help="로그인 없이 통합 역 목록 출력")
    parser.add_argument("--debug", action="store_true", help="디버그 모드")
    parser.add_argument("--reserve", action="store_true", help="조건에 맞는 열차 자동예약을 기본 선택")
    parser.add_argument("--pay", action="store_true", help="예매 메뉴의 자동 결제를 기본 선택")
    parser.add_argument("--train-number", default="", help="대상 열차 번호 (생략하면 시간대 내 모든 열차)")
    args = parser.parse_args(argv)
    if args.list_stations:
        print("\n".join(STATIONS))
        return

    app = importlib.import_module("cli_settings").create_app()
    from terminal_input import NavigationInquirer
    if not isinstance(app.inquirer, NavigationInquirer):
        app.inquirer = NavigationInquirer(app.inquirer)
    configure_stations(app)
    configure_options(app)
    configure_login(app)
    configure_reservation_list(app)
    configure_reservation(app, args.reserve, args.train_number, args.pay)
    run_menu(app, debug=args.debug)


def configure_reservation(app, reserve=False, train_number="", pay=False):
    """Use the project runner with queued waits and reservation safeguards."""
    from reservation_guard import Policy, main as guarded_main

    def reserve_menu(rail_type="KTX", debug=False):
        require_ktx(rail_type)
        from datetime import datetime
        _, selected = app.get_station(rail_type)
        options = app.get_options()
        questions = [
            app.inquirer.List("dep", message="출발역", choices=selected, default=selected[0]),
            app.inquirer.List("arr", message="도착역", choices=selected, default=selected[-1]),
            app.inquirer.Text("date", message="운행일 YYYYMMDD", default=datetime.now().strftime("%Y%m%d")),
            app.inquirer.Text("time", message="출발 시작 시각 HHMMSS", default="000000"),
            app.inquirer.Text("until", message="출발 종료 시각 HHMMSS", default="235959"),
            app.inquirer.Text("adults", message="성인 인원", default="1"),
            app.inquirer.List("seat", message="좌석 조건", choices=[
                ("일반실만", "general"), ("특실만", "special"),
                ("일반실 우선", "general-first"), ("특실 우선", "special-first")], default="general"),
            app.inquirer.Text("train-number", message="열차 번호 (선택, 비우면 시간대 내 모든 열차)", default=train_number),
            app.inquirer.Text("interval", message="최소 조회 대기 시간 (초, 15 이상)", default=str(Policy.interval)),
            app.inquirer.Text("max-interval", message="최대 조회 대기 시간 (초)", default=str(Policy.max_interval)),
        ]
        for label, key in PASSENGER_OPTIONS:
            if key in options:
                flag = {"child": "children", "senior": "seniors"}.get(key, key)
                questions.append(app.inquirer.Text(flag, message=f"{label} 인원", default="0"))
        questions.append(app.inquirer.Confirm(
            "reserve", message="조건에 맞는 열차 한 편을 자동예약", default=reserve,
        ))
        questions.append(app.inquirer.Confirm(
            "pay", message="예약 성공 시 저장된 카드로 즉시 결제 (실제로 청구됩니다)", default=pay,
        ))
        print("Enter: 다음 단계 · Esc: 이전 단계 (첫 단계에서는 메뉴로) · Ctrl+C: 취소")
        print("Home/End: 입력 처음·끝 또는 목록 처음·끝 · ↑/↓: 목록 순환 이동")
        result = app.inquirer.prompt(questions, allow_back=True)
        if result is None:
            return
        args = []
        for key in ("dep", "arr", "date", "time", "until", "adults", "children", "seniors",
                    "disability1to3", "disability4to6", "seat", "train-number", "interval", "max-interval"):
            if key in result:
                args += ["--" + key, str(result[key])]
        if result.get("reserve"):
            args += ["--reserve"]
            if result.get("pay"):
                args += ["--pay"]
        try:
            return guarded_main(args)
        except SystemExit as exc:
            # argparse rejects malformed interactive values without closing the menu.
            return exc.code

    app.reserve = reserve_menu


if __name__ == "__main__":
    main()
