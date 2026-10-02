FROM python:3.10-slim
WORKDIR /app
COPY . /app
EXPOSE 10000
CMD python -m http.server 10000 & python main.py
