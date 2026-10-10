
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY fonts ./fonts
COPY backtest.py ./
RUN useradd -m sk && mkdir -p /app/data && chown -R sk:sk /app
USER sk
CMD ["python", "app/bot.py"]
