FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    DB_PATH=/tmp/rpg_bot.sqlite3

RUN useradd -m -u 1000 user
RUN mkdir -p /home/user/app && chown -R user:user /home/user

WORKDIR /home/user/app

COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user:user . .

EXPOSE 7860

CMD ["python", "main.py"]
