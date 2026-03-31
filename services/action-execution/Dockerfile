FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-tk python3-dev scrot xdotool wmctrl x11-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8007
ENV DISPLAY=:0
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8007"]
