FROM python:3.13-slim

# 容器內 stdout 不是 TTY，Python 預設走 8KB 區塊緩衝：進度輸出會累積到緩衝區滿
# 才一次沖出，在 Railway 上看起來就像「啟動後靜默數分鐘」，且同批沖出的行時間戳
# 只差微秒，無法用來判斷各階段實際耗時。關掉緩衝讓 log 即時且時間戳可信。
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

CMD ["tw-stock-rawdata"]
