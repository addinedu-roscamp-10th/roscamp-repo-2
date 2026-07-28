FROM python:3.11-slim

RUN pip install --break-system-packages uv

WORKDIR /app

COPY requirements.txt .
RUN uv pip install --system --break-system-packages --no-cache-dir -r requirements.txt

COPY router.py .

ENV CIRCLE_URL=http://model-circle-service:8080
ENV ELLIPSE_URL=http://model-ellipse-service:8080
ENV RECTANGLE_URL=http://model-rectangle-service:8080

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "router:app", "--host", "0.0.0.0", "--port", "8080"]