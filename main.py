"""비 오는 날 디저트 판매량 예측 API (FastAPI).

실행: python -m uvicorn main:app --reload  →  http://127.0.0.1:8000/docs
"""

import csv
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query

import forecast

app = FastAPI(
    title="디저트 판매량 예측 API",
    description="날짜를 넣으면 기상청 단기예보 강수량으로 디저트 판매량과 적정재고량을 예측합니다.",
)

# ── 회귀식 (sales_rain.csv 전체 727일로 추정) ─────────────────────────
# 예상판매량 = 요일별 기준값 - 0.322178 × 예상 일강수량(mm)
WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
BASE_SALES = {
    "월요일": 74.9615,
    "화요일": 74.8720,
    "수요일": 75.8384,
    "목요일": 76.9225,
    "금요일": 74.4141,
    "토요일": 92.3010,
    "일요일": 96.6299,
}
RAIN_COEF = -0.322178
STOCK_MARGIN = 8  # 2024 학습 → 2025 검증 평균절대오차 약 7.98개
FORECAST_DAYS = 3  # 입력 가능한 날짜: 내일 ~ 3일 뒤. 그날 24시간 예보가 없으면 forecast.py가 거절
OUT_OF_RANGE = "당일 기준으로 +3일내의 날짜를 입력하시오"  # 요건정의서 문구

# CSV 경로: main.py 기준 data 폴더 (고정 경로 금지)
CSV_PATH = Path(__file__).parent / "data" / "sales_rain.csv"
SOURCE_NAME = {"api": "기상청 단기예보", "sample": "샘플 데이터(data/sample)"}

Weekday = Literal["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]


def predict_sales(weekday: str, rain_mm: float) -> tuple[float, int, int]:
    """회귀식으로 (계산값, 예상판매량, 적정재고량)을 구합니다.

    - 예상판매량: 회귀식 계산값을 반올림 (71.74 → 72개)
    - 적정재고량: 반올림한 예상판매량 + 8 (72 + 8 = 80개)
    """
    raw = BASE_SALES[weekday] + RAIN_COEF * rain_mm
    raw = max(raw, 0.0)  # 판매량은 음수가 될 수 없음
    sales = int(raw + 0.5)  # 사사오입 반올림 (Python의 round는 72.5 → 72 이므로 쓰지 않음)
    stock = sales + STOCK_MARGIN
    return round(raw, 2), sales, stock


def make_messages(day: date, weekday: str, sales: int, stock: int, usual: Optional[int]) -> dict:
    """요건정의서의 출력 문구를 만듭니다.

    예) 예상 판매량 : 72 , 적정 재고량 : 80 , 평소 준비량 : 70
        2026년 11월 30일 월요일 / 디저트 72개 판매 예상 / 10개 더 준비하세요! / 총 80개 준비하세요
    평소 준비량이 없으면 '평소 준비량'과 '더 준비/줄이세요' 줄을 생략합니다.
    """
    summary = f"예상 판매량 : {sales} , 적정 재고량 : {stock}"
    lines = [f"{day.year}년 {day.month}월 {day.day}일 {weekday}", f"디저트 {sales}개 판매 예상"]
    if usual is not None:
        summary += f" , 평소 준비량 : {usual}"
        diff = stock - usual
        if diff > 0:
            lines.append(f"{diff}개 더 준비하세요!")
        elif diff < 0:
            lines.append(f"{-diff}개 줄이세요!")
        else:
            lines.append("평소대로 준비하세요!")
    lines.append(f"총 {stock}개 준비하세요")
    return {"summary": summary, "lines": lines}


def load_history() -> list[dict]:
    """과거 기록 CSV를 읽습니다. 빈 칸이나 이상한 값이 있는 행은 건너뜁니다."""
    rows = []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append(
                    {
                        "date": date.fromisoformat(r["날짜"].strip()).isoformat(),
                        "weekday": r["요일"].strip(),
                        "station": r["관측소"].strip(),
                        "rain_mm": float(r["일강수량(mm)"]),
                        "dessert_sales": int(float(r["디저트개수"])),
                    }
                )
            except (KeyError, ValueError, TypeError, AttributeError):
                continue  # 빈 칸·잘못된 값 → 건너뜀
            if rows[-1]["weekday"] not in BASE_SALES:
                rows.pop()  # 요일 값이 이상하면 건너뜀
    return rows


def allowed_dates() -> list[date]:
    """입력 가능한 날짜: 내일 ~ 3일 뒤 (오늘은 지난 시간 예보가 없어 24시간 합계를 낼 수 없음)."""
    today = forecast.now_kst().date()
    return [today + timedelta(days=i) for i in range(1, FORECAST_DAYS + 1)]


@app.get("/status")
def status():
    """지금 동작 방식(api/sample)과 입력 가능한 날짜를 알려 줍니다."""
    m = forecast.mode()
    return {
        "mode": m,
        "rain_source": SOURCE_NAME[m],
        "predict_dates": [d.isoformat() for d in allowed_dates()],
        "note": "3일 뒤(글피)는 24시간 예보가 없으면 '데이터가 부족해…' 안내가 나옵니다."
        if m == "api"
        else "인증키가 없어 data/sample/forecast_sample.csv 의 저장된 예보로 계산합니다.",
    }


@app.get("/predict")
def predict(
    date_: date = Query(..., alias="date", description="디저트 준비량을 알고 싶은 날짜 (YYYY-MM-DD), 내일 ~ 3일 뒤"),
    usual_qty: Optional[int] = Query(
        None, description="평소 준비 수량(개, 선택). 음수는 입력하지 않은 것으로 처리"
    ),
):
    """날짜의 예상 강수량(기상청 예보)으로 예상판매량, 적정재고량과 안내 문구를 돌려줍니다."""
    if date_ not in allowed_dates():
        raise HTTPException(400, OUT_OF_RANGE)

    try:
        fc = forecast.daily_rain(date_)
    except forecast.ForecastError as e:
        raise HTTPException(e.status_code, e.message)

    weekday = WEEKDAYS[date_.weekday()]
    usual = usual_qty if usual_qty is not None and usual_qty >= 0 else None  # 음수 → 입력 안 함
    raw, sales, stock = predict_sales(weekday, fc["rain_mm"])
    return {
        "date": date_.isoformat(),
        "weekday": weekday,
        "rain_source": SOURCE_NAME[fc["mode"]],
        **fc,
        "predicted_sales_raw": raw,
        "predicted_sales": sales,
        "recommended_stock": stock,
        "usual_qty": usual,
        "diff": stock - usual if usual is not None else None,  # 더 준비할 개수 (음수면 줄일 개수)
        **make_messages(date_, weekday, sales, stock, usual),
    }


@app.get("/history")
def history(
    weekday: Optional[Weekday] = Query(None, description="요일 (월요일 ~ 일요일)"),
    max_rain: Optional[float] = Query(None, ge=0, le=300, description="일강수량 최대값(mm, 0~300)"),
):
    """과거 판매 기록 목록. 요일, 최대 강수량으로 거를 수 있습니다."""
    items = load_history()
    if weekday is not None:
        items = [r for r in items if r["weekday"] == weekday]
    if max_rain is not None:
        items = [r for r in items if r["rain_mm"] <= max_rain]
    return {"count": len(items), "items": items}


@app.get("/history/{day}")
def history_one(day: date):
    """날짜 하나의 기록. 없으면 404."""
    for r in load_history():
        if r["date"] == day.isoformat():
            return r
    raise HTTPException(404, f"{day} 기록이 없습니다.")
