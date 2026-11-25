"""실제 기상청 단기예보를 한 번 받아 샘플 CSV로 저장합니다.

인증키가 없는 다른 팀은 이 샘플로 서비스를 실행합니다.
- 실행: python make_sample.py   (.env 또는 환경변수에 DATA_GO_KR_API_KEY 필요)
- 저장: data/sample/forecast_sample.csv  (열: day_offset, fcst_time, PCP)
  day_offset 1 = 내일, 2 = 모레, 3 = 글피 (저장한 날 기준). 샘플 모드에서는 실행한 날 기준으로 다시 붙습니다.
- 저장하는 것은 1시간 강수량(PCP) 예보 값뿐입니다. 인증키와 요청 URL은 저장하지 않습니다.
"""

import csv
import sys
from datetime import datetime

import forecast


def main() -> int:
    if not forecast.has_key():
        print("인증키가 없습니다. .env.example 을 .env 로 복사하고 키를 넣은 뒤 다시 실행하세요.")
        return 1

    now = forecast.now_kst()
    base_date, base_time = forecast.latest_base(now)
    try:
        items = forecast.fetch_items(base_date, base_time)
    except forecast.ForecastError as e:
        print("실패:", e.message)
        return 1

    today = now.date()
    rows = []
    for it in items:
        if it.get("category") != "PCP":
            continue
        try:
            fcst_date = datetime.strptime(it["fcstDate"], "%Y%m%d").date()
        except (KeyError, ValueError):
            continue
        offset = (fcst_date - today).days
        if 1 <= offset <= 3:  # 내일 ~ 글피만 저장
            rows.append((offset, it.get("fcstTime", ""), it.get("fcstValue", "")))
    rows.sort()

    forecast.SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(forecast.SAMPLE_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day_offset", "fcst_time", "PCP"])
        w.writerows(rows)

    hours = {d: sum(1 for r in rows if r[0] == d) for d in (1, 2, 3)}
    print(f"저장 완료: {forecast.SAMPLE_PATH.relative_to(forecast.HERE)} (발표 {base_date} {base_time}, {len(rows)}행)")
    print("예보 시간 수 — 내일:", hours[1], "/ 모레:", hours[2], "/ 글피:", hours[3], "(24면 예측 가능)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
