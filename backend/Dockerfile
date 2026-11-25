FROM python:3.11-slim

WORKDIR /srv/app

COPY requirements.txt ./
# 依赖单一真相源：requirements.txt（新增解析库后重建镜像即生效）
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
