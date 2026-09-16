"""Card value object and OS keyring storage for optional automatic payment.

Card fields are grouped into one value object so they cannot be passed in the
wrong order, and every field stays a string because leading zeros carry meaning
("0412" is not 412). The card is never printed, audited or written to the
attempt store; only the masked repr may reach a traceback.
"""

from dataclasses import dataclass

import keyring


SERVICE = "card"
FIELDS = (
    ("number", "카드 번호"),
    ("password", "카드 비밀번호 앞 2자리"),
    ("birthday", "생년월일 YYMMDD / 사업자등록번호"),
    ("expire", "유효기간 YYMM"),
)


@dataclass(frozen=True)
class Card:
    """A credit card used to settle one reservation."""

    number: str
    password: str
    birthday: str
    expire: str
    installment: int = 0

    def __post_init__(self):
        for name, label in FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label}을(를) 입력하세요.")
        if not self.number.isdigit() or not 15 <= len(self.number) <= 16:
            raise ValueError("카드 번호는 하이픈 없이 15~16자리 숫자입니다.")
        if not self.password.isdigit() or len(self.password) != 2:
            raise ValueError("카드 비밀번호는 앞 2자리 숫자입니다.")
        if not self.birthday.isdigit() or len(self.birthday) not in (6, 10):
            raise ValueError("생년월일 6자리 또는 사업자등록번호 10자리를 입력하세요.")
        if not self.expire.isdigit() or len(self.expire) != 4:
            raise ValueError("유효기간은 YYMM 4자리입니다.")
        if not 1 <= int(self.expire[2:]) <= 12:
            raise ValueError("유효기간의 월은 01~12입니다.")
        if not isinstance(self.installment, int) or isinstance(self.installment, bool):
            raise ValueError("할부 개월은 정수여야 합니다.")
        if self.installment < 0:
            raise ValueError("할부 개월은 0 이상이어야 합니다.")

    @property
    def corporate(self):
        """A ten digit owner number is a business registration number."""
        return len(self.birthday) == 10

    @property
    def auth_type(self):
        """Owner verification code: S for a corporate card, J for a personal one."""
        return "S" if self.corporate else "J"

    def __repr__(self):
        """Keep the last four digits only; a full number must never reach a log."""
        masked = f"****{self.number[-4:]}" if len(self.number) >= 4 else "****"
        return f"Card(number={masked!r}, installment={self.installment}, corporate={self.corporate})"


def load_card(emit=print):
    """Read the saved card, or None when no usable card is stored."""
    try:
        if keyring.get_password(SERVICE, "ok") != "1":
            emit("저장된 카드 정보가 없어 결제를 진행하지 않습니다. 메뉴에서 카드를 설정하세요.")
            return None
        values = {name: (keyring.get_password(SERVICE, name) or "").strip() for name, _ in FIELDS}
    except Exception:
        emit("카드 정보를 읽지 못했습니다. 결제는 공식 앱에서 진행하세요.")
        return None
    try:
        return Card(**values)
    except ValueError as exc:
        # The message names a field, never a stored value.
        emit(f"저장된 카드 정보를 사용할 수 없습니다: {exc}")
        return None
