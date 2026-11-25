"""기상청 단기예보(getVilageFcst)에서 특정 날짜의 예상 일강수량을 구하는 모듈.

- 인증키는 환경변수 DATA_GO_KR_API_KEY 에서만 읽습니다(.env 가 있으면 자동으로 불러옴).
- 인증키가 없으면 data/sample/forecast_sample.csv (저장해 둔 예보)를 대신 읽습니다. = 샘플 모드
  샘플은 날짜 대신 '오늘 기준 며칠 뒤(day_offset 1~3)'로 저장되어 있어 언제 실행해도 내일 ~ 3일 뒤가 됩니다.
- 요청 URL에는 키가 들어 있으므로, 오류가 나도 URL이나 requests 예외 원문을 밖으로 내보내지 않습니다.
"""

import csv
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).parent
# 이 파일과 같은 폴더의 .env 를 불러옵니다. (이미 설정된 환경변수는 덮어쓰지 않음)
load_dotenv(HERE / ".env")

ENDPOINT = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
NX, NY = 60, 127  # 서울 격자 좌표
KST = timezone(timedelta(hours=9))  # 한국 시간 (Windows에서 zoneinfo 대신 사용)
BASE_HOURS = [2, 5, 8, 11, 14, 17, 20, 23]  # 단기예보 발표 시각
SAMPLE_PATH = HERE / "data" / "sample" / "forecast_sample.csv"
PLACEHOLDER_KEY = "여기에_Decoding_키"  # .env.example 의 자리표시 글자
# 예보가 없거나 24시간이 안 될 때 보여 줄 문구 (요건정의서)
NOT_ENOUGH_DATA = "데이터가 부족해 강수량을 예측 할 수 없습니다. 나중에 다시 시도해주세요."


