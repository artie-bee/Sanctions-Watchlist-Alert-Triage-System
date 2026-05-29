FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc curl
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8005 7000
CMD ["uvicorn", "alert_intake:app", "--host", "0.0.0.0", "--port", "8005"]
