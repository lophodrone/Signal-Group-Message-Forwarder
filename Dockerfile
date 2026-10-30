FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY forwarder.py .
RUN mkdir -p /data
VOLUME /data
CMD ["python", "-u", "forwarder.py"]
