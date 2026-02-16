FROM python:3.14-alpine
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "/vlxmqttha.py", "/vlxmqttha.conf"]
