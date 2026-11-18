FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data

COPY . /app
RUN mkdir -p /app/data

CMD ["python", "tg_bot.py"]