class ForecastError(Exception):
    """예보를 가져오지 못했을 때 쓰는 오류. status_code 는 API 서버가 돌려줄 상태 코드."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def now_kst() -> datetime:
    """지금 한국 시각."""
    return datetime.now(KST)


def has_key() -> bool:
    """진짜 인증키가 설정되어 있으면 True (비어 있거나 자리표시 글자 그대로면 False)."""
    key = (os.getenv("DATA_GO_KR_API_KEY") or "").strip()
    return bool(key) and key != PLACEHOLDER_KEY


def mode() -> str:
    """지금 동작 방식: 'api' (기상청 호출) 또는 'sample' (저장된 샘플 파일)."""
    return "api" if has_key() else "sample"


def latest_base(now: datetime) -> tuple[str, str]:
    """가장 최근 발표된 base_date, base_time 을 고릅니다.

    발표 직후에는 아직 조회가 안 될 수 있어서 15분 여유를 둡니다.
    새벽 2시 15분 전이면 전날 23시 발표분을 씁니다.
    """
    t = now - timedelta(minutes=15)
    hours = [h for h in BASE_HOURS if h <= t.hour]
    if hours:
        return t.strftime("%Y%m%d"), f"{hours[-1]:02d}00"
    prev = t - timedelta(days=1)
    return prev.strftime("%Y%m%d"), "2300"


def parse_pcp(value: str) -> float | None:
    """PCP(1시간 강수량) 문자열을 mm 숫자로 바꿉니다. 알 수 없는 값이면 None."""
    v = str(value).strip().replace(" ", "")
    if v in ("강수없음", "-", "0", ""):
        return 0.0
    if "미만" in v:  # 예: "1mm미만", "1.0mm미만"
        return 0.5
    m = re.fullmatch(r"([\d.]+)~([\d.]+)mm", v)  # 예: "30.0~50.0mm"
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2
    m = re.fullmatch(r"([\d.]+)mm이상", v)  # 예: "50.0mm이상"
    if m:
        return float(m.group(1))
    m = re.fullmatch(r"([\d.]+)(mm)?", v)  # 예: "6.0mm", "6"
    if m:
        return float(m.group(1))
    return None


def _service_error_message(text: str) -> str:
    """인증 오류 등에서 오는 XML/JSON 본문에서 원인만 뽑아냅니다(키는 포함되지 않음).

    예: SERVICE_KEY_IS_NOT_REGISTERED_ERROR (코드 30) = 등록되지 않은 인증키
    """
    found = {}
    for tag in ("errMsg", "returnReasonCode", "resultMsg"):
        # XML: <errMsg>...</errMsg>  /  JSON: "errMsg": "..."
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text) or re.search(rf'"{tag}"\s*:\s*"([^"]*)"', text)
        if m:
            found[tag] = m.group(1)
    if "errMsg" in found:
        code = found.get("returnReasonCode")
        return found["errMsg"] + (f" (코드 {code})" if code else "")
    return found.get("resultMsg", "알 수 없는 응답")


def parse_response(data: dict) -> tuple[list[dict], int]:
    """단기예보 응답(JSON)에서 (item 목록, totalCount)를 꺼냅니다. API와 샘플이 함께 씁니다."""
    try:
        header = data["response"]["header"]
    except (KeyError, TypeError):
        raise ForecastError("기상청 응답에 response.header 가 없습니다.")
    if header.get("resultCode") != "00":
        raise ForecastError(f"기상청 API 결과 코드 {header.get('resultCode')}: {header.get('resultMsg')}")
    try:
        body = data["response"]["body"]
        return body["items"]["item"], int(body.get("totalCount", 0))
    except (KeyError, TypeError, ValueError):
        raise ForecastError("기상청 응답에 response.body.items.item 이 없습니다.")


def fetch_items(base_date: str, base_time: str) -> list[dict]:
    """단기예보 API를 호출해 item 목록을 받습니다. 여러 쪽이면 모두 모읍니다."""
    key = (os.getenv("DATA_GO_KR_API_KEY") or "").strip()
    if not has_key():
        raise ForecastError("인증키(DATA_GO_KR_API_KEY)가 설정되어 있지 않습니다.", 503)

    items: list[dict] = []
    page = 1
    while True:
        params = {
            "serviceKey": key,  # Decoding 키를 그대로 넘기면 requests가 알아서 인코딩합니다
            "pageNo": page,
            "numOfRows": 1000,
            "dataType": "JSON",
            "base_date": base_date,
            "base_time": base_time,
            "nx": NX,
            "ny": NY,
        }
        try:
            res = requests.get(ENDPOINT, params=params, timeout=10)
        except requests.RequestException:
            # 예외 원문에는 키가 든 URL이 있으므로 출력하지 않습니다
            raise ForecastError("기상청 API에 연결하지 못했습니다(시간 초과 또는 네트워크 오류).")

        if res.status_code != 200:
            reason = _service_error_message(res.text)
            raise ForecastError(f"기상청 API 오류 (HTTP {res.status_code}): {reason}")

        try:
            data = res.json()
        except ValueError:
            reason = _service_error_message(res.text)
            raise ForecastError(f"기상청 API가 JSON이 아닌 응답을 보냈습니다: {reason}")

        page_items, total = parse_response(data)
        items.extend(page_items)
        if len(items) >= total or not page_items:
            return items
        page += 1


def load_sample_items() -> list[dict]:
    """샘플 CSV(day_offset, fcst_time, PCP)를 읽어 API 응답과 같은 모양의 item 목록으로 바꿉니다.

    day_offset 1 = 내일, 2 = 모레, 3 = 글피. 오늘 날짜에 더해 실제 날짜(fcstDate)를 만듭니다.
    빈 칸이나 이상한 값이 있는 행은 건너뜁니다.
    """
    today = now_kst().date()
    items = []
    try:
        with open(SAMPLE_PATH, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    offset = int(r["day_offset"])
                    fcst_date = today + timedelta(days=offset)
                    items.append(
                        {
                            "category": "PCP",
                            "fcstDate": fcst_date.strftime("%Y%m%d"),
                            "fcstTime": r["fcst_time"].strip(),
                            "fcstValue": r["PCP"].strip(),
                        }
                    )
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
    except FileNotFoundError:
        raise ForecastError("샘플 파일 data/sample/forecast_sample.csv 가 없습니다.", 503)
    except (OSError, UnicodeDecodeError):
        raise ForecastError("샘플 파일을 읽지 못했습니다.", 503)
    return items


def get_items() -> tuple[list[dict], str | None, str | None]:
    """예보 item 목록과 (base_date, base_time)을 돌려줍니다. 키가 없으면 샘플을 씁니다."""
    if has_key():
        base_date, base_time = latest_base(now_kst())
        return fetch_items(base_date, base_time), base_date, base_time
    return load_sample_items(), None, None


def rain_by_day(items: list[dict]) -> dict[str, tuple[float, int]]:
    """날짜(YYYYMMDD)별 (PCP 합계 mm, 더한 시간 수)."""
    days: dict[str, tuple[float, int]] = {}
    for it in items:
        if it.get("category") != "PCP":
            continue
        mm = parse_pcp(it.get("fcstValue", ""))
        if mm is None:
            continue  # 알 수 없는 값은 건너뜀
        total, hours = days.get(it.get("fcstDate", ""), (0.0, 0))
        days[it.get("fcstDate", "")] = (total + mm, hours + 1)
    return days


def daily_rain(target: date) -> dict:
    """target 날짜의 예상 일강수량(mm)과 계산에 쓴 정보를 돌려줍니다."""
    items, base_date, base_time = get_items()
    total, hours = rain_by_day(items).get(target.strftime("%Y%m%d"), (0.0, 0))

    if hours == 0:
        raise ForecastError(NOT_ENOUGH_DATA, 404)  # 그날 예보가 하나도 없음
    if hours < 24:
        # 일부 시간만 더하면 하루 강수량이 실제보다 적게 나오므로 거절합니다
        raise ForecastError(NOT_ENOUGH_DATA, 400)

    return {
        "mode": mode(),
        "rain_mm": round(total, 1),
        "forecast_hours": hours,
        "base_date": base_date,
        "base_time": base_time,
    }
