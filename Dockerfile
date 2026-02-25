FROM python:3.12-slim

WORKDIR /app

COPY requirements-remote.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements-remote.txt

COPY . .

RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
