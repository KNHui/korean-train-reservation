"""KTX station choices, including Suseo departures; not a live timetable."""

# Keep the spelling already used by the Korail client. Do not strip arbitrary
# parentheses or station suffixes: that could merge distinct stations.
STATION_ALIASES = {"김천(구미)": "김천구미"}


def normalize_station(name: str) -> str:
    name = name.strip()
    return STATION_ALIASES.get(name, name)


def merge_stations(*groups) -> tuple[str, ...]:
    """Normalize aliases and remove duplicates, preserving first-seen order."""
    return tuple(dict.fromkeys(
        normalized
        for group in groups
        for name in group
        if (normalized := normalize_station(name))
    ))


# Precomposed Hangul syllables sort in Korean dictionary (가나다) order.
STATIONS = ('강릉', '경산', '경주', '곡성', '공주', '광명', '광주송정', '구례구', '구포', '김천구미', '나주', '남원', '논산', '대전', '동대구', '동탄', '마산', '목포', '밀양', '부산', '서대구', '서대전', '서울', '수서', '수원', '순천', '여수EXPO', '여천', '영등포', '오송', '용산', '울산(통도사)', '익산', '전주', '정동진', '정읍', '진영', '진주', '창원', '창원중앙', '천안아산', '청량리', '평택지제', '포항', '행신')
DEFAULT_STATIONS = ("서울", "수서", "대전", "동대구", "부산")


if __name__ == "__main__":
    print("\n".join(STATIONS))
