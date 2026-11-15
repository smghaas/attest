FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN pip install --no-cache-dir -e .
ENV PYTHONUNBUFFERED=1
EXPOSE 8402
CMD ["./deploy/start.sh"]
