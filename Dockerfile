FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "-lc", "Xvfb :99 -screen 0 1366x768x24 -ac >/tmp/xvfb.log 2>&1 & export DISPLAY=:99; sleep 1; exec python -u bot.py"]
