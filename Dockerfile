FROM python:alpine3.18
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "/vlxmqttha.py", "/vlxmqttha.conf"]
